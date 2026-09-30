"""Positionsstorlek med separata valutor.

Riskbasen är avsatt kapital i SEK (allocated_capital_sek). Riskbudgeten räknas om till USD med USDSEK,
eftersom GLD:s pris, stop, exponering och ordrar är i USD. Enheter avrundas nedåt, och förväntad
förlust vid stop kontrolleras i både USD och SEK. eToros API-värde på 10 000 används aldrig här.
"""
import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Sizing:
    allocated_capital_sek: float
    allocated_capital_usd: float
    portfolio_display_currency: str
    instrument_currency: str
    gld_price_usd: float
    stop_usd: float
    usdsek_rate: float
    fx_timestamp: str
    fx_source: str
    risk_pct: float
    risk_budget_sek: float
    risk_budget_usd: float
    units: float                      # avrundat nedåt
    notional_exposure_usd: float
    margin_required_usd: float
    expected_loss_at_stop_usd: float
    expected_loss_at_stop_sek: float
    within_budget: bool

    def as_dict(self) -> dict:
        return asdict(self)


def size_trade(*, allocated_capital_sek: float, price_usd: float, stop_usd: float, leverage: int,
               usdsek_rate: float, fx_timestamp: str, fx_source: str, risk_pct: float,
               unit_step: float = 0.01) -> Sizing:
    if min(allocated_capital_sek, price_usd, usdsek_rate) <= 0 or leverage < 1:
        raise ValueError("ogiltig indata för storlek")
    dist = abs(price_usd - stop_usd)
    if dist <= 0:
        raise ValueError("stop lika med pris")
    risk_budget_sek = allocated_capital_sek * risk_pct
    risk_budget_usd = risk_budget_sek / usdsek_rate
    units = round(math.floor(risk_budget_usd / dist / unit_step) * unit_step, 8)  # alltid nedåt
    loss_usd = units * dist
    loss_sek = loss_usd * usdsek_rate
    notional = units * price_usd
    return Sizing(
        allocated_capital_sek=allocated_capital_sek, allocated_capital_usd=allocated_capital_sek / usdsek_rate,
        portfolio_display_currency="SEK", instrument_currency="USD", gld_price_usd=price_usd, stop_usd=stop_usd,
        usdsek_rate=usdsek_rate, fx_timestamp=fx_timestamp, fx_source=fx_source, risk_pct=risk_pct,
        risk_budget_sek=risk_budget_sek, risk_budget_usd=risk_budget_usd, units=units,
        notional_exposure_usd=notional, margin_required_usd=notional / leverage,
        expected_loss_at_stop_usd=loss_usd, expected_loss_at_stop_sek=loss_sek,
        within_budget=units > 0 and loss_usd <= risk_budget_usd + 1e-9 and loss_sek <= risk_budget_sek + 1e-9,
    )
