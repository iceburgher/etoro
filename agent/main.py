"""Lokal körning av ett jobb (alltid DRY_RUN utanför Vercel Production).

python -m agent.main --job monitor|strategy
Med DATABASE_URL används PostgreSQL, annars minnet (inget sparas mellan körningar).
"""
import argparse
import datetime as dt
import os
import time

from .config import Config
from .engine import Engine
from .jobs import JOBS, build_alerter, log
from .broker import EtoroBroker
from .store import MemoryStore, PgStore


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", choices=JOBS, default="monitor")
    args = ap.parse_args()
    cfg = Config()
    store = PgStore(os.environ["DATABASE_URL"]) if os.getenv("DATABASE_URL") else MemoryStore()
    broker = EtoroBroker(cfg.base_url, os.environ["ETORO_USER_KEY"], os.environ["ETORO_API_KEY"])
    eng = Engine(cfg, broker, store, clock=lambda: dt.datetime.now(dt.timezone.utc), sleep=time.sleep,
                 log=log, alert=build_alerter())
    log(event="local_run", job=args.job, mode=cfg.effective_mode, result=eng.run_job(args.job))


if __name__ == "__main__":
    main()
