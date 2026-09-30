"""Exekvering: utför godkända TradeActions och verifierar dem mot eToro. Räknar aldrig ett anrop som en fyllning."""
import uuid
from datetime import datetime

from .config import Config
from .models import CLOSES, OPENS, Action, TradeAction
from .state import State, StateStore

EXECUTABLE = OPENS | CLOSES


class ExecutionEngine:
    def __init__(self, cfg: Config, broker, store: StateStore, log, sleep):
        self.cfg, self.broker, self.store, self.log, self.sleep = cfg, broker, store, log, sleep

    def execute(self, ta: TradeAction, st: State, now: datetime) -> str:
        if not isinstance(ta.action, Action) or ta.action not in EXECUTABLE:
            self.log(event="rejected_unknown_action", action=str(ta.action))
            raise ValueError(f"okänd åtgärd: {ta.action}")
        if not self.cfg.real:
            self.log(event="DRY_RUN", **ta.log_dict())
            return "dry_run"
        if ta.action in OPENS:
            return self._open(ta, st, now)
        return self._close(ta, st, now)

    def _open(self, ta: TradeAction, st: State, now: datetime) -> str:
        key = ta.idempotency_key
        if key in st.executed_keys:
            self.log(event="duplicate_open_skipped", key=key)
            return "duplicate"
        # Har ordern redan skickats (t.ex. före en krasch)? Då skickas den inte igen.
        if self.broker.lookup(key) is None:
            st.pending = {"kind": "open", "key": key, "action": ta.action.value, "submitted": now.isoformat(),
                          "signal_bar": ta.signal_bar, "stop": ta.proposed_stop, "target": ta.proposed_target}
            st.executed_keys.append(key)
            self.store.save(st)  # spara INNAN anropet, så att en krasch inte ger dubbelorder
            res = self.broker.open_order(ta.instrument, ta.action == Action.OPEN_LONG, ta.position_size,
                                         ta.leverage, ta.proposed_stop, ta.proposed_target, request_id=key)
            self.log(event="order_submitted", key=key, response=res, **ta.log_dict())
        else:
            st.pending = st.pending or {"kind": "open", "key": key, "action": ta.action.value,
                                        "submitted": now.isoformat(), "signal_bar": ta.signal_bar}
            if key not in st.executed_keys:
                st.executed_keys.append(key)
        for _ in range(self.cfg.order_poll_tries):
            s = self.broker.lookup(key)
            if s and s.filled:
                st.pending = None
                st.opens_today += 1
                self.log(event="order_filled", key=key, position_ids=list(s.position_ids))
                return "filled"
            if s and s.failed:
                st.pending = None
                self.log(event="order_rejected", key=key, status=s.status_id, error=s.error)
                return "rejected"
            self.sleep(self.cfg.order_poll_sleep_s)
        self.log(event="order_pending", key=key)
        return "pending"

    def _close(self, ta: TradeAction, st: State, now: datetime) -> str:
        st.pending = {"kind": "close", "key": ta.idempotency_key, "action": ta.action.value,
                      "submitted": now.isoformat(), "position_ids": list(ta.position_ids),
                      "exit_reason": ta.exit_reason}
        self.store.save(st)
        for pid in ta.position_ids:
            res = self.broker.close_position(pid, ta.instrument, request_id=str(uuid.uuid4()))
            self.log(event="close_submitted", position_id=pid, response=res, **ta.log_dict())
        for _ in range(self.cfg.order_poll_tries):
            open_ids = {p.position_id for p in self.broker.snapshot().positions}
            if not set(ta.position_ids) & open_ids:
                self.log(event="close_verified", position_ids=list(ta.position_ids))
                return "closed"  # engine stämmer av och rensar pending
            self.sleep(self.cfg.order_poll_sleep_s)
        self.log(event="close_pending", position_ids=list(ta.position_ids))
        return "pending"
