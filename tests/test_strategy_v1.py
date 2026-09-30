from datetime import datetime, timedelta

from agent import strategy_v1 as s
from collections import namedtuple

Bar = namedtuple("Bar", "start open high low close")


def bars(closes, spread=1.0):
    t0 = datetime(2026, 1, 1)
    return [Bar(t0 + timedelta(hours=4 * i), c, c + spread, c - spread, c) for i, c in enumerate(closes)]


def test_regime_long_short_none():
    assert s.regime([100 + i for i in range(60)]) == "LONG"
    assert s.regime([200 - i for i in range(60)]) == "SHORT"
    assert s.regime([100] * 60) is None
    assert s.regime([100 + i for i in range(10)]) is None


def test_long_pullback_trigger():
    up = [100 + i * 0.5 for i in range(60)]
    closes = up + [up[-1] - 6, up[-1] - 5, up[-1] + 3]  # rekyl under MA20, sedan stängning över
    assert s.signal(bars(closes), "LONG")
    assert s.signal(bars(closes), "SHORT") is None


def test_short_rally_trigger_mirrors_long():
    down = [200 - i * 0.5 for i in range(60)]
    closes = down + [down[-1] + 6, down[-1] + 5, down[-1] - 3]
    assert s.signal(bars(closes), "SHORT")
    assert s.signal(bars(closes), "LONG") is None


def test_no_trigger_without_setup():
    up = [100 + i * 0.5 for i in range(63)]  # ingen rekyl under MA20
    assert s.signal(bars(up), "LONG") is None


def test_stop_target_2r_and_min_atr():
    b = bars([100 + i * 0.5 for i in range(60)])
    stop, target = s.stop_target(b, "LONG", 130.0)
    assert stop < 130 and abs((target - 130) - 2 * (130 - stop)) < 1e-9
    stop, target = s.stop_target(b, "SHORT", 130.0)
    assert stop > 130 and abs((130 - target) - 2 * (stop - 130)) < 1e-9


def test_exit_on_regime_or_ma50():
    b = bars([100 + i * 0.5 for i in range(60)])
    assert s.exit_reason("LONG", "SHORT", b) == "daily_regime_not_long"
    assert s.exit_reason("LONG", "LONG", b) is None
    crash = bars([100 + i * 0.5 for i in range(60)] + [80])
    assert s.exit_reason("LONG", "LONG", crash) == "4h_close_below_ma50"
