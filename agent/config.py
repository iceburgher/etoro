import os
from dataclasses import dataclass, field

MODES = ("DRY_RUN", "REAL_MICRO")


def _env(name, default, cast=str):
    v = os.getenv(name)
    return default if v in (None, "") else cast(v)


@dataclass(frozen=True)
class Config:
    base_url: str = "https://public-api.etoro.com"
    # Körläge som begärts. effective_mode avgör vad som faktiskt gäller (Preview = alltid DRY_RUN).
    mode: str = field(default_factory=lambda: _env("EXECUTION_MODE", "DRY_RUN"))
    # Vercel sätter VERCEL_ENV = production | preview | development. Tomt = lokalt.
    vercel_env: str = field(default_factory=lambda: _env("VERCEL_ENV", ""))
    git_sha: str = field(default_factory=lambda: _env("VERCEL_GIT_COMMIT_SHA", "local"))
    # Integrationsaffär: stoppa nya öppningar automatiskt efter första fyllda öppning i REAL_MICRO.
    integration_trade: bool = field(default_factory=lambda: _env("REAL_MICRO_INTEGRATION", "1") == "1")
    # Måste matcha /api/v1/me.username för att öppningar ska tillåtas
    expected_portfolio: str = field(default_factory=lambda: _env("EXPECTED_PORTFOLIO", "AI Burger-UMYYUR"))
    # Instrument: GLD som CFD (eToro 3025), USD. USDSEK = eToro 58.
    instrument: int = 3025
    instrument_symbol: str = "GLD"
    instrument_currency: str = "USD"
    fx_instrument: int = 58
    leverage: int = 2
    max_positions: int = 1
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
    # Jobb, lås, väntetider (Vercel-funktion max 60 s)
    lock_name: str = "gld-trading"
    lock_ttl_s: int = 120
    order_poll_tries: int = 8
    order_poll_sleep_s: float = 2.0
    pending_timeout_s: int = 600
    api_failure_alert_after: int = 3
    kill_switch_env: bool = field(default_factory=lambda: _env("KILL_SWITCH", "0").lower() in ("1", "yes", "on", "true"))

    @property
    def effective_mode(self) -> str:
        if self.mode == "REAL_MICRO" and self.vercel_env != "production":
            return "DRY_RUN"
        return self.mode if self.mode in MODES else "DRY_RUN"

    @property
    def real(self) -> bool:
        return self.effective_mode == "REAL_MICRO"

    @property
    def real_blocked_outside_production(self) -> bool:
        return self.mode == "REAL_MICRO" and self.vercel_env != "production"
