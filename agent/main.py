"""Start: python -m agent.main [--once]

Standard är EXECUTION_MODE=DRY_RUN. Riktiga ordrar kräver EXECUTION_MODE=REAL_MICRO och att
EXPECTED_PORTFOLIO matchar kontot nycklarna hör till. Ingen fråga per affär efter start.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

from .broker import EtoroBroker
from .config import MODES, Config
from .engine import Engine
from .state import StateStore


def log(**kw):
    print(json.dumps({"t": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **kw},
                     ensure_ascii=False, default=str), flush=True)


def banner(cfg: Config, ident: dict):
    lines = [
        "=" * 60,
        "  RIKTIGA PENGAR  (EXECUTION_MODE=REAL_MICRO)",
        f"  Portfölj:        {ident.get('username')} (gcid {ident.get('gcid')})",
        f"  Instrument:      {cfg.instrument_symbol} CFD (eToro {cfg.instrument}), x{cfg.leverage}",
        f"  Kapital (risk):  {cfg.allocated_capital_sek:,.2f} SEK",
        f"  Risk per affär:  {cfg.risk_per_trade:.2%} = {cfg.allocated_capital_sek * cfg.risk_per_trade:.2f} SEK",
        f"  Max dagsförlust: {cfg.daily_loss_pct:.1%}   Max veckoförlust: {cfg.weekly_loss_pct:.1%}",
        f"  Max exponering:  {cfg.max_exposure_pct:.0%} av kapitalet",
        f"  Kill switch:     skapa filen {cfg.kill_switch_file} eller sätt KILL_SWITCH=1",
        "=" * 60,
    ]
    print("\n".join(lines), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    cfg = Config()
    if cfg.mode not in MODES:
        sys.exit(f"Okänt EXECUTION_MODE={cfg.mode!r}. Tillåtna: {MODES}")
    broker = EtoroBroker(cfg.base_url, os.environ["ETORO_USER_KEY"], os.environ["ETORO_API_KEY"])
    eng = Engine(cfg, broker, StateStore(cfg.state_file),
                 clock=lambda: dt.datetime.now(dt.timezone.utc), sleep=time.sleep, log=log)
    ident = eng.startup()
    if cfg.real:
        if not eng.identity_ok:
            sys.exit(f"Stopp: nycklarna hör till {ident.get('username')!r}, förväntat {cfg.expected_portfolio!r}")
        banner(cfg, ident)
    log(event="start", mode=cfg.mode, portfolio=ident.get("username"), identity_ok=eng.identity_ok,
        instrument=cfg.instrument, eligibility=str(eng.eligibility))
    while True:
        try:
            eng.cycle()
        except Exception as e:  # sista skyddsnätet: logga, vänta, fortsätt
            log(event="error", where="cycle", error=repr(e))
        if args.once:
            break
        time.sleep(cfg.monitor_seconds)


if __name__ == "__main__":
    main()
