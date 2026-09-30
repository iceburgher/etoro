"""Agentens tillstånd. Laddas från databasen i början av varje jobb och sparas i slutet."""
from dataclasses import asdict, dataclass, field


@dataclass
class State:
    day: str = ""
    week: str = ""
    realized_day_usd: float = 0.0
    realized_week_usd: float = 0.0
    opens_today: int = 0
    last_strategy_bar: str = ""        # senast utvärderade 4H-stapel (ISO-start)
    last_exit_bar: str = ""            # stapel då senaste position stängdes
    desired: dict = field(default_factory=lambda: {"side": "FLAT"})
    pending: dict | None = None        # {"kind": "open"|"close", "key", "action", "submitted", ...}
    known_positions: dict = field(default_factory=dict)  # str(pid) -> {side, units, open_rate, stop, target}
    skipped_keys: list = field(default_factory=list)     # öppningar som blockerats/torrkörts för en stapel
    close_attempts: int = 0
    incident: dict | None = None       # ogiltigt läge (t.ex. long + short), blockerar allt utom stängning
    alert_marks: dict = field(default_factory=dict)      # avdubblering av larm: kind -> period
    api_failures: int = 0
    deployed_sha: str = ""

    def to_dict(self) -> dict:
        self.skipped_keys = self.skipped_keys[-200:]
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (d or {}).items() if k in known})
