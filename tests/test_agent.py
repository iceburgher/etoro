from agent import risk, strategy
from agent.config import Config

cfg = Config()


def st(**kw):
    return risk.State(day="2026-09-30", start_equity=1000, **kw)


def test_uptrend_buys():
    closes = [100 + i for i in range(60)]
    assert strategy.signal(closes, 20, 50) == "buy"


def test_downtrend_holds():
    closes = [200 - i for i in range(60)]
    assert strategy.signal(closes, 20, 50) == "hold"


def test_too_little_data_holds():
    assert strategy.signal([1, 2, 3], 20, 50) == "hold"


def test_blocks_other_instrument():
    assert not risk.check_buy(cfg, st(), 999, 1000, 0, 100)[0]


def test_blocks_oversize_trade():
    assert not risk.check_buy(cfg, st(), 18, 1000, 0, 500)[0]


def test_blocks_total_exposure():
    assert not risk.check_buy(cfg, st(), 18, 1000, 350, 100)[0]


def test_daily_loss_halts():
    s = st()
    assert not risk.check_buy(cfg, s, 18, 960, 0, 100)[0]
    assert s.halted


def test_ok_trade():
    assert risk.check_buy(cfg, risk.State(day="2026-09-30", start_equity=10000), 18, 10000, 0, 500)[0]


def test_blocks_below_min_exposure():
    ok, why = risk.check_buy(cfg, risk.State(day="2026-09-30", start_equity=10000), 18, 10000, 0, 200)
    assert not ok and "exponering" in why


def test_account_numbers_includes_pnl_and_held():
    from agent.main import account_numbers
    pf = {"clientPortfolio": {"credit": 800, "unrealizedPnL": -30,
                              "positions": [{"instrumentID": 18, "amount": 200}], "ordersForOpen": []}}
    assert account_numbers(pf) == (970, 200, {18})
