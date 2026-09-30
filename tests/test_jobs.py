from agent import jobs


def test_cron_secret_required(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    assert jobs.handle("monitor", None)[0] == 401
    assert jobs.handle("monitor", "Bearer fel")[0] == 401
    monkeypatch.delenv("CRON_SECRET")
    assert jobs.handle("monitor", "Bearer s3cret")[0] == 401     # ingen hemlighet konfigurerad = stängt


def test_unknown_job(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s")
    assert jobs.handle("buy_now", "Bearer s")[0] == 404


def test_unhandled_job_failure_returns_500_and_critical_alert(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s")
    sent = []

    class A:
        provider = None

        def __call__(self, level, kind, message, **d):
            sent.append((level, kind))
    monkeypatch.setattr(jobs, "build_alerter", lambda: A())

    def boom(cfg, alert):
        raise RuntimeError("kallstart misslyckades")
    monkeypatch.setattr(jobs, "build_engine", boom)
    code, body = jobs.handle("strategy", "Bearer s")
    assert code == 500 and ("critical", "job_failure") in sent
