"""Rapport efter integrationsaffären: python -m agent.report (kräver DATABASE_URL).

Samlar det som ska verifieras: orderstorlek, fyllning, position, stop/mål, stängning, PnL, avgifter,
finansiering, valuta och vad API-värdet 10 000 verkar vara. Avgifter/finansiering läses ur eventloggen
och måste jämföras manuellt med eToros kontoutdrag.
"""
import json
import os

import psycopg

EVENTS = ("order_submitted", "order_filled", "reconciled_fill", "close_submitted", "close_verified",
          "close_reconciled", "broker_closed", "cycle", "alert")


def main():
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute("select idempotency_key, action, status, payload, broker_order_id, error, git_sha, created_at "
                    "from order_intents order by created_at")
        intents = [dict(zip(("key", "action", "status", "payload", "broker_order_id", "error", "git_sha", "at"), r))
                   for r in cur.fetchall()]
        cur.execute("select ts, job, event, payload, git_sha from events where event = any(%s) order by ts",
                    (list(EVENTS),))
        events = [dict(zip(("ts", "job", "event", "payload", "git_sha"), r)) for r in cur.fetchall()]
    cycles = [e for e in events if e["event"] == "cycle"]
    api_values = sorted({e["payload"].get("api_reported_value") for e in cycles if e["payload"]}, key=str)
    report = {
        "order_intents": intents,
        "trade_events": [e for e in events if e["event"] != "cycle"],
        "api_reported_value_seen": api_values,
        "att_verifiera_manuellt": [
            "faktisk fyllningskurs och enheter mot eToro-appen",
            "stop och mål på positionen i appen",
            "realiserad PnL i USD och SEK mot kontoutdrag",
            "avgift: dras den vid öppning, stängning eller båda (uppdatera research/backtest.py)",
            "nattavgift om positionen hölls över natten",
            "hur API-värdet 10 000 ändrades av affären jämfört med kapitalet i appen",
        ],
    }
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
