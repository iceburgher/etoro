"""Motorn. Två jobb på samma logik, båda bakom samma databaslås:

STRATEGY_JOB   efter varje färdig 4H-stapel: avstämning + strategi + beslut + utförande
BROKER_MONITOR varje minut: avstämning + nödlägen + skyddande stängningar + väntande ordrar + beslut

Ett jobb: ta lås -> ladda tillstånd -> portfölj -> stäm av -> (strategi) -> beslut -> risk -> utför ->
verifiera -> stäm av igen -> spara -> släpp lås. Allt loggas med git-SHA.
"""
import uuid
from datetime import datetime, timedelta, timezone

from . import strategy_v1
from .alerts import CRITICAL, NORMAL
from .config import Config
from .execution import ExecutionEngine
from .models import (CLOSES, OPENS, PRIO_CLOSE, PRIO_EMERGENCY, PRIO_OPEN, Action, Actual, Desired,
                     Snapshot, TradeAction)
from .risk_engine import RiskContext, RiskEngine
from .sizing import size_trade
from .state import State
from .store import DatabaseError

NS = uuid.UUID("7c1b7f3e-3a52-4b8e-9a51-2f1f0d6a9e10")
H4 = timedelta(hours=4)
STRATEGY_JOB, MONITOR_JOB = "strategy", "monitor"

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
    return now.replace(hour=now.hour - now.hour % 4, minute=0, second=0, microsecond=0) - H4


class Engine:
    def __init__(self, cfg: Config, broker, store, *, clock, sleep, log, alert, strategy=strategy_v1):
        self.cfg, self.broker, self.store, self.clock, self.strategy = cfg, broker, store, clock, strategy
        self._log, self.alert, self.sleep = log, alert, sleep
        self.risk = RiskEngine(cfg)
        self.exe = ExecutionEngine(cfg, broker, store, self.log, sleep, alert)
        self.st = State()
        self.ctrl = {"kill_switch": False, "halt_new_entries": False, "integration_armed": False}
        self.job = ""
        self.identity_ok = False
        self.eligibility = None
        self.alerts_ready = True   # jobs.py sätter False om ingen e-postleverantör finns

    def log(self, **kw):
        kw.setdefault("git_sha", self.cfg.git_sha)
        self._log(job=self.job, **kw)
        try:
            self.store.log_event(self.job, kw.get("event", "?"), kw, self.cfg.git_sha)
        except Exception:
            pass  # loggning till databasen får aldrig stoppa handel

    # ---------- jobb ----------
    def run_job(self, job: str) -> str:
        self.job, now, holder = job, self.clock(), str(uuid.uuid4())
        if self.cfg.real_blocked_outside_production:
            self.alert(CRITICAL, "real_micro_outside_production",
                       f"EXECUTION_MODE=REAL_MICRO i {self.cfg.vercel_env or 'lokal miljö'}: körs som DRY_RUN")
        try:
            if not self.store.acquire_lock(self.cfg.lock_name, holder, now, self.cfg.lock_ttl_s):
                self.log(event="lock_busy", note="annat jobb håller låset; inget görs")
                return "locked"
        except Exception as e:
            self.alert(CRITICAL, "database_failure", "Kunde inte ta låset", error=str(e))
            return "db_error"
        try:
            self.st = self.store.load_state()
            self.ctrl = self.store.control()
            self.st.deployed_sha = self.cfg.git_sha
            self._startup()
            result = self.cycle(evaluate=job == STRATEGY_JOB)
            self.store.save_state(self.st, self.cfg.git_sha)
            return result
        except DatabaseError as e:
            self.alert(CRITICAL, "database_failure", "Databasfel under jobb; nästa jobb stämmer av mot eToro",
                       error=str(e))
            return "db_error"
        finally:
            try:
                self.store.release_lock(self.cfg.lock_name, holder)
            except Exception as e:
                self.log(event="lock_release_failed", error=str(e))  # låset går ut av sig självt (lease)

    def _startup(self):
        if self.eligibility is not None:
            return
        ident = self._safe(self.broker.identity)
        self.identity_ok = bool(ident) and ident.get("username") == self.cfg.expected_portfolio
        self.eligibility = self._safe(self.broker.eligibility, self.cfg.instrument)

    def _safe(self, fn, *a):
        try:
            out = fn(*a)
            return out
        except DatabaseError:
            raise
        except Exception as e:  # nätverksfel får aldrig ge ett beslut på fel underlag
            msg = str(e)
            self.log(event="error", where=getattr(fn, "__name__", "?"), error=msg)
            if "-> 401" in msg or "-> 403" in msg:
                self._alert_once(CRITICAL, "authentication_failure", "eToro nekar nycklarna", "day", error=msg)
            return None

    def _alert_once(self, level, kind, message, period: str, **data):
        """Avdubblat larm: högst en gång per period ('day', 'hour', 'bar' eller fri nyckel)."""
        now = self.clock()
        mark = {"day": now.date().isoformat(), "hour": now.strftime("%Y-%m-%dT%H"),
                "bar": bar_id(latest_completed_4h(now))}.get(period, period)
        if self.st.alert_marks.get(kind) == mark:
            return
        self.st.alert_marks[kind] = mark
        self.alert(level, kind, message, **data)

    # ---------- ett varv ----------
    def cycle(self, evaluate: bool) -> str:
        now, st, cfg = self.clock(), self.st, self.cfg
        latest = bar_id(latest_completed_4h(now))
        snap = self._safe(self.broker.snapshot)
        if snap is None:
            st.api_failures += 1
            if st.api_failures >= cfg.api_failure_alert_after:
                self._alert_once(CRITICAL, "repeated_api_failure", "eToro svarar inte upprepade gånger", "hour",
                                 failures=st.api_failures)
            self.log(event="uncertain", reason="kunde inte läsa portföljen, inget görs")
            return "no_snapshot"
        st.api_failures = 0
        quote = self._safe(self.broker.quote, cfg.instrument)
        fx = self._safe(self.broker.quote, cfg.fx_instrument)
        self._roll_periods(now)
        self._reconcile(snap, now, latest, quote)
        actual, conflict = self._actual(snap)
        self._invalid_state(snap, actual, conflict, now)
        self._watch(now, fx, snap, quote)
        if st.incident is None and actual in (Actual.LONG, Actual.SHORT) and st.desired.get("side") == "FLAT" \
                and not st.desired.get("exit_reason"):
            st.desired = {"side": actual.value, "adopted": True}
            self.log(event="desired_adopted_from_broker", side=actual.value)
        # Strategijobbet är den primära triggern, men varje jobb utvärderar en ny stapel som ännu inte är
        # utvärderad (t.ex. om monitor tog låset, datan inte var klar eller en order väntade). En gång per stapel.
        if latest > st.last_strategy_bar and actual != Actual.PENDING and st.incident is None:
            self._evaluate(actual, now, latest)
        elif evaluate and latest <= st.last_strategy_bar:
            self.log(event="bar_already_evaluated", bar=latest)
        ta = self._decide(snap, actual, conflict, quote, fx, now, latest)
        result = "no_action"
        if ta.action != Action.NO_ACTION:
            ctx = RiskContext(now=now, snapshot=snap, state=st, actual=actual, latest_bar=latest,
                              identity_ok=self.identity_ok, eligibility=self.eligibility, quote=quote, fx=fx,
                              unrealized_usd=self._unrealized(snap, quote), kill_switch=self._kill(),
                              halt_new_entries=self.ctrl.get("halt_new_entries", False),
                              integration_armed=self.ctrl.get("integration_armed", False),
                              alerts_ready=self.alerts_ready or not cfg.real,
                              integration_opens_sent=(self.store.count_sent_opens()
                                                      if cfg.real and cfg.integration_trade and ta.action in OPENS
                                                      else 0))
            why = self.risk.check(ta, ctx)
            if why:
                self.log(event="blocked", reasons=why, **ta.log_dict())
                result = "blocked"
                if any("USDSEK" in w for w in why):
                    self._alert_once(CRITICAL, "stale_fx", "USDSEK saknas eller är gammal: öppning stoppad", "bar")
                if ta.action in OPENS:
                    st.skipped_keys.append(ta.idempotency_key)  # samma signal prövas inte om varje minut
            else:
                result = self.exe.execute(ta, st, now)
                if ta.action in OPENS and result == "dry_run":
                    st.skipped_keys.append(ta.idempotency_key)
                if ta.action in OPENS and result == "filled":
                    self._integration_halt()
                snap2 = self._safe(self.broker.snapshot) if result not in ("dry_run", "duplicate") else None
                if snap2:
                    self._reconcile(snap2, now, latest, quote)
                    self._invalid_state(snap2, *self._actual(snap2), now)
        self.log(event="cycle", actual=actual.value, desired=st.desired.get("side"), action=ta.action.value,
                 result=result, api_reported_value=snap.api_reported_value, incident=st.incident)
        return result

    def _integration_halt(self):
        """Efter första riktiga öppningen: kill switch på igen, avarmera, stoppa nya öppningar och larma.
        Riskmotorn blockerar dessutom oberoende av detta (räknar skickade öppningar i databasen), så ett missat
        anrop här släpper inget igenom. Stängningar blockeras aldrig av kill switch."""
        if not (self.cfg.real and self.cfg.integration_trade):
            return
        try:
            self.store.set_control(kill_switch=True, halt_new_entries=True, integration_armed=False)
        except Exception as e:
            self.log(event="halt_write_failed", error=str(e))
        self._alert_once(CRITICAL, "integration_trade_done",
                         "Första riktiga affären fylld: nya öppningar stoppade tills rapporten granskats", "integration")

    def _kill(self) -> bool:
        return self.cfg.kill_switch_env or bool(self.ctrl.get("kill_switch"))

    def _watch(self, now, fx, snap, quote):
        """Kritiska larm som inte hör till en viss order."""
        st, cfg = self.st, self.cfg
        if self._kill():
            self._alert_once(CRITICAL, "kill_switch_active", "Kill switch är på: inga nya öppningar", "day")
        if fx:
            unreal = self._unrealized(snap, quote)
            cap = cfg.allocated_capital_sek
            if -(st.realized_day_usd + unreal) * fx.mid >= cap * cfg.daily_loss_pct:
                self._alert_once(CRITICAL, "daily_loss_limit", "Daglig förlustgräns nådd: inga nya öppningar", "day")
            if -(st.realized_week_usd + unreal) * fx.mid >= cap * cfg.weekly_loss_pct:
                self._alert_once(CRITICAL, "weekly_loss_limit", "Veckans förlustgräns nådd: inga nya öppningar",
                                 st.week)

    # ---------- avstämning ----------
    def _roll_periods(self, now: datetime):
        st = self.st
        day, (y, w, _) = now.date().isoformat(), now.isocalendar()
        week = f"{y}-W{w:02d}"
        if st.day != day:
            st.day, st.realized_day_usd, st.opens_today = day, 0.0, 0
        if st.week != week:
            st.week, st.realized_week_usd = week, 0.0

    def _realize(self, meta: dict, quote) -> float | None:
        """Bokför realiserat resultat. Saknas kurs: hämta igen; saknas den fortfarande, räkna med stoppen
        (sämsta planerade utfall), så att förlustgränserna aldrig underskattas."""
        quote = quote or self._safe(self.broker.quote, self.cfg.instrument)
        long = meta["side"] == "LONG"
        if quote:
            exit_px = quote.bid if long else quote.ask
        elif meta.get("stop"):
            exit_px = meta["stop"]
            self.log(event="realized_estimated_at_stop", meta=meta)
        else:
            exit_px = meta["open_rate"]
        if not meta.get("units") or not meta.get("open_rate"):
            return None
        pnl = (exit_px - meta["open_rate"]) * meta["units"] * (1 if long else -1)
        self.st.realized_day_usd += pnl
        self.st.realized_week_usd += pnl
        return pnl

    def _reconcile(self, snap: Snapshot, now: datetime, latest: str, quote):
        st, cfg = self.st, self.cfg
        mine = {p.position_id: p for p in snap.for_instrument(cfg.instrument)}
        handled = set()
        p = st.pending
        if p:
            age = (now - datetime.fromisoformat(p["submitted"])).total_seconds()
            if p["kind"] == "open":
                try:
                    s, lookup_ok = self.broker.lookup(p["key"]), True
                except Exception as e:  # nätverksfel = vet inte; får aldrig tolkas som "okänd order"
                    s, lookup_ok = None, False
                    self.log(event="lookup_failed", key=p["key"], error=str(e))
                if age > cfg.pending_timeout_s and (not lookup_ok or (s and not s.filled and not s.failed)):
                    self._alert_once(CRITICAL, "unknown_order_state",
                                     "Öppningsorder fortfarande oavgjord efter tidsgräns; inga nya affärer tills den är avgjord",
                                     f"stuck-{p['key']}", key=p["key"], status=getattr(s, "status_id", None))
                if s and s.filled:
                    self.exe.remember_fill(st, s.position_ids, None)
                    st.pending = None
                    st.opens_today += 1
                    self.exe._safe_intent(p["key"], status="filled")
                    self._integration_halt()
                    self.log(event="reconciled_fill", key=p["key"], position_ids=list(s.position_ids))
                    self.alert(NORMAL, p["action"], f"{p['action']} fylld (bekräftad vid avstämning)", key=p["key"])
                elif s and s.failed:
                    st.pending = None
                    self.exe._safe_intent(p["key"], status="rejected", error=s.error)
                    self.log(event="reconciled_reject", key=p["key"], error=s.error)
                elif s is None and lookup_ok and age > cfg.pending_timeout_s:
                    st.pending = None
                    self.exe._safe_intent(p["key"], status="lost")
                    self.alert(CRITICAL, "unknown_order_state", "Order okänd hos eToro efter tidsgräns; markerad förlorad",
                               key=p["key"])
            else:
                remaining = [pid for pid in p["position_ids"] if pid in mine]
                if not remaining:
                    pnl = 0.0
                    for pid in p["position_ids"]:
                        meta = st.known_positions.get(str(pid))
                        if meta:
                            pnl += self._realize(meta, quote) or 0.0
                        handled.add(str(pid))
                    st.pending, st.last_exit_bar, st.close_attempts = None, latest, 0
                    self.exe._safe_intent(p["key"], status="closed")
                    self.log(event="close_reconciled", position_ids=p["position_ids"], exit_reason=p.get("exit_reason"))
                    self.alert(NORMAL, p["action"], f"{p['action']}: {p.get('exit_reason')}",
                               position_ids=p["position_ids"], exit_reason=p.get("exit_reason"),
                               est_realized_pnl_usd=round(pnl, 4))
                elif age > cfg.pending_timeout_s:
                    st.pending = None
                    st.close_attempts += 1
                    self.alert(CRITICAL, "unknown_order_state", "Stängning ej bekräftad inom tidsgräns; försöker igen",
                               remaining=remaining)
        for pid, meta in list(st.known_positions.items()):
            if int(pid) not in mine and pid not in handled:
                pnl = self._realize(meta, quote)
                st.last_exit_bar = latest
                kind = self._broker_exit_kind(meta, quote)
                if kind in ("stop_exit", "target_exit"):
                    self.alert(NORMAL, kind, f"Position {pid} stängd av eToro ({kind})", position_id=pid,
                               est_realized_pnl_usd=pnl)
                else:
                    self.alert(CRITICAL, "reconciliation_mismatch",
                               f"Position {pid} försvann utan att agenten stängde den", position_id=pid, meta=meta)
                self.log(event="broker_closed", position_id=pid, kind=kind)
        for pid, pos in mine.items():
            if str(pid) not in st.known_positions and not (p and p["kind"] == "open"):
                self.alert(CRITICAL, "unexpected_broker_position", f"Okänd position {pid} hos eToro",
                           position_id=pid, side=pos.side.value, has_stop=pos.has_stop)
        st.known_positions = {str(pid): {"side": pos.side.value, "units": pos.units, "open_rate": pos.open_rate,
                                         "stop": pos.stop_loss, "target": pos.take_profit}
                              for pid, pos in mine.items()}

    @staticmethod
    def _broker_exit_kind(meta: dict, quote) -> str:
        if not quote:
            return "unknown"
        long = meta["side"] == "LONG"
        px = quote.bid if long else quote.ask
        stop, target = meta.get("stop"), meta.get("target")
        tol = 0.002
        if stop and (px <= stop * (1 + tol) if long else px >= stop * (1 - tol)):
            return "stop_exit"
        if target and (px >= target * (1 - tol) if long else px <= target * (1 + tol)):
            return "target_exit"
        return "unknown"

    def _actual(self, snap: Snapshot) -> tuple[Actual, bool]:
        mine = snap.for_instrument(self.cfg.instrument)
        longs, shorts = [p for p in mine if p.is_buy], [p for p in mine if not p.is_buy]
        conflict = bool(longs and shorts)
        if conflict or self.st.pending or self.cfg.instrument in snap.pending_instruments:
            return Actual.PENDING, conflict
        return (Actual.LONG if longs else Actual.SHORT if shorts else Actual.FLAT), False

    def _invalid_state(self, snap: Snapshot, actual: Actual, conflict: bool, now: datetime):
        """LONG + SHORT samtidigt: blockera allt, stäng båda, verifiera FLAT, först då återuppta."""
        st = self.st
        if conflict and st.incident is None:
            st.incident = {"kind": "long_and_short", "since": now.isoformat()}
            st.desired = {"side": "FLAT", "exit_reason": "invalid_state_long_and_short"}
            self.store.open_incident("long_and_short", {"positions": [p.position_id for p in snap.positions]})
            self.alert(CRITICAL, "long_and_short", "LONG och SHORT samtidigt: stänger båda, inga nya affärer",
                       positions=[p.position_id for p in snap.for_instrument(self.cfg.instrument)])
        elif st.incident and not snap.for_instrument(self.cfg.instrument) and actual == Actual.FLAT:
            self.store.resolve_incidents(st.incident["kind"])
            self.alert(CRITICAL, "incident_resolved", "Avstämning klar: FLAT hos eToro, agenten återupptas",
                       incident=st.incident)
            st.incident = None
            st.desired = {"side": "FLAT"}

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
        self.log(event="strategy", bar=latest, regime=reg, actual=actual.value, desired=desired, version=s.VERSION)

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
        if st.pending:
            return none  # 3. avstämning av väntande order pågår
        # 1. nödläge: ogiltigt läge -> stäng allt (long först, sedan short), aldrig öppna
        if st.incident:
            for side in ("LONG", "SHORT"):
                if any(p.is_buy == (side == "LONG") for p in mine):
                    return self._close(snap, side, "invalid_state_long_and_short", PRIO_EMERGENCY, actual, now)
            return none
        if actual == Actual.PENDING:
            return none
        for p in mine:
            if not p.has_stop:
                self._alert_once(CRITICAL, "missing_broker_stop", f"Position {p.position_id} saknar stop hos eToro",
                                 f"nostop-{p.position_id}", position_id=p.position_id)
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
        if key in self.st.skipped_keys or "swing" not in d:
            return None
        intent = self.store.get_order_intent(key)
        if intent and intent["status"] != "created":
            return None  # redan skickad/avgjord; avstämningen sköter resten
        if not quote or not fx:
            self.log(event="open_skipped", reason="pris eller USDSEK saknas")
            return None
        side = d["side"]
        entry = quote.ask if side == "LONG" else quote.bid
        stop, target = self.strategy.stop_from(side, d["swing"], d["atr"], entry)
        sz = size_trade(allocated_capital_sek=cfg.allocated_capital_sek, price_usd=entry, stop_usd=stop,
                        leverage=cfg.leverage, usdsek_rate=fx.mid, fx_timestamp=fx.time.isoformat(),
                        fx_source="eToro rates, instrument 58 USDSEK",
                        risk_pct=cfg.integration_risk_per_trade if cfg.real and cfg.integration_trade
                        else cfg.risk_per_trade)
        return self._ta(action, actual, now, entry_reason=d.get("entry_reason"), proposed_entry=entry,
                        proposed_stop=round(stop, 4), proposed_target=round(target, 4),
                        risk_budget=sz.risk_budget_sek, position_size=sz.units, leverage=cfg.leverage,
                        sizing=sz.as_dict(), priority=PRIO_OPEN, idempotency_key=key)
