"""Ingång för Vercel-jobben. Bygger motorn från miljövariabler, kontrollerar CRON_SECRET och fångar allt."""
import datetime as dt
import json
import os
import time

from .alerts import CRITICAL, ResendEmailAlerter, SafeAlerter
from .broker import EtoroBroker
from .config import Config
from .engine import MONITOR_JOB, STRATEGY_JOB, Engine
from .store import PgStore

JOBS = (STRATEGY_JOB, MONITOR_JOB)


def log(**kw):
    print(json.dumps({"t": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **kw},
                     ensure_ascii=False, default=str), flush=True)


def build_alerter() -> SafeAlerter:
    key, to = os.getenv("RESEND_API_KEY"), os.getenv("ALERT_EMAIL_TO")
    provider = ResendEmailAlerter(key, os.getenv("ALERT_EMAIL_FROM", "guldagenten@resend.dev"),
                                  [a.strip() for a in to.split(",")]) if key and to else None
    return SafeAlerter(provider, log)


def build_engine(cfg: Config, alert: SafeAlerter) -> Engine:
    broker = EtoroBroker(cfg.base_url, os.environ["ETORO_USER_KEY"], os.environ["ETORO_API_KEY"])
    store = PgStore(os.environ["DATABASE_URL"])
    eng = Engine(cfg, broker, store, clock=lambda: dt.datetime.now(dt.timezone.utc), sleep=time.sleep,
                 log=log, alert=alert)
    eng.alerts_ready = alert.provider is not None
    return eng


def handle(job: str, authorization: str | None) -> tuple[int, dict]:
    secret = os.getenv("CRON_SECRET")
    if not secret or authorization != f"Bearer {secret}":
        return 401, {"error": "unauthorized"}
    if job not in JOBS:
        return 404, {"error": "unknown job"}
    alert = build_alerter()
    cfg = Config()
    try:
        eng = build_engine(cfg, alert)
        result = eng.run_job(job)
        return 200, {"job": job, "result": result, "mode": cfg.effective_mode, "git_sha": cfg.git_sha}
    except Exception as e:  # ohanterat fel i jobbet: kritiskt larm, 500 så att Vercel visar felet
        alert(CRITICAL, "job_failure", f"Ohanterat fel i {job}", error=repr(e), git_sha=cfg.git_sha)
        return 500, {"job": job, "error": repr(e)}
