"""Motorn: ett varv = hämta portfölj, stäm av, (ny 4H-stapel: strategi), besluta, risk, utför, verifiera, spara.

Strategiloopen körs bara när en ny 4H-stapel är färdig. Broker/risk-övervakningen körs varje varv.
Båda går i samma process, så två ordrar skickas aldrig samtidigt.
"""
import uuid
from datetime import datetime, timedelta, timezone

from . import strategy_v1
from .config import Config
from .execution import ExecutionEngine
from .models import (CLOSES, OPENS, PRIO_CLOSE, PRIO_EMERGENCY, PRIO_OPEN, Action, Actual, Desired,
                     Snapshot, TradeAction)
from .risk_engine import RiskContext, RiskEngine
from .sizing import size_trade
from .state import StateStore

NS = uuid.UUID("7c1b7f3e-3a52-4b8e-9a51-2f1f0d6a9e10")
H4 = timedelta(hours=4)

TABLE = {
    (Actual.FLAT, Desired.LONG): Action.OPEN_LONG,
    (Actual.FLAT, Desired.SHORT): Action.OPEN_SHORT,
    (Actual.FLAT, Desired.FLAT): Action.NO_ACTION,
    (Actual.LONG, Desired.LONG): Action.NO_ACTION,
    (Actual.LONG, Desired.FLAT): Action.CLOSE_LONG,
    (Actual.LONG, Desired.SHORT): Action.CLOSE_LONG,
    (Actual.SHORT, Desired.SHORT): Action.NO_ACTION,
    (Actual.SHORT, Desired.FLAT): Action.CLOSE_SHORT,
    (Actual.SHORT, Desired.LONG): Action.CLOSE_SHORT,
}


def bar_id(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_completed_4h(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    floor = now.replace(hour=now.hour - now.hour % 4, minute=0, second=0, microsecond=0)
    return floor - H4


class Engine:
    def __init__(self, cfg: Config, broker, store: StateStore, *, clock, sleep, log, strategy=strategy_v1):
        self.cfg, self.broker, self.store, self.clock, self.log, self.strategy = cfg, broker, store, clock, log, strategy
        self.st = store.load()
        self.risk = RiskEngine(cfg)
        self.exe = ExecutionEngine(cfg, broker, store, log, sleep)
        self.identity: dict = {}
        self.identity_ok = False
        self.eligibility = None

    # ---------- start ----------
    def startup(self) -> dict:
        self.identity = self.broker.identity()
        self.identity_ok = self.identity.get("username") == self.cfg.expected_portfolio
        self.eligibility = self.broker.eligibility(self.cfg.instrument)
        return self.identity

    def _safe(self, fn, *a):
        try:
            return fn(*a)
        except Exception as e:  # nätverksfel får aldrig krascha loopen eller ge ett beslut på fel underlag
            self.log(event="error", where=getattr(fn, "__name__", "?"), error=str(e))
            return None

    # ---------- ett varv ----------
    def cycle(self) -> str:
        now, st, cfg = self.clock(), self.st, self.cfg
        latest = bar_id(latest_completed_4h(now))
        snap = self._safe(self.broker.snapshot)
        if snap is None:
            self.log(event="uncertain", reason="kunde inte läsa portföljen, inget görs")
            return "no_snapshot"
        quote = self._safe(self.broker.quote, cfg.instrument)
        fx = self._safe(self.broker.quote, cfg.fx_instrument)
        self._roll_periods(now)
        self._reconcile(snap, now, latest, quote)
        actual, conflict = self._actual(snap)
        if actual in (Actual.LONG, Actual.SHORT) and st.desired.get("side") == "FLAT" \
                and not st.desired.get("exit_reason"):
            # Position som agenten inte beslutat om (omstart, tappat tillstånd): ta över den i stället för
            # att stänga den blint. Strategins exits och stop gäller från nästa stapel.
            st.desired = {"side": actual.value, "adopted": True}
            self.log(event="desired_adopted_from_broker", side=actual.value)
        if latest > st.last_strategy_bar and actual != Actual.PENDING:
            self._evaluate(actual, now, latest)
        ta = self._decide(snap, actual, conflict, quote, fx, now, latest)
        result = "no_action"
        if ta.action != Action.NO_ACTION:
            ctx = RiskContext(now=now, snapshot=snap, state=st, actual=actual, latest_bar=latest,
                              identity_ok=self.identity_ok, eligibility=self.eligibility, quote=quote, fx=fx,
                              unrealized_usd=self._unrealized(snap, quote), kill_switch=cfg.kill_switch())
            why = self.risk.check(ta, ctx)
            if why:
                self.log(event="blocked", reasons=why, **ta.log_dict())
                result = "blocked"
                if ta.action in OPENS:
                    st.executed_keys.append(ta.idempotency_key)  # samma signal prövas inte om varje minut
            else:
                result = self.exe.execute(ta, st, now)
                if ta.action in OPENS and result == "dry_run":
                    st.executed_keys.append(ta.idempotency_key)
                snap2 = self._safe(self.broker.snapshot) if result not in ("dry_run", "duplicate") else None
                if snap2:
                    self._reconcile(snap2, now, latest, quote)
        self.store.save(st)
        self.log(event="cycle", actual=actual.value, desired=st.desired.get("side"), action=ta.action.value,
                 result=result, api_reported_value=snap.api_reported_value)
        return result

    # ---------- avstämning ----------
    def _roll_periods(self, now: datetime):
        st = self.st
        day, (y, w, _) = now.date().isoformat(), now.isocalendar()
        week = f"{y}-W{w:02d}"
        if st.day != day:
            st.day, st.realized_day_usd, st.opens_today = day, 0.0, 0
        if st.week != week:
            st.week, st.realized_week_usd = week, 0.0

    def _realize(self, meta: dict, quote):
        if not quote:
            return
        exit_px = quote.bid if meta["side"] == "LONG" else quote.ask
        pnl = (exit_px - meta["open_rate"]) * meta["units"] * (1 if meta["side"] == "LONG" else -1)
        self.st.realized_day_usd += pnl
        self.st.realized_week_usd += pnl

    def _reconcile(self, snap: Snapshot, now: datetime, latest: str, quote):
        st, cfg = self.st, self.cfg
        mine = {p.position_id: p for p in snap.for_instrument(cfg.instrument)}
        handled = set()
        p = st.pending
        if p:
            age = (now - datetime.fromisoformat(p["submitted"])).total_seconds()
            if p["kind"] == "open":
                s = self._safe(self.broker.lookup, p["key"])
                if s and s.filled:
                    st.pending = None
                    st.opens_today += 1
                    self.log(event="reconciled_fill", key=p["key"], position_ids=list(s.position_ids))
                elif s and s.failed:
                    st.pending = None
                    self.log(event="reconciled_reject", key=p["key"], error=s.error)
                elif s is None and age > cfg.pending_timeout_s:
                    st.pending = None
                    self.log(event="pending_open_lost", key=p["key"])
            else:
                remaining = [pid for pid in p["position_ids"] if pid in mine]
                if not remaining:
                    for pid in p["position_ids"]:
                        meta = st.known_positions.get(str(pid))
                        if meta:
                            self._realize(meta, quote)
                        handled.add(str(pid))
                    st.pending, st.last_exit_bar, st.close_attempts = None, latest, 0
                    self.log(event="close_reconciled", position_ids=p["position_ids"], exit_reason=p.get("exit_reason"))
                elif age > cfg.pending_timeout_s:
                    st.pending = None
                    st.close_attempts += 1
                    self.log(event="close_timeout_retry", remaining=remaining)
        for pid, meta in list(st.known_positions.items()):
            if int(pid) not in mine and pid not in handled:
                self._realize(meta, quote)
                st.last_exit_bar = latest
                self.log(event="broker_closed", position_id=pid, note="stängd hos eToro (stop/mål eller manuellt)")
        for pid, pos in mine.items():
            if str(pid) not in st.known_positions:
                self.log(event="adopted_position", position_id=pid, side=pos.side.value, has_stop=pos.has_stop)
        st.known_positions = {str(pid): {"side": pos.side.value, "units": pos.units, "open_rate": pos.open_rate,
                                         "stop": pos.stop_loss, "target": pos.take_profit}
                              for pid, pos in mine.items()}

    def _actual(self, snap: Snapshot) -> tuple[Actual, bool]:
        mine = snap.for_instrument(self.cfg.instrument)
        longs, shorts = [p for p in mine if p.is_buy], [p for p in mine if not p.is_buy]
        conflict = bool(longs and shorts)
        if conflict or self.st.pending or self.cfg.instrument in snap.pending_instruments:
            return Actual.PENDING, conflict
        return (Actual.LONG if longs else Actual.SHORT if shorts else Actual.FLAT), False

    def _unrealized(self, snap: Snapshot, quote) -> float:
        if not quote:
            return 0.0
        return sum((quote.mid - p.open_rate) * p.units * (1 if p.is_buy else -1)
                   for p in snap.for_instrument(self.cfg.instrument))

    # ---------- strategi (bara på ny färdig 4H-stapel) ----------
    def _evaluate(self, actual: Actual, now: datetime, latest: str):
        s, st, iid = self.strategy, self.st, self.cfg.instrument
        daily = self._safe(self.broker.candles, iid, "OneDay", 120)
        h4 = self._safe(self.broker.candles, iid, "FourHours", 200)
        if daily is None or h4 is None:
            return
        daily = [b for b in daily if b.start + timedelta(days=1) <= now]
        h4 = [b for b in h4 if b.start + H4 <= now]
        if not h4 or bar_id(h4[-1].start) != latest:
            self.log(event="data_not_ready", latest=latest)
            return
        reg = s.regime([b.close for b in daily])
        if actual in (Actual.LONG, Actual.SHORT):
            why = s.exit_reason(actual.value, reg, h4)
            if not why:
                desired = {**st.desired, "side": actual.value}
            else:
                opp = "SHORT" if actual == Actual.LONG else "LONG"
                reason = s.signal(h4, opp) if reg == opp else None
                desired = self._entry(h4, opp, reason, latest) if reason else {"side": "FLAT", "signal_bar": latest}
                desired["exit_reason"] = why
        else:
            reason = s.signal(h4, reg) if reg else None
            desired = self._entry(h4, reg, reason, latest) if reason else {"side": "FLAT", "signal_bar": latest}
        st.desired, st.last_strategy_bar = desired, latest
        self.log(event="strategy", bar=latest, regime=reg, actual=actual.value, desired=desired,
                 version=s.VERSION)

    def _entry(self, h4, side: str, reason: str, latest: str) -> dict:
        swing, a = self.strategy.swing_and_atr(h4, side)
        return {"side": side, "signal_bar": latest, "entry_reason": reason, "swing": swing, "atr": a,
                "ref_close": h4[-1].close}

    # ---------- beslut ----------
    def _ta(self, action: Action, actual: Actual, now: datetime, **kw) -> TradeAction:
        d = self.st.desired
        return TradeAction(action=action, instrument=self.cfg.instrument, current_position=actual,
                           desired_position=Desired(d.get("side", "FLAT")), signal_timestamp=now.isoformat(),
                           signal_bar=kw.pop("signal_bar", d.get("signal_bar") or ""),
                           strategy_version=self.strategy.VERSION, **kw)

    def _close(self, snap: Snapshot, side: str, reason: str, prio: int, actual: Actual, now: datetime) -> TradeAction:
        ids = tuple(sorted(p.position_id for p in snap.for_instrument(self.cfg.instrument) if p.is_buy == (side == "LONG")))
        key = str(uuid.uuid5(NS, f"{self.strategy.VERSION}|CLOSE_{side}|{ids}|{self.st.close_attempts}"))
        return self._ta(Action(f"CLOSE_{side}"), actual, now, exit_reason=reason, position_ids=ids,
                        priority=prio, idempotency_key=key)

    def _decide(self, snap, actual, conflict, quote, fx, now, latest) -> TradeAction:
        st = self.st
        none = self._ta(Action.NO_ACTION, actual, now, idempotency_key="none")
        mine = snap.for_instrument(self.cfg.instrument)
        # 1. nödläge
        if conflict and not st.pending:
            return self._close(snap, "LONG", "conflict_long_and_short", PRIO_EMERGENCY, actual, now)
        if st.pending or actual == Actual.PENDING:
            return none  # 3. avstämning pågår
        for p in mine:
            if not p.has_stop:
                return self._close(snap, p.side.value, "no_broker_stop", PRIO_EMERGENCY, actual, now)
        # 2. skyddande stängning om eToros stop/mål passerats men positionen ligger kvar
        if quote and actual in (Actual.LONG, Actual.SHORT):
            for p in mine:
                px = quote.bid if p.is_buy else quote.ask
                hit_stop = p.stop_loss and (px <= p.stop_loss if p.is_buy else px >= p.stop_loss)
                hit_target = p.take_profit and (px >= p.take_profit if p.is_buy else px <= p.take_profit)
                if hit_stop or hit_target:
                    return self._close(snap, actual.value, "stop_hit" if hit_stop else "target_hit",
                                       PRIO_CLOSE, actual, now)
        # 4. strategins önskade läge
        desired = Desired(st.desired.get("side", "FLAT"))
        action = TABLE[(actual, desired)]
        if action in CLOSES:
            return self._close(snap, actual.value, st.desired.get("exit_reason") or "reversal", PRIO_CLOSE, actual, now)
        if action in OPENS:
            return self._open(action, actual, quote, fx, now) or none
        return none

    def _open(self, action: Action, actual: Actual, quote, fx, now) -> TradeAction | None:
        d, cfg = self.st.desired, self.cfg
        key = str(uuid.uuid5(NS, f"{self.strategy.VERSION}|{action.value}|{d.get('signal_bar')}"))
        if key in self.st.executed_keys or "swing" not in d:
            return None
        if not quote or not fx:
            self.log(event="open_skipped", reason="pris eller USDSEK saknas")
            return None
        side = d["side"]
        entry = quote.ask if side == "LONG" else quote.bid
        stop, target = self.strategy.stop_from(side, d["swing"], d["atr"], entry)
        sz = size_trade(allocated_capital_sek=cfg.allocated_capital_sek, price_usd=entry, stop_usd=stop,
                        leverage=cfg.leverage, usdsek_rate=fx.mid, fx_timestamp=fx.time.isoformat(),
                        fx_source="eToro rates, instrument 58 USDSEK", risk_pct=cfg.risk_per_trade)
        return self._ta(action, actual, now, entry_reason=d.get("entry_reason"), proposed_entry=entry,
                        proposed_stop=round(stop, 4), proposed_target=round(target, 4),
                        risk_budget=sz.risk_budget_sek, position_size=sz.units, leverage=cfg.leverage,
                        sizing=sz.as_dict(), priority=PRIO_OPEN, idempotency_key=key)
