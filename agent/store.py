"""Varaktig lagring: tillstånd, lås, orderavsikter, händelser, incidenter, styrning.

PgStore = produktion (Supabase/PostgreSQL). MemoryStore = tester och lokal torrkörning, samma semantik.
"""
import copy
import json
from datetime import datetime, timedelta

from .state import State

FINAL_INTENT = {"filled", "rejected", "lost", "closed"}
# Öppningar som kan ha nått eToro (räknas konservativt för integrationsspärren)
SENT_OPEN = {"submitted", "filled", "unknown"}


class DatabaseError(Exception):
    pass


class MemoryStore:
    def __init__(self, state: State | None = None):
        self._state = (state or State()).to_dict()
        self.locks: dict[str, tuple[str, datetime]] = {}
        self.intents: dict[str, dict] = {}
        self.events: list[dict] = []
        self.incidents: list[dict] = []
        self.ctrl = {"kill_switch": False, "halt_new_entries": False}
        self.fail_saves_after: int | None = None   # testkrok: kasta DatabaseError från och med N:e sparningen
        self.saves = 0

    def load_state(self) -> State:
        return State.from_dict(copy.deepcopy(self._state))

    def save_state(self, st: State, git_sha: str = "") -> None:
        self.saves += 1
        if self.fail_saves_after is not None and self.saves >= self.fail_saves_after:
            raise DatabaseError("simulerat databasfel")
        self._state = copy.deepcopy(st.to_dict())

    def acquire_lock(self, name: str, holder: str, now: datetime, ttl_s: int) -> bool:
        cur = self.locks.get(name)
        if cur and cur[1] > now and cur[0] != holder:
            return False
        self.locks[name] = (holder, now + timedelta(seconds=ttl_s))
        return True

    def release_lock(self, name: str, holder: str) -> None:
        if self.locks.get(name, ("",))[0] == holder:
            del self.locks[name]

    def create_order_intent(self, key: str, action: str, instrument: int, payload: dict, git_sha: str) -> bool:
        if key in self.intents:
            return False
        self.intents[key] = {"action": action, "instrument": instrument, "status": "created",
                             "payload": payload, "git_sha": git_sha, "broker_order_id": None, "error": None}
        return True

    def update_order_intent(self, key: str, **fields) -> None:
        self.intents[key].update(fields)

    def get_order_intent(self, key: str) -> dict | None:
        return self.intents.get(key)

    def count_sent_opens(self) -> int:
        return sum(1 for i in self.intents.values()
                   if i["action"].startswith("OPEN") and i["status"] in SENT_OPEN)

    def log_event(self, job: str, event: str, payload: dict, git_sha: str) -> None:
        self.events.append({"job": job, "event": event, "payload": payload, "git_sha": git_sha})

    def open_incident(self, kind: str, details: dict) -> None:
        self.incidents.append({"kind": kind, "details": details, "resolved": False})

    def resolve_incidents(self, kind: str) -> None:
        for i in self.incidents:
            if i["kind"] == kind:
                i["resolved"] = True

    def control(self) -> dict:
        return dict(self.ctrl)

    def set_control(self, **kw) -> None:
        self.ctrl.update(kw)


class PgStore:
    """PostgreSQL via psycopg 3. Varje metod är en egen transaktion (fungerar bakom Supabase-poolern)."""

    def __init__(self, dsn: str):
        import psycopg
        self._psycopg = psycopg
        self.dsn = dsn

    def _run(self, sql: str, params=(), fetch: str | None = None):
        try:
            with self._psycopg.connect(self.dsn, autocommit=False, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    out = cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
                conn.commit()
                return out
        except self._psycopg.Error as e:
            raise DatabaseError(str(e)) from e

    def load_state(self) -> State:
        row = self._run("select state from agent_state where id = 1", fetch="one")
        return State.from_dict(row[0]) if row else State()

    def save_state(self, st: State, git_sha: str = "") -> None:
        self._run("""insert into agent_state (id, state, git_sha, updated_at) values (1, %s, %s, now())
                     on conflict (id) do update set state = excluded.state, git_sha = excluded.git_sha,
                     updated_at = now()""", (json.dumps(st.to_dict()), git_sha))

    def acquire_lock(self, name: str, holder: str, now: datetime, ttl_s: int) -> bool:
        # Tar låset om det är ledigt eller har gått ut. Atomiskt i en sats; två jobb kan aldrig båda få det.
        row = self._run("""insert into job_locks (name, holder, acquired_at, expires_at)
                           values (%s, %s, %s, %s)
                           on conflict (name) do update
                             set holder = excluded.holder, acquired_at = excluded.acquired_at,
                                 expires_at = excluded.expires_at
                             where job_locks.expires_at < excluded.acquired_at or job_locks.holder = excluded.holder
                           returning holder""",
                        (name, holder, now, now + timedelta(seconds=ttl_s)), fetch="one")
        return bool(row and row[0] == holder)

    def release_lock(self, name: str, holder: str) -> None:
        self._run("delete from job_locks where name = %s and holder = %s", (name, holder))

    def create_order_intent(self, key: str, action: str, instrument: int, payload: dict, git_sha: str) -> bool:
        row = self._run("""insert into order_intents (idempotency_key, action, instrument, status, payload, git_sha)
                           values (%s, %s, %s, 'created', %s, %s)
                           on conflict (idempotency_key) do nothing returning idempotency_key""",
                        (key, action, instrument, json.dumps(payload, default=str), git_sha), fetch="one")
        return row is not None

    def update_order_intent(self, key: str, **fields) -> None:
        cols = [c for c in ("status", "broker_order_id", "error") if c in fields]
        if not cols:
            return
        sets = ", ".join(f"{c} = %s" for c in cols)
        self._run(f"update order_intents set {sets}, updated_at = now() where idempotency_key = %s",
                  tuple(fields[c] for c in cols) + (key,))

    def get_order_intent(self, key: str) -> dict | None:
        row = self._run("""select action, instrument, status, payload, broker_order_id, error, git_sha
                           from order_intents where idempotency_key = %s""", (key,), fetch="one")
        if not row:
            return None
        return dict(zip(("action", "instrument", "status", "payload", "broker_order_id", "error", "git_sha"), row))

    def count_sent_opens(self) -> int:
        row = self._run("""select count(*) from order_intents
                           where action like 'OPEN%%' and status = any(%s)""", (list(SENT_OPEN),), fetch="one")
        return int(row[0])

    def log_event(self, job: str, event: str, payload: dict, git_sha: str) -> None:
        self._run("insert into events (job, event, payload, git_sha) values (%s, %s, %s, %s)",
                  (job, event, json.dumps(payload, default=str), git_sha))

    def open_incident(self, kind: str, details: dict) -> None:
        self._run("insert into incidents (kind, details) values (%s, %s)", (kind, json.dumps(details, default=str)))

    def resolve_incidents(self, kind: str) -> None:
        self._run("update incidents set resolved_at = now() where kind = %s and resolved_at is null", (kind,))

    def control(self) -> dict:
        row = self._run("select kill_switch, halt_new_entries from control where id = 1", fetch="one")
        return {"kill_switch": bool(row and row[0]), "halt_new_entries": bool(row and row[1])}

    def set_control(self, **kw) -> None:
        cols = [c for c in ("kill_switch", "halt_new_entries") if c in kw]
        sets = ", ".join(f"{c} = %s" for c in cols)
        self._run(f"update control set {sets}, updated_at = now() where id = 1", tuple(kw[c] for c in cols))
