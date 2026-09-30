import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    base_url: str = "https://public-api.etoro.com"
    # Bara dessa instrument får handlas. Allt annat blockeras.
    allowed_instruments: tuple = (18,)  # 18 = GOLD (CFD, den du handlar i appen). 3025 = GLD (guld-ETF)
    # Risk (i USD / procent)
    max_per_trade_pct: float = 0.20      # max andel av kontot per trade
    max_total_exposure_pct: float = 0.40  # max andel av kontot investerat totalt
    stop_loss_pct: float = 0.03          # obligatorisk stop loss under inköpspris
    take_profit_pct: float = 0.06
    daily_loss_halt_pct: float = 0.03    # pausa resten av dagen vid -3 %
    max_orders_per_day: int = 4
    # "real" = äg riktiga andelar. "cfd" = derivat med nattavgifter. Väljs medvetet, aldrig automatiskt.
    settlement_type: str = "cfd"
    # Hävstång: 5 = exponering 5x insatsen. Stop loss 3 % i pris = 15 % av insatsen vid x5.
    leverage: int = 5
    # Hårt tak på insats per affär i USD, oavsett vad kontosaldot säger (kontot är ~10 000 SEK ≈ 1 000 USD)
    max_trade_usd: float = 200.0
    # Strategi
    fast_ma: int = 20
    slow_ma: int = 50
    poll_seconds: int = 900
    # Säkerhet: riktig handel kräver LIVE=yes
    live: bool = field(default_factory=lambda: os.getenv("LIVE") == "yes")
    use_ai_filter: bool = field(default_factory=lambda: bool(os.getenv("ANTHROPIC_API_KEY")))
    ai_model: str = "claude-sonnet-5-5"
