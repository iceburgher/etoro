import argparse
import datetime as dt
import json
import math
import time

from . import ai_filter, risk, sizing, strategy
from .config import Config
from .etoro import Etoro


def log(**kw):
    print(json.dumps({"t": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **kw}, ensure_ascii=False), flush=True)


def _iid(x: dict):
    return x.get("instrumentId", x.get("instrumentID"))


def account_numbers(pf: dict) -> tuple[float, float, set]:
    """(equity, invested, instrument-id:n vi redan har position/väntande order i).
    eToro: credit = fritt saldo, positions[].amount = investerat, unrealizedPnL = orealiserat."""
    cp = pf.get("clientPortfolio") or pf
    positions = cp.get("positions") or []
    pending = cp.get("ordersForOpen") or []
    invested = sum(float(p.get("amount") or 0) for p in positions + pending)
    equity = float(cp.get("credit") or 0) + invested + float(cp.get("unrealizedPnL") or 0)
    return equity, invested, {_iid(p) for p in positions + pending}


def step(cfg: Config, api: Etoro, st: risk.State | None):
    today = dt.date.today().isoformat()
    pf = api.portfolio()
    equity, invested, held = account_numbers(pf)
    # API:et rapporterar 10 000 (betydelse ej bevisad). Riskregler räknas på avsatt kapital.
    log(event="account", api_reported_value=equity, risk_base_usd=cfg.allocated_capital_usd)
    equity = cfg.allocated_capital_usd
    if equity <= 0:
        log(event="stop", reason="kunde inte läsa kontovärde", raw_keys=list(pf)[:10])
        return st
    if st is None or st.day != today:
        st = risk.State(day=today, start_equity=equity)

    for iid in cfg.allowed_instruments:
        closes = api.daily_closes(iid)
        sig = strategy.signal(closes, cfg.fast_ma, cfg.slow_ma)
        log(event="signal", instrument=iid, signal=sig, last=closes[-1])
        if sig != "buy":
            continue
        if iid in held:  # annars köper den igen var 15:e minut så länge signalen står kvar
            log(event="blocked", instrument=iid, reason="har redan position")
            continue
        px = closes[-1]
        sl, tp = px * (1 - cfg.stop_loss_pct), px * (1 + cfg.take_profit_pct)
        fx = api.quote(cfg.fx_instrument)
        fx_time = dt.datetime.fromisoformat(fx["date"]).replace(tzinfo=dt.timezone.utc)
        fx_age = (dt.datetime.now(dt.timezone.utc) - fx_time).total_seconds()
        if fx.get("quoteType") != "realtime" or fx_age > cfg.fx_max_age_s:
            log(event="blocked", instrument=iid, reason="USDSEK saknas eller är gammal", fx=fx)
            continue
        sz = sizing.size_trade(allocated_capital_usd=cfg.allocated_capital_usd,
                               price_usd=px, stop_usd=sl, leverage=cfg.leverage,
                               usdsek_rate=(fx["bid"] + fx["ask"]) / 2, fx_timestamp=fx["date"],
                               fx_source="eToro rates, instrument 58 USDSEK", risk_pct=cfg.risk_per_trade)
        log(event="sizing", instrument=iid, **sz.__dict__)
        if not sz.within_budget:
            log(event="blocked", instrument=iid, reason="förväntad förlust över riskbudget")
            continue
        amount = math.floor(sz.margin_required_usd * 100) / 100  # insats i USD, nedåt till hela cent
        ok, why = risk.check_buy(cfg, st, iid, equity, invested, amount)
        if not ok:
            log(event="blocked", instrument=iid, reason=why)
            continue
        if (cfg.settlement_type, cfg.leverage) not in api.allowed_long(iid):
            log(event="blocked", instrument=iid, reason=f"kontot får inte köpa {cfg.settlement_type} x{cfg.leverage}")
            continue
        if cfg.use_ai_filter and not ai_filter.approve(cfg.ai_model, str(iid), closes, []):
            log(event="ai_veto", instrument=iid)
            continue
        if not cfg.live:
            log(event="DRY_RUN_buy", instrument=iid, amount=amount, leverage=cfg.leverage,
                exposure=amount * cfg.leverage, sl=round(sl, 2), tp=round(tp, 2))
            continue
        if not cfg.real_open_enabled:
            log(event="blocked", instrument=iid, reason="riktiga öppningar spärrade tills valutamodellen är verifierad")
            continue
        res = api.open_buy(iid, amount, sl, tp, cfg.settlement_type, cfg.leverage)
        st.orders_today += 1
        log(event="order", instrument=iid, amount=amount, sl=sl, tp=tp, response=res)
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    cfg = Config()
    api = Etoro(cfg.base_url)
    log(event="start", live=cfg.live, instruments=cfg.allowed_instruments)
    st = None
    while True:
        try:
            st = step(cfg, api, st)
        except Exception as e:  # aldrig krascha tyst, aldrig handla på fel
            log(event="error", error=str(e))
        if args.once:
            break
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
