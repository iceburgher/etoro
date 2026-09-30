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
    # Tak på insats per affär. 40 USD x2 = 80 USD exponering; stop loss -3 % = ca 2,40 USD = ca 0,25 % av kontot
    max_trade_usd: float = 40.0
    # Agentportföljen har ~1000 USD, men API:et rapporterar credit 10000. Tills det är utrett räknas
    # kontot som högst så här stort, så att alla %-regler blir rätt.
    equity_cap_usd: float = 1000.0
    # Strategi
    fast_ma: int = 20
    slow_ma: int = 50
    poll_seconds: int = 900
    # Säkerhet: riktig handel kräver LIVE=yes
    live: bool = field(default_factory=lambda: os.getenv("LIVE") == "yes")
    use_ai_filter: bool = field(default_factory=lambda: bool(os.getenv("ANTHROPIC_API_KEY")))
    ai_model: str = "claude-sonnet-5-5"
