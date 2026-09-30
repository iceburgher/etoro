"""Positionsstorlek med separata valutor.

eToros modell för agentportföljer: agenten handlar ett virtuellt saldo i USD (10 000). Den som kopierar
(du) har investerat ett eget belopp som speglas proportionellt. Risken räknas därför på agentens
USD-saldo, och samma procent landar i din kopia, som vi också redovisar i SEK.
"""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Sizing:
    agent_equity_usd: float          # agentportföljens virtuella saldo (API: credit / accountTotalValue)
    copy_investment_usd: float       # ditt kopieringsbelopp
    copy_ratio: float                # copy_investment_usd / agent_equity_usd
    copy_equity_sek: float
    instrument_currency: str
    price_usd: float
    stop_usd: float
    usdsek_rate: float
    fx_timestamp: str
    fx_source: str
    risk_pct: float
    risk_budget_usd: float           # på agentnivå
    risk_budget_sek: float           # i din kopia
    units: float                     # på agentnivå, avrundat nedåt
    notional_exposure_usd: float
    margin_required_usd: float
    expected_loss_at_stop_usd: float  # på agentnivå
    copy_expected_loss_usd: float
    expected_loss_at_stop_sek: float  # i din kopia
    within_budget: bool


def size_trade(*, agent_equity_usd: float, copy_investment_usd: float, price_usd: float, stop_usd: float,
               leverage: int, usdsek_rate: float, fx_timestamp: str, fx_source: str,
               risk_pct: float, unit_step: float = 0.01) -> Sizing:
    if min(agent_equity_usd, copy_investment_usd, price_usd, usdsek_rate) <= 0 or leverage < 1:
        raise ValueError("ogiltig indata för storlek")
    dist = abs(price_usd - stop_usd)
    if dist <= 0:
        raise ValueError("stop lika med pris")
    risk_budget_usd = agent_equity_usd * risk_pct
    units = math.floor(risk_budget_usd / dist / unit_step) * unit_step  # alltid nedåt
    units = round(units, 8)
    loss_usd = units * dist
    ratio = copy_investment_usd / agent_equity_usd
    copy_equity_sek = copy_investment_usd * usdsek_rate
    risk_budget_sek = copy_equity_sek * risk_pct
    copy_loss_usd = loss_usd * ratio
    loss_sek = copy_loss_usd * usdsek_rate
    notional = units * price_usd
    return Sizing(
        agent_equity_usd=agent_equity_usd, copy_investment_usd=copy_investment_usd, copy_ratio=ratio,
        copy_equity_sek=copy_equity_sek, instrument_currency="USD", price_usd=price_usd, stop_usd=stop_usd,
        usdsek_rate=usdsek_rate, fx_timestamp=fx_timestamp, fx_source=fx_source, risk_pct=risk_pct,
        risk_budget_usd=risk_budget_usd, risk_budget_sek=risk_budget_sek, units=units,
        notional_exposure_usd=notional, margin_required_usd=notional / leverage,
        expected_loss_at_stop_usd=loss_usd, copy_expected_loss_usd=copy_loss_usd,
        expected_loss_at_stop_sek=loss_sek,
        within_budget=units > 0 and loss_usd <= risk_budget_usd + 1e-9 and loss_sek <= risk_budget_sek + 1e-9,
    )
