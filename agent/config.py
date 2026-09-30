import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    base_url: str = "https://public-api.etoro.com"
    # Bara dessa instrument får handlas. Allt annat blockeras.
    allowed_instruments: tuple = (3025,)  # 3025 = GLD som CFD (min 10 USD). 18 = GOLD (min 1000 USD exponering)
    # Risk (i USD / procent)
    max_per_trade_pct: float = 0.20      # max andel av kontot per trade
    max_total_exposure_pct: float = 0.40  # max andel av kontot investerat totalt
    stop_loss_pct: float = 0.03          # obligatorisk stop loss under inköpspris
    take_profit_pct: float = 0.06
    daily_loss_halt_pct: float = 0.03    # pausa resten av dagen vid -3 %
    max_orders_per_day: int = 4
    # "real" = äg riktiga andelar. "cfd" = derivat med nattavgifter. Väljs medvetet, aldrig automatiskt.
    settlement_type: str = "cfd"
    # Hävstång: 2 = exponering 2x insatsen. Stop loss 3 % i pris = 6 % av insatsen vid x2.
    leverage: int = 2
    # eToros minsta exponering (insats x hävstång) för GLD CFD
    min_exposure_usd: float = 10.0
    # Valutor. Riskbasen = ditt faktiskt avsatta kapital, inte API:ets saldo (10 000, betydelse ej bevisad).
    allocated_capital_usd: float = 1000.0        # = SEK 9 983,59 i appen
    portfolio_display_currency: str = "SEK"
    instrument_currency: str = "USD"
    risk_per_trade: float = 0.0025
    fx_instrument: int = 58                      # USDSEK hos eToro
    fx_max_age_s: int = 300
    # Riktiga ÖPPNINGAR är spärrade tills valutamodellen är verifierad, även med LIVE=yes
    real_open_enabled: bool = False
    # Strategi
    fast_ma: int = 20
    slow_ma: int = 50
    poll_seconds: int = 900
    # Säkerhet: riktig handel kräver LIVE=yes
    live: bool = field(default_factory=lambda: os.getenv("LIVE") == "yes")
    use_ai_filter: bool = field(default_factory=lambda: bool(os.getenv("ANTHROPIC_API_KEY")))
    ai_model: str = "claude-sonnet-5-5"
