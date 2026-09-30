"""Regressionstester för de 7 fynden i kodgranskningen 30 sep."""
from agent.models import OrderStatus
from agent.state import State
from tests.fakes import FakeBroker, M, S, edit_state, make, Clock


def alerts(eng, level=None):
    return [k for lv, k in eng.alert.sent if level is None or lv == level]


def long_signal(s):
    s.reg, s.sig = "LONG", {"LONG": "trigger"}


# 1. En stapel som strategijobbet missar utvärderas av nästa monitorjobb.
def test_monitor_evaluates_bar_missed_by_strategy_job():
    eng, b, s, c, logs = make()
    eng.store.acquire_lock("gld-trading", "monitor-som-höll-låset", c(), 30)
    long_signal(s)
    assert S(eng) == "locked"
    c.minutes(1)
    assert M(eng) == "filled"
    assert len(b.opened) == 1


# 2. Integrationsspärren håller även om fyllningen bekräftas först vid avstämning, och även om
#    skrivningen av halt-flaggan misslyckas.
def test_integration_halt_when_fill_confirmed_in_reconciliation_and_halt_write_fails():
    eng, b, s, c, logs = make(integration_trade=True)
    b.fill_mode = "pending"
    long_signal(s)
    S(eng)
    key = b.opened[0]["key"]

    def broken(**kw):
        raise RuntimeError("db nere")
    eng.store.set_control = broken
    pid = b.add_position(True, 369.25, 401.8)
    b.orders[key] = OrderStatus(3, None, (pid,))
    M(eng)                                              # fyllning bekräftas här
    b.positions.clear()                                 # positionen stängs hos eToro (stop)
    c.next_bar()
    long_signal(s)
    S(eng)
    assert len(b.opened) == 1                           # ingen ny öppning
    assert any("integrationsaffären" in r for l in logs if l.get("event") == "blocked" for r in l["reasons"])


# 3. Stängning vars avsikt skapats men aldrig skickats (databasfel) skickas direkt nästa jobb.
def test_close_intent_created_but_not_sent_is_sent_next_job():
    clock = Clock()
    b = FakeBroker(clock)
    pid = b.add_position(True, 375.0, 400.0)
    st = State(desired={"side": "LONG"}, known_positions={str(pid): {"side": "LONG", "units": 0.3,
               "open_rate": 380.0, "stop": 375.0, "target": 400.0}})
    eng, b, s, c, logs = make(broker=b, clock=clock, state=st)
    b.bid, b.ask = 374.0, 374.1                         # stop passerad
    eng.store.fail_saves_after = 1
    assert M(eng) == "db_error" and not b.closed         # inget skickat
    eng.store.fail_saves_after = None
    c.minutes(1)
    M(eng)
    assert b.closed == [pid]                             # skickat direkt, inte efter 10 min


# 4. Position som eToro stänger när kursen saknas: förlusten bokförs ändå (vid stoppen).
def test_realized_loss_counted_even_without_quote():
    st = State(day="2026-09-30", week="2026-W40",
               known_positions={"55": {"side": "LONG", "units": 1.0, "open_rate": 380.0, "stop": 370.0, "target": 400}})
    eng, b, s, c, logs = make(state=st)
    b.quote_fails = True
    M(eng)
    assert eng.store.load_state().realized_day_usd == -10.0


# 5. Exponering räknar bara GLD.
def test_exposure_ignores_other_instruments():
    eng, b, s, c, logs = make()
    b.add_position(True, 1, 100000, units=100.0, open_rate=100.0, instrument=999)   # stor annan position
    long_signal(s)
    assert S(eng) == "filled"


# 6. Nätverksfel mot lookup i över 10 min får inte märka ordern som förlorad; fast order larmar.
def test_lookup_network_failure_does_not_mark_lost_and_stuck_order_alerts():
    eng, b, s, c, logs = make()
    b.fill_mode = "pending"
    long_signal(s)
    S(eng)
    b.lookup_fails = True
    c.minutes(11)
    M(eng)
    assert eng.st.pending is not None                    # fortfarande väntande, inte "lost"
    assert "unknown_order_state" in alerts(eng, "critical")
    b.lookup_fails = False
    c.minutes(1)
    M(eng)
    assert eng.st.pending is not None                    # eToro säger fortfarande "pågår": väntar
    assert len(b.opened) == 1


# 7. Öppning vars avsikt sparades men som aldrig skickades (databasfel före anrop) skickas nästa jobb.
def test_open_intent_created_but_never_sent_is_sent_next_job():
    eng, b, s, c, logs = make()
    long_signal(s)
    eng.store.fail_saves_after = 1
    assert S(eng) == "db_error" and not b.opened
    eng.store.fail_saves_after = None
    c.minutes(1)
    assert M(eng) == "filled"
    assert len(b.opened) == 1
    assert "unknown_order_state" not in alerts(eng)
