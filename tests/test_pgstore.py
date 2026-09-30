"""PgStore mot riktig PostgreSQL. Körs bara om TEST_DATABASE_URL är satt (tabellerna töms)."""
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from agent.state import State
from agent.store import PgStore
from tests.fakes import Clock, FakeBroker, M, S, make

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL saknas")
NOW = datetime(2026, 9, 30, 12, 5, tzinfo=timezone.utc)


@pytest.fixture
def pg():
    import psycopg
    here = os.path.dirname(__file__)
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(open(os.path.join(here, "..", "sql", "schema.sql")).read())
        c.execute("truncate agent_state, job_locks, order_intents, events, incidents")
        c.execute("update control set kill_switch = false, halt_new_entries = false")
    return PgStore(DSN)


def test_lock_is_exclusive_and_expires(pg):
    assert pg.acquire_lock("gld", "a", NOW, 120)
    assert not pg.acquire_lock("gld", "b", NOW + timedelta(seconds=10), 120)
    assert pg.acquire_lock("gld", "b", NOW + timedelta(seconds=121), 120)   # leasen gått ut
    pg.release_lock("gld", "a")                                              # fel ägare: inget händer
    assert not pg.acquire_lock("gld", "c", NOW + timedelta(seconds=130), 120)
    pg.release_lock("gld", "b")
    assert pg.acquire_lock("gld", "c", NOW + timedelta(seconds=130), 120)


def test_lock_under_concurrency_exactly_one_winner(pg):
    wins, barrier = [], threading.Barrier(12)

    def go(i):
        barrier.wait()
        if pg.acquire_lock("race", f"h{i}", NOW, 120):
            wins.append(i)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1


def test_order_intent_is_unique_under_concurrency(pg):
    created, barrier = [], threading.Barrier(10)

    def go(i):
        barrier.wait()
        if pg.create_order_intent("k-1", "OPEN_LONG", 3025, {"i": i}, "sha"):
            created.append(i)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(created) == 1
    pg.update_order_intent("k-1", status="filled", broker_order_id=42)
    assert pg.get_order_intent("k-1")["status"] == "filled"


def test_state_control_events_incidents_roundtrip(pg):
    st = State(desired={"side": "LONG", "swing": 370.0}, opens_today=1, deployed_sha="abc")
    pg.save_state(st, "abc")
    assert pg.load_state().desired["side"] == "LONG" and pg.load_state().deployed_sha == "abc"
    pg.set_control(kill_switch=True)
    assert pg.control() == {"kill_switch": True, "halt_new_entries": False}
    pg.log_event("monitor", "cycle", {"x": 1}, "abc")
    pg.open_incident("long_and_short", {"p": [1, 2]})
    pg.resolve_incidents("long_and_short")


def test_engine_end_to_end_on_postgres(pg):
    clock = Clock()
    b = FakeBroker(clock)
    eng, b, s, c, logs = make(broker=b, clock=clock, store=pg)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "filled"
    assert M(eng) == "no_action"
    c.next_bar()
    s.exit = "daily_regime_not_long"
    assert S(eng) == "closed"
    assert len(b.opened) == 1 and b.closed and pg.load_state().pending is None
    assert pg.load_state().deployed_sha == "testsha"
