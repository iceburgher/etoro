"""Tillstånd som överlever omstart. Skrivs atomiskt (tmp-fil + rename)."""
import json
import os
from dataclasses import asdict, dataclass, field


@dataclass
class State:
    day: str = ""
    week: str = ""
    realized_day_usd: float = 0.0
    realized_week_usd: float = 0.0
    opens_today: int = 0
    last_strategy_bar: str = ""       # senast utvärderade 4H-stapel (ISO-start)
    last_exit_bar: str = ""           # stapel då senaste position stängdes
    desired: dict = field(default_factory=lambda: {"side": "FLAT"})
    pending: dict | None = None       # {"kind": "open"|"close", "key", "action", "submitted", ...}
    known_positions: dict = field(default_factory=dict)  # str(pid) -> {side, units, open_rate, stop, target}
    executed_keys: list = field(default_factory=list)
    close_attempts: int = 0


class StateStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> State:
        if not os.path.exists(self.path):
            return State()
        with open(self.path) as f:
            d = json.load(f)
        known = set(State.__dataclass_fields__)
        return State(**{k: v for k, v in d.items() if k in known})

    def save(self, st: State) -> None:
        st.executed_keys = st.executed_keys[-500:]
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(st), f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


class MemoryStore(StateStore):
    """För tester."""
    def __init__(self, st: State | None = None):
        self.st = st or State()
        self.saves = 0

    def load(self) -> State:
        return self.st

    def save(self, st: State) -> None:
        self.st = st
        self.saves += 1
