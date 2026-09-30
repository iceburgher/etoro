"""Exekvering: utför godkända TradeActions och verifierar dem mot eToro.

Idempotens: varje order får en nyckel som (1) skrivs som orderavsikt i databasen INNAN ordern skickas,
(2) skickas som eToros x-request-id. Finns avsikten redan skickas ingen ny order, bara statusfrågor.
Ett lyckat anrop räknas aldrig som en fyllning.
"""
from datetime import datetime

from .alerts import CRITICAL, NORMAL
from .config import Config
from .models import CLOSES, OPENS, Action, TradeAction
from .state import State
from .store import FINAL_INTENT


class ExecutionEngine:
    def __init__(self, cfg: Config, broker, store, log, sleep, alert):
        self.cfg, self.broker, self.store, self.log, self.sleep, self.alert = cfg, broker, store, log, sleep, alert

    def execute(self, ta: TradeAction, st: State, now: datetime) -> str:
        if not isinstance(ta.action, Action) or ta.action not in (OPENS | CLOSES):
            self.log(event="rejected_unknown_action", action=str(getattr(ta, "action", None)))
            raise ValueError(f"okänd åtgärd: {getattr(ta, 'action', None)}")
        if not self.cfg.real:
            self.log(event="DRY_RUN", **ta.log_dict())
            return "dry_run"
        return self._open(ta, st, now) if ta.action in OPENS else self._close(ta, st, now)

    # ---------- öppning ----------
    def _open(self, ta: TradeAction, st: State, now: datetime) -> str:
        key = ta.idempotency_key
        intent = self.store.get_order_intent(key)
        if intent and intent["status"] in FINAL_INTENT:
            self.log(event="duplicate_open_skipped", key=key, status=intent["status"])
            return "duplicate"
        if intent is None:
            if not self.store.create_order_intent(key, ta.action.value, ta.instrument, ta.log_dict(), self.cfg.git_sha):
                self.log(event="duplicate_open_skipped", key=key, status="race")
                return "duplicate"
            st.pending = {"kind": "open", "key": key, "action": ta.action.value, "submitted": now.isoformat(),
                          "signal_bar": ta.signal_bar}
            self.store.save_state(st, self.cfg.git_sha)  # varaktigt INNAN anropet; fel här = ingen order skickas
            try:
                res = self.broker.open_order(ta.instrument, ta.action == Action.OPEN_LONG, ta.position_size,
                                             ta.leverage, ta.proposed_stop, ta.proposed_target, request_id=key)
            except Exception as e:  # timeout m.m.: ordern kan ha nått eToro. Avstämning via lookup avgör.
                self._safe_intent(key, status="unknown", error=str(e))
                self.alert(CRITICAL, "unknown_order_state", "Öppningsorder utan svar; stäms av via request-id",
                           key=key, error=str(e))
                return "unknown"
            self._safe_intent(key, status="submitted", broker_order_id=res.get("orderId"))
            self.log(event="order_submitted", key=key, response=res, **ta.log_dict())
        else:
            st.pending = st.pending or {"kind": "open", "key": key, "action": ta.action.value,
                                        "submitted": now.isoformat(), "signal_bar": ta.signal_bar}
        return self.poll_open(key, st, ta)

    def poll_open(self, key: str, st: State, ta: TradeAction | None = None) -> str:
        for _ in range(self.cfg.order_poll_tries):
            try:
                s = self.broker.lookup(key)
            except Exception as e:
                self.log(event="lookup_failed", key=key, error=str(e))
                s = None
            if s and s.filled:
                self.remember_fill(st, s.position_ids, ta)
                st.pending = None
                st.opens_today += 1
                self._safe_intent(key, status="filled")
                self.log(event="order_filled", key=key, position_ids=list(s.position_ids))
                if ta:
                    self.alert(NORMAL, ta.action.value, f"{ta.action.value} fylld", key=key,
                               position_ids=list(s.position_ids), units=ta.position_size,
                               stop=ta.proposed_stop, target=ta.proposed_target, sizing=ta.sizing)
                return "filled"
            if s and s.failed:
                st.pending = None
                self._safe_intent(key, status="rejected", error=s.error)
                self.log(event="order_rejected", key=key, status=s.status_id, error=s.error)
                return "rejected"
            self.sleep(self.cfg.order_poll_sleep_s)
        self.log(event="order_pending", key=key)
        return "pending"

    @staticmethod
    def remember_fill(st: State, position_ids, ta: TradeAction | None):
        """Markera egna nya positioner som kända, så att avstämningen inte larmar om 'okänd position'."""
        action = (st.pending or {}).get("action") or (ta.action.value if ta else "")
        side = "LONG" if "LONG" in action else "SHORT"
        for pid in position_ids:
            st.known_positions.setdefault(str(pid), {
                "side": side, "units": ta.position_size if ta else 0.0, "open_rate": ta.proposed_entry if ta else 0.0,
                "stop": ta.proposed_stop if ta else None, "target": ta.proposed_target if ta else None})

    # ---------- stängning ----------
    def _close(self, ta: TradeAction, st: State, now: datetime) -> str:
        key = ta.idempotency_key
        intent = self.store.get_order_intent(key)
        if intent is None:
            self.store.create_order_intent(key, ta.action.value, ta.instrument, ta.log_dict(), self.cfg.git_sha)
        st.pending = {"kind": "close", "key": key, "action": ta.action.value, "submitted": now.isoformat(),
                      "position_ids": list(ta.position_ids), "exit_reason": ta.exit_reason}
        self.store.save_state(st, self.cfg.git_sha)
        if intent is None:
            for i, pid in enumerate(ta.position_ids):
                try:
                    res = self.broker.close_position(pid, ta.instrument, request_id=f"{key[:-4]}{i:04d}")
                    self.log(event="close_submitted", position_id=pid, response=res, **ta.log_dict())
                except Exception as e:
                    self.log(event="close_submit_failed", position_id=pid, error=str(e))
                    self.alert(CRITICAL, "unknown_order_state", "Stängningsorder utan svar", position_id=pid,
                               error=str(e))
            self._safe_intent(key, status="submitted")
        for _ in range(self.cfg.order_poll_tries):
            try:
                open_ids = {p.position_id for p in self.broker.snapshot().positions}
            except Exception as e:
                self.log(event="snapshot_failed", error=str(e))
                open_ids = set(ta.position_ids)
            if not set(ta.position_ids) & open_ids:
                self.log(event="close_verified", position_ids=list(ta.position_ids))
                return "closed"  # motorn stämmer av, rensar pending och larmar
            self.sleep(self.cfg.order_poll_sleep_s)
        self.log(event="close_pending", position_ids=list(ta.position_ids))
        return "pending"

    def _safe_intent(self, key: str, **fields):
        try:
            self.store.update_order_intent(key, **fields)
        except Exception as e:  # avsikten finns redan; statusen rättas vid avstämning
            self.log(event="intent_update_failed", key=key, error=str(e))
