# Guldagenten – drift på Vercel

## Struktur

```
api/strategy.py      Vercel-funktion: STRATEGY_JOB (cron efter varje 4H-stapel)
api/monitor.py       Vercel-funktion: BROKER_MONITOR_JOB (cron varje minut)
vercel.json          cron-scheman och maxDuration 60 s
agent/jobs.py        ingång: CRON_SECRET, bygger motor, fångar ohanterade fel
agent/engine.py      motorn: lås, avstämning, strategi, beslut, incidenter, larm
agent/risk_engine.py riskregler (OPEN: full lista, CLOSE: bara position/riktning/antal)
agent/execution.py   order: avsikt i DB -> x-request-id -> verifiera fyllning/stängning
agent/store.py       PgStore (Supabase/PostgreSQL) och MemoryStore (tester)
agent/alerts.py      larm: e-post via Resend, utbytbar leverantör, fel sväljs
agent/strategy_v1.py strategi gld-v1.1 (fryst)
agent/sizing.py      SEK-kapital -> USD-risk -> enheter (nedåt) -> förlust i USD och SEK
agent/broker.py      eToro-API
agent/report.py      rapport efter integrationsaffären
sql/schema.sql       databasschema
research/backtest.py backtest
```

## Cron (UTC)

| Jobb | Schema | Varför |
| --- | --- | --- |
| `/api/strategy` | `2 0,4,8,12,16,20 * * *` | 2 min efter att en 4H-stapel stängt |
| `/api/monitor` | `* * * * *` | stop/mål, väntande ordrar, nödlägen |

Vercel kör cron bara mot **Production**-deployen. Cron varje minut kräver Vercel Pro.

## Miljövariabler

| Variabel | Production | Preview |
| --- | --- | --- |
| `ETORO_USER_KEY`, `ETORO_API_KEY` | ja | ja (läsning) |
| `DATABASE_URL` | Supabase (egen databas) | separat databas, aldrig produktionens |
| `CRON_SECRET` | ja | ja |
| `EXECUTION_MODE` | `DRY_RUN` under torrkörningen, sedan `REAL_MICRO` | `DRY_RUN` (koden tvingar det ändå) |
| `REAL_MICRO_INTEGRATION` | `1` (stoppar nya öppningar efter första fyllning) | – |
| `EXPECTED_PORTFOLIO` | `AI Burger-UMYYUR` | samma |
| `ALLOCATED_CAPITAL_SEK` | `9983.59` | samma |
| `RESEND_API_KEY`, `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM` | ja (utan dem blockeras riktiga öppningar) | valfritt |
| `KILL_SWITCH` | `0` | – |

Kill switch utan omdeploy: `update control set kill_switch = true;` i Supabase.
Efter integrationsaffären: `update control set halt_new_entries = false;` först när rapporten är godkänd.

## Deploy (aldrig från feature-gren)

1. Granska grenen (PR mot `main`), alla tester gröna: `pip install -r requirements-dev.txt && pytest`
   (med `TEST_DATABASE_URL` mot en testdatabas körs även databastesterna).
2. Merga till `main`.
3. Tagga: `git tag -a v1.1.0 -m "gld-v1.1" && git push origin v1.1.0`.
4. Kör `sql/schema.sql` mot produktionsdatabasen (idempotent).
5. Deploya exakt den taggade commiten till Production: `git checkout v1.1.0 && vercel deploy --prod`.
   Kontrollera att `VERCEL_GIT_COMMIT_SHA` i loggarna = taggens SHA (sparas i `agent_state.git_sha`,
   `order_intents.git_sha` och `events.git_sha`).
6. Starta i `EXECUTION_MODE=DRY_RUN` i minst 2 veckor och tills tillräckligt många signaler/livscykler
   har passerat. Inga strategiändringar under perioden.
7. Integrationsaffär: sätt `EXECUTION_MODE=REAL_MICRO`, `REAL_MICRO_INTEGRATION=1`, deploya om samma tagg.
   Efter första fyllningen stoppas nya öppningar automatiskt. Kör `python -m agent.report` och granska.
8. Först efter godkänd rapport: `update control set halt_new_entries = false;`.
