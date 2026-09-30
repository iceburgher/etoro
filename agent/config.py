import os
from dataclasses import dataclass, field

MODES = ("DRY_RUN", "REAL_MICRO")


def _env(name, default, cast=str):
    v = os.getenv(name)
    return default if v in (None, "") else cast(v)


@dataclass(frozen=True)
class Config:
    base_url: str = "https://public-api.etoro.com"
    # Körläge. REAL_MICRO kräver uttrycklig konfiguration; allt annat = torrkörning.
    mode: str = field(default_factory=lambda: _env("EXECUTION_MODE", "DRY_RUN"))
    # Måste matcha /api/v1/me.username för att REAL_MICRO ska starta
    expected_portfolio: str = field(default_factory=lambda: _env("EXPECTED_PORTFOLIO", "AI Burger-UMYYUR"))
    # Instrument: GLD som CFD (eToro 3025), USD. USDSEK = eToro 58.
    instrument: int = 3025
    instrument_symbol: str = "GLD"
    instrument_currency: str = "USD"
    fx_instrument: int = 58
    leverage: int = 2
    # Riskbas: avsatt kapital i SEK. eToros API-värde (10 000) används aldrig för risk.
    allocated_capital_sek: float = field(default_factory=lambda: _env("ALLOCATED_CAPITAL_SEK", 9983.59, float))
    portfolio_display_currency: str = "SEK"
    risk_per_trade: float = 0.0025
    daily_loss_pct: float = 0.01
    weekly_loss_pct: float = 0.025
    max_exposure_pct: float = 0.50       # nominell exponering / kapital
    max_opens_per_day: int = 2
    # Färskhet
    fx_max_age_s: int = 300
    quote_max_age_s: int = 120
    bar_grace_s: int = 1800              # hur länge efter stapelns slut en signal får användas
    # Loopar och väntetider
    monitor_seconds: int = 60
    order_poll_tries: int = 10
    order_poll_sleep_s: float = 2.0
    pending_timeout_s: int = 600
    # Filer
    state_file: str = field(default_factory=lambda: _env("STATE_FILE", "state.json"))
    kill_switch_file: str = field(default_factory=lambda: _env("KILL_SWITCH_FILE", "KILL_SWITCH"))

    @property
    def real(self) -> bool:
        return self.mode == "REAL_MICRO"

    def kill_switch(self) -> bool:
        return os.getenv("KILL_SWITCH", "").lower() in ("1", "yes", "on", "true") or os.path.exists(self.kill_switch_file)
