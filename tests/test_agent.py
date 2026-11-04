import pytest

from agent import sizing


def _size(**kw):
    base = dict(allocated_capital_sek=9983.59, price_usd=381.47, stop_usd=370.03, leverage=2,
                usdsek_rate=9.9845, fx_timestamp="t", fx_source="test", risk_pct=0.0025)
    return sizing.size_trade(**{**base, **kw})


def test_sizing_within_budget_both_currencies():
    s = _size()
    assert s.within_budget
    assert s.expected_loss_at_stop_usd <= s.risk_budget_usd
    assert s.expected_loss_at_stop_sek <= s.risk_budget_sek
    assert abs(s.risk_budget_sek - 24.96) < 0.01 and abs(s.risk_budget_usd - 2.50) < 0.01


def test_sizing_rounds_units_down():
    s = _size(stop_usd=381.47 - 7.0)  # 2,4998 / 7 = 0,357 -> 0,35
    assert s.units == 0.35


def test_sizing_rejects_zero_stop_distance():
    with pytest.raises(ValueError):
        _size(stop_usd=381.47)


def test_leverage_changes_margin_not_risk():
    a, b = _size(leverage=1), _size(leverage=5)
    assert a.units == b.units and a.expected_loss_at_stop_sek == b.expected_loss_at_stop_sek
    assert b.margin_required_usd < a.margin_required_usd
