"""Strikta datatyper. Allt som flyttar pengar går genom TradeAction."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Action(str, Enum):
    OPEN_LONG = "OPEN_LONG"
    CLOSE_LONG = "CLOSE_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_SHORT = "CLOSE_SHORT"
    NO_ACTION = "NO_ACTION"


class Desired(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class Actual(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"
    PENDING = "PENDING"


OPENS = {Action.OPEN_LONG, Action.OPEN_SHORT}
CLOSES = {Action.CLOSE_LONG, Action.CLOSE_SHORT}

# Lägre siffra = högre prioritet
PRIO_EMERGENCY, PRIO_CLOSE, PRIO_RECONCILE, PRIO_OPEN, PRIO_NONE = 1, 2, 3, 4, 5


@dataclass(frozen=True)
class TradeAction:
    action: Action
    instrument: int
    current_position: Actual
    desired_position: Desired
    signal_timestamp: str
    signal_bar: str
    strategy_version: str
    idempotency_key: str
    entry_reason: str | None = None
    exit_reason: str | None = None
    proposed_entry: float | None = None
    proposed_stop: float | None = None
    proposed_target: float | None = None
    risk_budget: float | None = None      # SEK
    position_size: float | None = None    # units
    leverage: int | None = None
    position_ids: tuple = ()
    priority: int = PRIO_NONE
    sizing: dict | None = None            # alla valutafält från sizing.Sizing

    def __post_init__(self):
        # Action("X") kastar ValueError för okända actions
        object.__setattr__(self, "action", Action(self.action))
        object.__setattr__(self, "current_position", Actual(self.current_position))
        object.__setattr__(self, "desired_position", Desired(self.desired_position))
        if self.action in OPENS:
            for f in ("proposed_entry", "proposed_stop", "proposed_target", "risk_budget", "position_size", "leverage"):
                if getattr(self, f) is None:
                    raise ValueError(f"{self.action.value} saknar {f}")
        if self.action in CLOSES and not self.position_ids:
            raise ValueError(f"{self.action.value} saknar position_ids")

    def log_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        for k in ("action", "current_position", "desired_position"):
            d[k] = d[k].value
        d["position_ids"] = list(self.position_ids)
        return d


@dataclass(frozen=True)
class Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    time: datetime
    realtime: bool

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class BrokerPosition:
    position_id: int
    instrument_id: int
    is_buy: bool
    units: float
    amount: float
    open_rate: float
    stop_loss: float | None
    take_profit: float | None
    leverage: int
    has_stop: bool

    @property
    def side(self) -> Actual:
        return Actual.LONG if self.is_buy else Actual.SHORT


@dataclass(frozen=True)
class Snapshot:
    currency_id: int | None
    api_reported_value: float           # eToros 10 000-värde: loggas, används aldrig för risk
    positions: tuple = ()
    pending_instruments: frozenset = frozenset()  # instrument med väntande order hos eToro

    def for_instrument(self, iid: int) -> list[BrokerPosition]:
        return [p for p in self.positions if p.instrument_id == iid]


@dataclass(frozen=True)
class Eligibility:
    symbol: str
    min_exposure: float
    max_units: float
    # (direction "long"/"short", leverage) -> (max_sl_pct_of_margin, min_amount)
    configs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OrderStatus:
    status_id: int
    error: str | None
    position_ids: tuple = ()

    @property
    def filled(self) -> bool:
        return self.status_id in (3, 5)

    @property
    def failed(self) -> bool:
        return self.status_id in (4, 6, 7, 8, 9, 10)
