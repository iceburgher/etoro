"""Motorns livscykel: övergångar, vändningar, dubbletter, avstämning, stop/mål, spärrar."""
import pytest

from agent.models import Actual, TradeAction
from agent.state import State
from tests.fakes import Clock, FakeBroker, FailingProvider, M, S, edit_state, make


def sides(b):
    return sorted("LONG" if p.is_buy else "SHORT" for p in b.positions.values())


def events(logs, name):
    return [l for l in logs if l.get("event") == name]


def alerts(eng, level=None):
    return [k for lv, k in eng.alert.sent if level is None or lv == level]


def open_long(eng, b, s):
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "filled"
    assert sides(b) == ["LONG"]


# ---------- grundövergångar ----------
def test_flat_to_long():
    eng, b, s, c, logs = make()
    open_long(eng, b, s)
    o = b.opened[0]
    assert o["is_buy"] and o["stop"] < 380.1 < o["target"]
    assert eng.st.opens_today == 1 and "OPEN_LONG" in alerts(eng, "normal")
    assert "unexpected_broker_position" not in alerts(eng)


def test_long_to_flat_on_strategy_exit():
    eng, b, s, c, logs = make()
    open_long(eng, b, s)
    c.next_bar()
    s.reg, s.sig, s.exit = None, {}, "daily_regime_not_long"
    assert S(eng) == "closed"
    assert sides(b) == [] and eng.st.last_exit_bar and eng.st.pending is None
    assert "CLOSE_LONG" in alerts(eng, "normal")


def test_flat_to_short_and_back_to_flat():
    eng, b, s, c, logs = make()
    s.reg, s.sig = "SHORT", {"SHORT": "trigger"}
    assert S(eng) == "filled"
    assert sides(b) == ["SHORT"] and not b.opened[0]["is_buy"]
    assert b.opened[0]["stop"] > 380.0 > b.opened[0]["target"]
    c.next_bar()
    s.exit = "4h_close_above_ma50"
    assert S(eng) == "closed"
    assert sides(b) == [] and "CLOSE_SHORT" in alerts(eng, "normal")


# ---------- vändningar ----------
def test_long_to_short_reversal_waits_one_bar():
    eng, b, s, c, logs = make()
    open_long(eng, b, s)
    c.next_bar()
    s.reg, s.sig, s.exit = "SHORT", {"SHORT": "trigger"}, "daily_regime_not_long"
    assert S(eng) == "closed"
    assert sides(b) == [] and len(b.opened) == 1       # ingen short i samma jobb
    M(eng)
    assert len(b.opened) == 1                           # inte heller på samma stapel
    c.next_bar()
    s.exit = None
    assert S(eng) == "filled"                           # nästa stapel, signalen finns kvar
    assert sides(b) == ["SHORT"]


def test_short_to_long_reversal_needs_signal_on_next_bar():
    eng, b, s, c, logs = make()
    s.reg, s.sig = "SHORT", {"SHORT": "trigger"}
    S(eng)
    c.next_bar()
    s.reg, s.sig, s.exit = "LONG", {"LONG": "trigger"}, "daily_regime_not_short"
    assert S(eng) == "closed"
    c.next_bar()
    s.sig, s.exit = {}, None                            # signalen borta på nästa stapel
    S(eng)
    assert sides(b) == [] and len(b.opened) == 1


# ---------- dubbletter och väntande ----------
def test_no_duplicate_open_when_order_pending():
    eng, b, s, c, logs = make()
    b.fill_mode = "pending"
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "pending"
    for _ in range(3):
        M(eng)
    assert len(b.opened) == 1 and eng.st.pending["kind"] == "open"


def test_pending_open_fills_later_via_reconciliation():
    eng, b, s, c, logs = make()
    b.fill_mode = "pending"
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    S(eng)
    key = b.opened[0]["key"]
    pid = b.add_position(True, 369.25, 401.8)
    b.orders[key] = type(b.orders[key])(3, None, (pid,))
    M(eng)
    assert eng.st.pending is None and events(logs, "reconciled_fill") and eng.st.opens_today == 1
    assert eng.store.get_order_intent(key)["status"] == "filled"


def test_no_duplicate_close():
    eng, b, s, c, logs = make()
    open_long(eng, b, s)
    b.close_mode = "stuck"
    c.next_bar()
    s.exit = "4h_close_below_ma50"
    assert S(eng) == "pending"
    for _ in range(3):
        M(eng)
    assert len(b.closed) == 1


def test_broker_pending_order_means_no_action():
    eng, b, s, c, logs = make()
    b.pending_instr = {3025}
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    S(eng)
    assert not b.opened


def test_rejected_open_is_not_retried_same_bar():
    eng, b, s, c, logs = make()
    b.fill_mode = "reject"
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "rejected"
    M(eng)
    assert len(b.opened) == 1


# ---------- avstämning ----------
def test_local_state_says_long_but_broker_is_flat_is_critical_mismatch():
    st = State(known_positions={"55": {"side": "LONG", "units": 0.3, "open_rate": 380.0, "stop": 370, "target": 400}})
    eng, b, s, c, logs = make(state=st)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    S(eng)
    assert "reconciliation_mismatch" in alerts(eng, "critical")
    assert eng.st.known_positions == {} and not b.opened   # väntar på nästa stapel efter exit


def test_broker_stop_exit_is_normal_alert():
    st = State(known_positions={"55": {"side": "LONG", "units": 0.3, "open_rate": 380.0, "stop": 380.0, "target": 400}})
    eng, b, s, c, logs = make(state=st)
    M(eng)
    assert "stop_exit" in alerts(eng, "normal") and "reconciliation_mismatch" not in alerts(eng)


def test_position_without_broker_stop_is_emergency_closed():
    eng, b, s, c, logs = make()
    pid = b.add_position(True, None, 400, has_stop=False)
    M(eng)
    assert b.closed == [pid]
    assert {"missing_broker_stop", "unexpected_broker_position"} <= set(alerts(eng, "critical"))


# ---------- stop, mål ----------
def test_stop_hit_closes():
    eng, b, s, c, logs = make()
    pid = b.add_position(True, 375.0, 400.0)
    b.bid, b.ask = 374.0, 374.1
    M(eng)
    assert b.closed == [pid]
    assert any(l.get("exit_reason") == "stop_hit" for l in events(logs, "close_submitted"))


def test_target_hit_closes_short():
    eng, b, s, c, logs = make()
    pid = b.add_position(False, 390.0, 370.0)
    b.bid, b.ask = 369.8, 369.9
    M(eng)
    assert b.closed == [pid]
    assert any(l.get("exit_reason") == "target_hit" for l in events(logs, "close_submitted"))


# ---------- spärrar ----------
def test_kill_switch_blocks_open_but_allows_close():
    eng, b, s, c, logs = make()
    eng.store.set_control(kill_switch=True)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "blocked" and not b.opened
    assert "kill_switch_active" in alerts(eng, "critical")


def test_daily_loss_limit_blocks_open():
    eng, b, s, c, logs = make()
    edit_state(eng, day="2026-09-30", week="2026-W40", realized_day_usd=-20.0, realized_week_usd=-20.0)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "blocked"
    assert any("daglig förlustgräns nådd" in l["reasons"] for l in events(logs, "blocked"))
    assert "daily_loss_limit" in alerts(eng, "critical")


def test_dry_run_sends_nothing():
    eng, b, s, c, logs = make(mode="DRY_RUN")
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "dry_run"
    M(eng)
    assert not b.opened and len(events(logs, "DRY_RUN")) == 1


def test_wrong_portfolio_blocks_open():
    clock = Clock()
    b = FakeBroker(clock)
    b.username = "someone-else"
    eng, b, s, c, logs = make(broker=b, clock=clock)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "blocked" and not b.opened


def test_unknown_action_is_rejected():
    with pytest.raises(ValueError):
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
    assert S(eng) == "blocked" and not b.opened


def test_order_sizes_to_risk_budget_in_sek():
    eng, b, s, c, logs = make()
    open_long(eng, b, s)
    sz = events(logs, "order_submitted")[0]["sizing"]
    assert sz["expected_loss_at_stop_sek"] <= sz["risk_budget_sek"] <= 50.0
    assert abs(sz["risk_budget_sek"] - 49.92) < 0.01
    assert sz["units"] == b.opened[0]["units"]
    assert all(l.get("git_sha") == "testsha" for l in events(logs, "order_submitted"))


def test_max_one_gld_position():
    eng, b, s, c, logs = make()
    b.add_position(True, 370, 400)
    edit_state(eng, desired={"side": "LONG"})
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    S(eng)
    assert not b.opened


def test_no_email_provider_blocks_real_opens():
    eng, b, s, c, logs = make()
    eng.alerts_ready = False
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "blocked" and not b.opened


def test_alert_failure_never_blocks_close():
    eng, b, s, c, logs = make(provider=FailingProvider())
    pid = b.add_position(True, 375.0, 400.0)
    b.bid, b.ask = 374.0, 374.1
    M(eng)
    assert b.closed == [pid] and events(logs, "alert_failed")


def test_integration_trade_halts_new_entries_after_first_fill():
    eng, b, s, c, logs = make(integration_trade=True)
    open_long(eng, b, s)
    assert eng.store.control()["halt_new_entries"] and "integration_trade_done" in alerts(eng, "critical")
    c.next_bar()
    s.exit = "daily_regime_not_long"
    S(eng)                                               # stängning tillåts
    c.next_bar()
    s.exit = None
    S(eng)                                               # men ingen ny öppning
    assert len(b.opened) == 1 and sides(b) == []


# ---------- armerad integrationsaffär (kill switch på, en enda affär får passera) ----------
def test_armed_integration_trade_passes_kill_switch_once_then_kill_switch_stays_on():
    eng, b, s, c, logs = make(integration_trade=True)
    eng.store.set_control(kill_switch=True, integration_armed=True)
    open_long(eng, b, s)
    sz = events(logs, "order_submitted")[0]["sizing"]
    assert sz["risk_pct"] == 0.0025 and sz["expected_loss_at_stop_sek"] <= sz["risk_budget_sek"] <= 25.0
    ctrl = eng.store.control()
    assert ctrl == {"kill_switch": True, "halt_new_entries": True, "integration_armed": False}
    assert "integration_trade_done" in alerts(eng, "critical")
    c.next_bar()
    s.exit = "daily_regime_not_long"
    assert S(eng) == "closed"                            # stängning tillåts trots kill switch
    c.next_bar()
    s.exit = None
    eng.store.set_control(integration_armed=True)        # även om någon armerar igen: bara en affär
    S(eng)
    assert len(b.opened) == 1 and sides(b) == []


def test_kill_switch_blocks_open_when_not_armed_in_integration_mode():
    eng, b, s, c, logs = make(integration_trade=True)
    eng.store.set_control(kill_switch=True)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "blocked" and not b.opened


def test_armed_does_not_bypass_kill_switch_outside_integration_mode():
    eng, b, s, c, logs = make(integration_trade=False)
    eng.store.set_control(kill_switch=True, integration_armed=True)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    assert S(eng) == "blocked" and not b.opened


def test_armed_does_not_send_real_orders_in_dry_run():
    eng, b, s, c, logs = make(mode="DRY_RUN", integration_trade=True)
    eng.store.set_control(kill_switch=True, integration_armed=True)
    s.reg, s.sig = "LONG", {"LONG": "trigger"}
    S(eng)
    assert not b.opened
