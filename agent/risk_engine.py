"""Riskmotorn: får stoppa ny exponering, aldrig en stängning (utom när positionen inte stämmer)."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Config
from .models import CLOSES, OPENS, Action, Actual, Eligibility, Quote, Snapshot, TradeAction
from .state import State


@dataclass
class RiskContext:
    now: datetime
    snapshot: Snapshot
    state: State
    actual: Actual
    latest_bar: str
    identity_ok: bool
    eligibility: Eligibility | None
    quote: Quote | None
    fx: Quote | None
    unrealized_usd: float
    kill_switch: bool


class RiskEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def check(self, ta: TradeAction, ctx: RiskContext) -> list[str]:
        if ta.action in OPENS:
            return self._open(ta, ctx)
        if ta.action in CLOSES:
            return self._close(ta, ctx)
        return []

    def _open(self, ta: TradeAction, c: RiskContext) -> list[str]:
        cfg, why = self.cfg, []
        if not c.identity_ok:
            why.append("fel eller okänd agentportfölj")
        if ta.instrument != cfg.instrument or not c.eligibility or c.eligibility.symbol != cfg.instrument_symbol:
            why.append("fel instrument")
        if c.snapshot.currency_id != 1 or cfg.instrument_currency != "USD":
            why.append("okänd kontovaluta eller prisvaluta")
        if c.actual != Actual.FLAT or c.state.pending or cfg.instrument in c.snapshot.pending_instruments:
            why.append("motstridig position eller väntande order")
        if c.state.last_exit_bar and ta.signal_bar <= c.state.last_exit_bar:
            why.append("väntar på nästa färdiga stapel efter stängning")
        if ta.signal_bar != c.latest_bar:
            why.append("signalstapeln är inte den senaste färdiga")
        else:
            bar_end = datetime.fromisoformat(ta.signal_bar.replace("Z", "+00:00")) + timedelta(hours=4)
            if (c.now - bar_end).total_seconds() > cfg.bar_grace_s:
                why.append("signalen är för gammal")
        if not c.quote or not c.quote.realtime or (c.now - c.quote.time).total_seconds() > cfg.quote_max_age_s:
            why.append("marknadsdata saknas eller är gammal")
        if not c.fx or not c.fx.realtime or (c.now - c.fx.time).total_seconds() > cfg.fx_max_age_s:
            why.append("USDSEK saknas eller är gammal")
        if c.kill_switch:
            why.append("kill switch på")
        if c.state.opens_today >= cfg.max_opens_per_day:
            why.append("max öppningar i dag")
        why += self._stop_and_size(ta, c)
        why += self._loss_limits(c)
        return why

    def _stop_and_size(self, ta: TradeAction, c: RiskContext) -> list[str]:
        why, e, s, t = [], ta.proposed_entry, ta.proposed_stop, ta.proposed_target
        long = ta.action == Action.OPEN_LONG
        if not ((s < e < t) if long else (t < e < s)):
            why.append("ogiltig stop/mål")
        sz = ta.sizing or {}
        if not sz.get("within_budget"):
            why.append("förlust vid stop över riskbudget")
        units, exposure = ta.position_size or 0, sz.get("notional_exposure_usd", 0)
        el = c.eligibility
        cfgl = el.configs.get(("long" if long else "short", ta.leverage)) if el else None
        if not cfgl:
            why.append(f"hävstång x{ta.leverage} ej tillåten")
        else:
            max_sl_pct, min_amount = cfgl
            sl_pct_of_margin = abs(e - s) / e * ta.leverage * 100
            if sl_pct_of_margin > max_sl_pct:
                why.append("stop längre bort än eToro tillåter")
            if sz.get("margin_required_usd", 0) < min_amount:
                why.append("insats under eToros minimum")
        if el and (exposure < el.min_exposure or units > el.max_units or units <= 0):
            why.append("ogiltig storlek")
        cap_usd = sz.get("allocated_capital_usd", 0)
        held = sum(p.units * (c.quote.mid if c.quote else p.open_rate) for p in c.snapshot.positions)
        if exposure + held > cap_usd * self.cfg.max_exposure_pct:
            why.append("över max exponering")
        return why

    def _loss_limits(self, c: RiskContext) -> list[str]:
        if not c.fx:
            return ["USDSEK saknas för förlustgränser"]
        cap = self.cfg.allocated_capital_sek
        day_sek = (c.state.realized_day_usd + c.unrealized_usd) * c.fx.mid
        week_sek = (c.state.realized_week_usd + c.unrealized_usd) * c.fx.mid
        why = []
        if -day_sek >= cap * self.cfg.daily_loss_pct:
            why.append("daglig förlustgräns nådd")
        if -week_sek >= cap * self.cfg.weekly_loss_pct:
            why.append("veckans förlustgräns nådd")
        return why

    def _close(self, ta: TradeAction, c: RiskContext) -> list[str]:
        by_id = {p.position_id: p for p in c.snapshot.positions}
        want_buy = ta.action == Action.CLOSE_LONG
        why = []
        for pid in ta.position_ids:
            p = by_id.get(pid)
            if not p:
                why.append(f"position {pid} finns inte")
            elif p.instrument_id != self.cfg.instrument or p.is_buy != want_buy:
                why.append(f"position {pid} har fel instrument eller riktning")
            elif p.units <= 0:
                why.append(f"position {pid} saknar känt antal enheter")
        return why
