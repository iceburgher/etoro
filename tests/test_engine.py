from dataclasses import replace

import pytest

from agent.execution import ExecutionEngine
from agent.models import Action, Actual
from agent.state import State
from tests.fakes import Clock, FakeBroker, make


def sides(b):
    return sorted("LONG" if p.is_buy else "SHORT" for p in b.positions.values())


def events(logs, name):
    return [l for l in logs if l.get("event") == name]


def open_long(eng, b, s):
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert eng.cycle() == "filled"
    assert sides(b) == ["LONG"]


# ---------- grundövergångar ----------
def test_flat_to_long():
    eng, b, s, c, logs = make()
    open_long(eng, b, s)
    o = b.opened[0]
    assert o["is_buy"] and o["stop"] < 380.1 < o["target"]
    assert eng.st.opens_today == 1


def test_long_to_flat_on_strategy_exit():
    eng, b, s, c, logs = make()
    open_long(eng, b, s)
    c.next_bar()
    s.reg, s.sig, s.exit = None, {}, "daily_regime_not_long"
    assert eng.cycle() == "closed"
    assert sides(b) == [] and eng.st.last_exit_bar and eng.st.pending is None


def test_flat_to_short_and_back_to_flat():
    eng, b, s, c, logs = make()
    s.reg, s.sig = "SHORT", {"SHORT": "trigger"}
    assert eng.cycle() == "filled"
    assert sides(b) == ["SHORT"] and not b.opened[0]["is_buy"]
    assert b.opened[0]["stop"] > 380.0 > b.opened[0]["target"]
    c.next_bar()
    s.exit = "4h_close_above_ma50"
    assert eng.cycle() == "closed"
    assert sides(b) == []


# ---------- vändningar ----------
def test_long_to_short_reversal_waits_one_bar():
    eng, b, s, c, logs = make()
    open_long(eng, b, s)
    c.next_bar()
    s.reg, s.sig, s.exit = "SHORT", {"SHORT": "trigger"}, "daily_regime_not_long"
    assert eng.cycle() == "closed"
    assert sides(b) == [] and len(b.opened) == 1       # ingen short i samma varv
    eng.cycle()
    assert len(b.opened) == 1                           # inte heller på samma stapel
    c.next_bar()
    s.exit = None
    assert eng.cycle() == "filled"                      # nästa stapel, signalen finns kvar
    assert sides(b) == ["SHORT"]


def test_short_to_long_reversal_needs_signal_on_next_bar():
    eng, b, s, c, logs = make()
    s.reg, s.sig = "SHORT", {"SHORT": "trigger"}
    eng.cycle()
    c.next_bar()
    s.reg, s.sig, s.exit = "LONG", {"LONG": "trigger"}, "daily_regime_not_short"
    assert eng.cycle() == "closed"
    c.next_bar()
    s.sig, s.exit = {}, None                            # signalen borta på nästa stapel
    eng.cycle()
    assert sides(b) == [] and len(b.opened) == 1


def test_never_long_and_short_at_once_conflict_is_emergency_closed():
    eng, b, s, c, logs = make()
    b.add_position(True, 370, 400)
    b.add_position(False, 390, 360)
    eng.cycle()
    assert b.closed and sides(b) == ["SHORT"]           # long stängs som nödläge
    eng.cycle()
    assert sides(b) == ["SHORT"] and not b.opened       # short tas över, inget öppnas


# ---------- dubbletter och väntande ----------
def test_no_duplicate_open_when_order_pending():
    eng, b, s, c, logs = make()
    b.fill_mode = "pending"
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert eng.cycle() == "pending"
    for _ in range(3):
        eng.cycle()
    assert len(b.opened) == 1
    assert eng.st.pending and eng.st.pending["kind"] == "open"


def test_pending_open_fills_later_via_reconciliation():
    eng, b, s, c, logs = make()
    b.fill_mode = "pending"
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    eng.cycle()
    key = b.opened[0]["key"]
    pid = b.add_position(True, 369.25, 401.8)
    b.orders[key] = type(b.orders[key])(3, None, (pid,))
    eng.cycle()
    assert eng.st.pending is None and events(logs, "reconciled_fill") and eng.st.opens_today == 1


def test_no_duplicate_close():
    eng, b, s, c, logs = make()
    open_long(eng, b, s)
    b.close_mode = "stuck"
    c.next_bar()
    s.exit = "4h_close_below_ma50"
    assert eng.cycle() == "pending"
    for _ in range(3):
        eng.cycle()
    assert len(b.closed) == 1


def test_broker_pending_order_means_no_action():
    eng, b, s, c, logs = make()
    b.pending_instr = {3025}
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    eng.cycle()
    assert not b.opened


def test_rejected_open_is_not_retried_same_bar():
    eng, b, s, c, logs = make()
    b.fill_mode = "reject"
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert eng.cycle() == "rejected"
    eng.cycle()
    assert len(b.opened) == 1


# ---------- omstart och avstämning ----------
def test_restart_with_open_long():
    clock = Clock()
    b = FakeBroker(clock)
    pid = b.add_position(True, 370, 400)
    eng, b, s, c, logs = make(state=State(), broker=b, clock=clock)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    eng.cycle()
    assert events(logs, "adopted_position") and not b.opened   # ingen extra long
    c.next_bar()
    s.exit = "daily_regime_not_long"
    eng.cycle()
    assert b.closed == [pid]


def test_restart_with_open_short():
    clock = Clock()
    b = FakeBroker(clock)
    pid = b.add_position(False, 390, 360)
    eng, b, s, c, logs = make(state=State(), broker=b, clock=clock)
    s.reg, s.sig = "SHORT", {"SHORT": "trigger"}
    eng.cycle()
    assert not b.opened and eng._actual(b.snapshot())[0] == Actual.SHORT
    c.next_bar()
    s.exit = "4h_close_above_ma50"
    eng.cycle()
    assert b.closed == [pid]


def test_restart_resumes_pending_open_without_resubmitting():
    clock = Clock()
    b = FakeBroker(clock)
    b.fill_mode = "pending"
    eng, b, s, c, logs = make(broker=b, clock=clock)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    eng.cycle()
    saved = eng.st
    eng2, _, s2, _, logs2 = make(state=saved, broker=b, clock=clock)
    s2.reg, s2.sig = "LONG", {"LONG": "trigger"}
    eng2.cycle()
    assert len(b.opened) == 1


def test_local_state_says_long_but_broker_is_flat():
    st = State(known_positions={"55": {"side": "LONG", "units": 0.3, "open_rate": 380.0, "stop": 370, "target": 400}})
    eng, b, s, c, logs = make(state=st)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    eng.cycle()
    assert events(logs, "broker_closed")
    assert eng.st.known_positions == {}
    assert not b.opened                                  # väntar på nästa stapel efter exit


def test_position_without_broker_stop_is_emergency_closed():
    eng, b, s, c, logs = make()
    pid = b.add_position(True, None, 400, has_stop=False)
    eng.cycle()
    assert b.closed == [pid]


# ---------- stop, mål, strategi-exit ----------
def test_stop_hit_closes():
    eng, b, s, c, logs = make()
    pid = b.add_position(True, 375.0, 400.0)
    b.bid, b.ask = 374.0, 374.1
    eng.cycle()
    assert b.closed == [pid]
    assert any(l.get("exit_reason") == "stop_hit" for l in events(logs, "close_submitted"))


def test_target_hit_closes_short():
    eng, b, s, c, logs = make()
    pid = b.add_position(False, 390.0, 370.0)
    b.bid, b.ask = 369.8, 369.9
    eng.cycle()
    assert b.closed == [pid]
    assert any(l.get("exit_reason") == "target_hit" for l in events(logs, "close_submitted"))


# ---------- kill switch och förlustgränser ----------
def test_kill_switch_blocks_open_but_allows_close(tmp_path):
    ks = tmp_path / "KILL_SWITCH"
    ks.write_text("1")
    eng, b, s, c, logs = make(kill_switch_file=str(ks))
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert eng.cycle() == "blocked" and not b.opened
    pid = b.add_position(True, 370, 400)
    c.next_bar()
    s.exit = "daily_regime_not_long"
    eng.cycle()
    assert b.closed == [pid]


def test_daily_loss_limit_blocks_open_but_allows_close():
    eng, b, s, c, logs = make()
    eng._roll_periods(c())
    eng.st.realized_day_usd = -20.0                      # ~ -200 SEK > 1 % av 9 983 SEK
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert eng.cycle() == "blocked"
    assert any("daglig förlustgräns nådd" in l["reasons"] for l in events(logs, "blocked"))
    pid = b.add_position(True, 370, 400)
    c.next_bar()
    eng.st.realized_day_usd = -20.0
    s.exit = "daily_regime_not_long"
    eng.cycle()
    assert b.closed == [pid]


# ---------- säkerhet ----------
def test_dry_run_sends_nothing():
    eng, b, s, c, logs = make(mode="DRY_RUN")
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert eng.cycle() == "dry_run"
    eng.cycle()
    assert not b.opened and len(events(logs, "DRY_RUN")) == 1


def test_wrong_portfolio_blocks_open():
    clock = Clock()
    b = FakeBroker(clock)
    b.username = "someone-else"
    eng, b, s, c, logs = make(broker=b, clock=clock)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert eng.cycle() == "blocked" and not b.opened


def test_unknown_action_is_rejected():
    with pytest.raises(ValueError):
        from agent.models import TradeAction
        TradeAction(action="BUY_EVERYTHING", instrument=3025, current_position="FLAT", desired_position="LONG",
                    signal_timestamp="t", signal_bar="b", strategy_version="v", idempotency_key="k")
    eng, b, s, c, logs = make()

    class Fake:
        action = "BUY_EVERYTHING"
    with pytest.raises(ValueError):
        eng.exe.execute(Fake(), eng.st, c())


def test_stale_signal_bar_is_not_traded():
    eng, b, s, c, logs = make()
    c.t = c.t.replace(hour=12, minute=45)                # 45 min efter stapelns slut > 30 min
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert eng.cycle() == "blocked" and not b.opened


def test_stale_usdsek_blocks_open():
    eng, b, s, c, logs = make()
    b.fx_age_s = 600
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert eng.cycle() == "blocked" and not b.opened
    assert any("USDSEK saknas eller är gammal" in l["reasons"] for l in events(logs, "blocked"))


def test_order_sizes_to_risk_budget_in_sek():
    eng, b, s, c, logs = make()
    open_long(eng, b, s)
    sz = events(logs, "order_submitted")[0]["sizing"]
    assert sz["expected_loss_at_stop_sek"] <= sz["risk_budget_sek"] <= 25.0
    assert abs(sz["risk_budget_sek"] - 24.96) < 0.01
    assert sz["units"] == b.opened[0]["units"]
