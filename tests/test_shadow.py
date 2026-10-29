"""Skuggläge: klassning, papperstrategier, papperssiffror och att det aldrig rör handeln."""
from datetime import datetime, timedelta, timezone

from agent import shadow
from agent.models import Bar
from tests.fakes import M, S, make

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def bars(closes, step=timedelta(days=1), spread=1.0):
    return [Bar(T0 + step * i, c, c + spread, c - spread, c) for i, c in enumerate(closes)]


# ---------- klassning ----------
def test_classify_clean_uptrend():
    env = shadow.classify(bars([100 + i for i in range(120)]), bars([100 + i * 0.1 for i in range(200)], timedelta(hours=4)))
    assert env["riktning_dag"] == "UPP" and env["styrka_dag"] == "STARK"
    assert env["etikett"].startswith("UPP/STARK/")
    assert env["h4_mot_ma50"] == "ÖVER"


def test_classify_downtrend_and_chop():
    down = shadow.classify(bars([300 - i for i in range(120)]), bars([300 - i * 0.1 for i in range(200)], timedelta(hours=4)))
    assert down["riktning_dag"] == "NER" and down["h4_mot_ma50"] == "UNDER"
    zig = bars([100 + (i % 2) * 2 for i in range(120)])
    chop = shadow.classify(zig, bars([100 + (i % 2) for i in range(200)], timedelta(hours=4)))
    assert chop["styrka_dag"] == "SVAG"


def test_classify_with_too_little_data_does_not_crash():
    env = shadow.classify(bars([100.0] * 10), bars([100.0] * 10, timedelta(hours=4)))
    assert (env["riktning_dag"], env["styrka_dag"], env["vol_4h"], env["h4_mot_ma50"]) == ("OKLAR", None, None, None)
    assert env["etikett"] == "OKLAR/?/?"


def test_volatility_rank_high_when_latest_range_is_widest():
    h4 = bars([100.0] * 60, timedelta(hours=4), spread=1.0)
    h4[-1] = Bar(h4[-1].start, 100, 110, 90, 100)
    assert shadow.classify(bars([100.0] * 120), h4)["vol_4h"] == "HÖG"


# ---------- papperstrategier ----------
def test_buy_and_hold_is_always_long():
    assert shadow.side_bh([]) == "LONG"


def test_sma200_needs_history_then_follows_trend():
    assert shadow.side_sma200(bars([100.0] * 199)) is None
    assert shadow.side_sma200(bars([100 + i for i in range(220)])) == "LONG"
    assert shadow.side_sma200(bars([400 - i for i in range(220)])) == "FLAT"


def test_donchian_enters_on_breakout_and_exits_on_20d_low():
    up = bars([100.0] * 60 + [101, 102, 103], spread=0.1)
    assert shadow.side_donchian(up) == "LONG"
    assert shadow.side_donchian(bars([100.0] * 54)) is None
    out = bars([100.0] * 60 + [101, 102, 103] + [98.0], spread=0.1)
    assert shadow.side_donchian(out) == "FLAT"


def test_tsmom_compares_with_252_days_ago():
    assert shadow.side_tsmom(bars([100.0] * 252)) is None
    assert shadow.side_tsmom(bars([100.0] + [90.0] * 251 + [110.0])) == "LONG"
    assert shadow.side_tsmom(bars([100.0] + [110.0] * 251 + [90.0])) == "FLAT"


# ---------- papperssiffror ----------
def row(bar, close, sides, label="UPP/STARK/LÅG"):
    return {"bar": bar, "close": close, "env": {"etikett": label}, "sides": sides}


def test_paper_stats_long_short_flat_and_environment_split():
    flat = {n: "FLAT" for n in shadow.STRATEGIES}
    rows = [row("a", 100, {**flat, "bh": "LONG", "gld-v1.1": "SHORT"}, "NER/MEDEL/HÖG"),
            row("b", 110, {**flat, "bh": "LONG", "gld-v1.1": "FLAT"}),
            row("c", 99, flat)]
    st = shadow.paper_stats(rows)
    assert abs(st["bh"]["avkastning"] - (1.1 * 0.9 - 1)) < 1e-12           # +10 %, sedan -10 %
    assert abs(st["gld-v1.1"]["avkastning"] - (-0.10)) < 1e-12              # short under +10 %, sedan flat
    assert st["gld-v1.1"]["sidbyten"] == 1 and st["bh"]["sidbyten"] == 0
    assert st["bh"]["andel_i_marknad"] == 1.0 and st["sma200"]["avkastning"] == 0.0
    assert st["gld-v1.1"]["per_miljo"]["NER/MEDEL/HÖG"]["staplar"] == 1
    assert st["bh"]["max_ras"] < 0


def test_paper_stats_skips_strategies_without_data():
    rows = [row("a", 100, {"bh": "LONG", "sma200": None}), row("b", 101, {"bh": "LONG", "sma200": None})]
    st = shadow.paper_stats(rows)
    assert st["sma200"]["staplar"] == 0 and st["bh"]["staplar"] == 1


# ---------- motorn ----------
def test_records_once_per_bar_after_strategy_and_never_trades():
    eng, b, s, c, logs = make()
    assert eng.run_shadow() == "waiting_for_strategy"
    S(eng)
    assert eng.run_shadow() == "recorded"
    assert eng.run_shadow() == "already"
    (r,) = eng.store.shadow_rows()
    assert set(r["sides"]) == set(shadow.STRATEGIES) and r["sides"]["gld-v1.1"] == "FLAT"
    assert r["env"]["etikett"] and r["close"] == 380
    assert b.opened == [] and b.closed == []


def test_next_bar_gets_its_own_row():
    eng, b, s, c, logs = make()
    S(eng), eng.run_shadow()
    c.next_bar()
    S(eng)
    assert eng.run_shadow() == "recorded"
    assert len(eng.store.shadow_rows()) == 2


def test_shadow_records_live_desired_side():
    eng, b, s, c, logs = make(mode="DRY_RUN")
    s.reg, s.sig = "SHORT", {"SHORT": "trigger"}
    S(eng)
    eng.run_shadow()
    assert eng.store.shadow_rows()[0]["sides"]["gld-v1.1"] == "SHORT"


def test_candle_failure_is_swallowed_and_logged_and_does_not_touch_trading():
    eng, b, s, c, logs = make()
    S(eng)
    b.candles = lambda *a: (_ for _ in ()).throw(ConnectionError("eToro nere"))
    assert eng.run_shadow() == "error"
    assert any(l.get("event") == "shadow_error" for l in logs)
    assert eng.store.shadow_rows() == []
    assert M(eng) == "no_action"       # handeln fungerar som vanligt


def test_data_not_ready_is_skipped_not_recorded():
    eng, b, s, c, logs = make()
    S(eng)
    real = b.candles
    b.candles = lambda iid, iv, n: real(iid, iv, n)[:-1] if iv == "FourHours" else real(iid, iv, n)
    assert eng.run_shadow() == "data_not_ready"
    assert eng.store.shadow_rows() == []


def test_not_recorded_in_preview():
    eng, b, s, c, logs = make(vercel_env="preview")
    S(eng)
    assert eng.run_shadow() == "disabled_outside_production"
    assert eng.store.shadow_rows() == []
