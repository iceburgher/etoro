from dataclasses import dataclass

from .config import Config


@dataclass
class State:
    day: str
    start_equity: float
    orders_today: int = 0
    halted: bool = False


def check_buy(cfg: Config, st: State, instrument_id: int, equity: float, invested: float,
              amount: float) -> tuple[bool, str]:
    """Hårda regler. Returnerar (ok, skäl). Varken strategi eller AI kan kringgå dessa."""
    if instrument_id not in cfg.allowed_instruments:
        return False, "instrument ej tillåtet"
    if st.halted:
        return False, "pausad för dagen"
    if equity < st.start_equity * (1 - cfg.daily_loss_halt_pct):
        st.halted = True
        return False, "daglig förlustgräns nådd"
    if st.orders_today >= cfg.max_orders_per_day:
        return False, "max antal ordrar för dagen"
    if amount > equity * cfg.max_per_trade_pct:
        return False, "över max per trade"
    if invested + amount > equity * cfg.max_total_exposure_pct:
        return False, "över max total exponering"
    if amount < 10:
        return False, "under minsta belopp"
    if amount * cfg.leverage < cfg.min_exposure_usd:
        return False, "under eToros minsta exponering"
    return True, "ok"
