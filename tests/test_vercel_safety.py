"""Säkerhet i Vercel-körmiljön: överlappande jobb, omförsök, timeouts, databasfel, kallstart, ogiltigt läge.
Inget test får ge en dubbelorder (kontrolleras automatiskt i conftest.py)."""
from agent.state import State
from agent.store import DatabaseError, MemoryStore
from tests.fakes import Clock, FakeBroker, M, S, edit_state, make


def sides(b):
    return sorted("LONG" if p.is_buy else "SHORT" for p in b.positions.values())


def alerts(eng, level=None):
    return [k for lv, k in eng.alert.sent if level is None or lv == level]


def long_signal(s):
    s.reg, s.sig = "LONG", {"LONG": "trigger"}


# ---------- lås ----------
def test_overlapping_broker_monitor_jobs():
    eng, b, s, c, logs = make()
    eng.store.acquire_lock("gld-trading", "annat-jobb", c(), 120)
    b.add_position(True, 375.0, 400.0)
    b.bid, b.ask = 374.0, 374.1
    assert M(eng) == "locked"
    assert not b.closed and not b.opened


def test_overlapping_strategy_and_monitor_jobs_during_order():
    """Ett monitor-jobb startar medan strategijobbet är mitt i ett orderanrop: det måste avstå."""
    clock = Clock()
    b = FakeBroker(clock)
    store = MemoryStore()
    eng1, _, s1, _, _ = make(broker=b, clock=clock, store=store)
    eng2, _, s2, _, logs2 = make(broker=b, clock=clock, store=store)
    long_signal(s1)
    long_signal(s2)
    results = []
    b.on_open = lambda: results.append(M(eng2))
    assert S(eng1) == "filled"
    assert results == ["locked"] and len(b.opened) == 1


def test_expired_lock_is_taken_over():
    eng, b, s, c, logs = make()
    eng.store.acquire_lock("gld-trading", "kraschat-jobb", c(), 120)
    c.minutes(3)                                          # leasen har gått ut
    long_signal(s)
    assert S(eng) != "locked"


def test_lock_acquisition_failure_is_critical_and_does_nothing():
    eng, b, s, c, logs = make()

    def boom(*a):
        raise DatabaseError("connection refused")
    eng.store.acquire_lock = boom
    long_signal(s)
    assert S(eng) == "db_error"
    assert "database_failure" in alerts(eng, "critical") and not b.opened


# ---------- omförsök, timeout, databasfel ----------
def test_retry_after_order_submission():
    """Jobbet dör efter att ordern skickats (sista sparningen misslyckas); Vercel kör om det."""
    eng, b, s, c, logs = make()
    long_signal(s)
    eng.store.fail_saves_after = 2          # 1: pending före order, 2: slutsparningen
    assert S(eng) == "db_error"
    eng.store.fail_saves_after = None
    S(eng)
    M(eng)
    assert len(b.opened) == 1 and sides(b) == ["LONG"]


def test_database_write_failure_after_broker_fill():
    eng, b, s, c, logs = make()
    long_signal(s)
    eng.store.fail_saves_after = 2
    assert S(eng) == "db_error"
    assert "database_failure" in alerts(eng, "critical")
    eng.store.fail_saves_after = None
    for _ in range(3):
        M(eng)
    assert len(b.opened) == 1 and eng.store.load_state().pending is None
    assert "3025" not in str(eng.store.load_state().pending)


def test_database_failure_before_submission_sends_nothing():
    eng, b, s, c, logs = make()
    long_signal(s)
    eng.store.fail_saves_after = 1          # redan sparningen före order misslyckas
    assert S(eng) == "db_error"
    assert not b.opened


def test_timeout_after_order_submission():
    eng, b, s, c, logs = make()
    b.timeout_on_open = True                # ordern når eToro och fylls, men svaret tappas
    long_signal(s)
    assert S(eng) == "unknown"
    assert "unknown_order_state" in alerts(eng, "critical")
    b.timeout_on_open = False
    M(eng)
    M(eng)
    assert len(b.opened) == 1 and sides(b) == ["LONG"] and eng.st.pending is None


def test_order_never_reached_broker_is_marked_lost_after_timeout():
    eng, b, s, c, logs = make()
    b.fill_mode = "pending"
    long_signal(s)
    S(eng)
    b.orders.clear()                        # eToro känner inte till ordern
    c.minutes(11)
    M(eng)
    assert eng.st.pending is None and "unknown_order_state" in alerts(eng, "critical")
    assert len(b.opened) == 1


# ---------- idempotens ----------
def test_duplicate_evaluation_of_same_4h_bar():
    eng, b, s, c, logs = make()
    b.fill_mode = "reject"
    long_signal(s)
    S(eng)
    S(eng)
    S(eng)
    assert len(b.opened) == 1
    assert any(l.get("event") == "bar_already_evaluated" for l in logs)


def test_duplicate_idempotency_key_after_state_loss():
    """Tillståndet försvinner men orderavsikten finns kvar i databasen: samma nyckel skickas inte igen."""
    eng, b, s, c, logs = make()
    b.fill_mode = "pending"
    long_signal(s)
    S(eng)
    eng.store._state = State().to_dict()   # tappat tillstånd, orderavsikter kvar
    S(eng)
    M(eng)
    assert len(b.opened) == 1


# ---------- kallstart / deploy ----------
def _cold_start(is_buy):
    clock = Clock()
    b = FakeBroker(clock)
    pid = b.add_position(is_buy, 370.0 if is_buy else 390.0, 400.0 if is_buy else 360.0)
    side = "LONG" if is_buy else "SHORT"
    st = State(desired={"side": side, "signal_bar": "2026-09-29T20:00:00Z"},
               known_positions={str(pid): {"side": side, "units": 0.3, "open_rate": 380.0,
                                           "stop": 370.0 if is_buy else 390.0, "target": 400.0 if is_buy else 360.0}})
    eng, b, s, c, logs = make(broker=b, clock=clock, state=st)
    return eng, b, s, c, pid


def test_cold_start_with_open_long():
    eng, b, s, c, pid = _cold_start(True)
    long_signal(s)
    S(eng)
    assert not b.opened and not b.closed and not alerts(eng, "critical")
    c.next_bar()
    s.exit = "daily_regime_not_long"
    S(eng)
    assert b.closed == [pid]


def test_cold_start_with_open_short():
    eng, b, s, c, pid = _cold_start(False)
    s.reg, s.sig = "SHORT", {"SHORT": "trigger"}
    S(eng)
    assert not b.opened and not b.closed and not alerts(eng, "critical")
    c.next_bar()
    s.exit = "4h_close_above_ma50"
    S(eng)
    assert b.closed == [pid]


def test_cold_start_with_empty_database_and_open_position_adopts_and_alerts():
    clock = Clock()
    b = FakeBroker(clock)
    b.add_position(True, 370.0, 400.0)
    eng, b, s, c, logs = make(broker=b, clock=clock)
    long_signal(s)
    S(eng)
    assert not b.opened and not b.closed and "unexpected_broker_position" in alerts(eng, "critical")


def test_database_broker_mismatch():
    st = State(known_positions={"55": {"side": "SHORT", "units": 0.3, "open_rate": 380.0, "stop": 390, "target": 360}})
    eng, b, s, c, logs = make(state=st)
    M(eng)
    assert "reconciliation_mismatch" in alerts(eng, "critical")
    assert eng.store.load_state().known_positions == {}


# ---------- data och spärrar med öppen position ----------
def test_stale_usdsek_blocks_open_and_alerts():
    eng, b, s, c, logs = make()
    b.fx_age_s = 600
    long_signal(s)
    assert S(eng) == "blocked" and not b.opened
    assert "stale_fx" in alerts(eng, "critical")


def test_kill_switch_while_position_is_open_still_closes():
    eng, b, s, c, pid = _cold_start(True)
    eng.store.set_control(kill_switch=True)
    c.next_bar()
    s.exit = "daily_regime_not_long"
    S(eng)
    assert b.closed == [pid] and "kill_switch_active" in alerts(eng, "critical")


def test_daily_loss_limit_while_position_is_open_still_closes():
    eng, b, s, c, pid = _cold_start(True)
    edit_state(eng, day="2026-09-30", week="2026-W40", realized_day_usd=-20.0, realized_week_usd=-20.0)
    b.bid, b.ask = 369.0, 369.1                          # stop passerad
    M(eng)
    assert b.closed == [pid] and "daily_loss_limit" in alerts(eng, "critical")


def test_repeated_api_failure_alerts():
    eng, b, s, c, logs = make()
    b.snapshot_fails = True
    for _ in range(3):
        assert M(eng) == "no_snapshot"
    assert "repeated_api_failure" in alerts(eng, "critical") and not b.opened


# ---------- ogiltigt läge ----------
def test_long_and_short_invalid_state_closes_both_then_resumes():
    eng, b, s, c, logs = make()
    lp = b.add_position(True, 370, 400)
    sp = b.add_position(False, 390, 360)
    long_signal(s)
    M(eng)
    assert "long_and_short" in alerts(eng, "critical") and eng.store.incidents
    assert b.closed == [lp] and not b.opened                     # long först, ingen riktning väljs
    S(eng)
    assert b.closed == [lp, sp] and sides(b) == [] and not b.opened
    assert "incident_resolved" in alerts(eng, "critical")
    assert eng.st.incident is None and all(i["resolved"] for i in eng.store.incidents)
    c.next_bar()
    long_signal(s)
    assert S(eng) == "filled"                                    # återupptas först efter avstämning


# ---------- miljö ----------
def test_real_micro_attempted_in_preview_runs_as_dry_run():
    eng, b, s, c, logs = make(mode="REAL_MICRO", vercel_env="preview")
    long_signal(s)
    assert S(eng) == "dry_run"
    assert not b.opened and "real_micro_outside_production" in alerts(eng, "critical")


def test_real_micro_locally_runs_as_dry_run():
    eng, b, s, c, logs = make(mode="REAL_MICRO", vercel_env="")
    long_signal(s)
    assert S(eng) == "dry_run" and not b.opened


def test_git_sha_stored_in_state_and_intents():
    eng, b, s, c, logs = make()
    long_signal(s)
    S(eng)
    assert eng.store.load_state().deployed_sha == "testsha"
    assert all(i["git_sha"] == "testsha" for i in eng.store.intents.values())
