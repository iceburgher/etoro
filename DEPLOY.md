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

## Schemaläggning (UTC) – Supabase pg_cron, inte Vercel Cron

Vercel Hobby tillåter bara dagliga cron-jobb, så jobben väcks från Supabase (`sql/schedule.sql`):
`pg_cron` startar, `pg_net` anropar Vercel-funktionen med `Authorization: Bearer CRON_SECRET`.

| Jobb | Schema | Varför |
| --- | --- | --- |
| `/api/strategy` | `2 0,4,8,12,16,20 * * *` | 2 min efter att en 4H-stapel stängt |
| `/api/monitor` | `* * * * *` | stop/mål, väntande ordrar, nödlägen |

Hemligheterna (deploy-adress, CRON_SECRET, ev. bypass-token) ligger i Supabase Vault, inte i SQL-filen.
Pausa allt: `update cron.job set active = false where jobname like 'guldagenten-%';`.
Byts planen till Vercel Pro kan samma scheman flyttas till `vercel.json` (`crons`).

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

## Uppsatt 30 sep

- **Supabase:** projekt `guldagenten` (ref `hvedgrlyetvtsmtzulvi`, eu-north-1, gratisplan). Schemat är kört,
  RLS på alla tabeller (REST-API:et stängt; agenten ansluter direkt).
- **Resend:** API-nyckel `guldagenten-alerts` (bara sändning). Avsändare `onboarding@resend.dev` tills en egen
  domän verifierats; då kan larm bara gå till Resend-kontots ägaradress. Testlarm skickat.
- **Vercel:** projekt `guldagenten` i teamet Iceburgher's projects, funktionsregion arn1 (Stockholm).
  **Inte** kopplat till GitHub, så inget deployas automatiskt; produktion deployas bara med CLI från en tagg.
  Satta variabler: `EXECUTION_MODE=DRY_RUN`, `REAL_MICRO_INTEGRATION`, `EXPECTED_PORTFOLIO`,
  `ALLOCATED_CAPITAL_SEK`, `KILL_SWITCH`, `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM`, `RESEND_API_KEY`, `CRON_SECRET`.
- **Kvar att lägga in själv (hemligheter):** `ETORO_USER_KEY`, `ETORO_API_KEY` och `DATABASE_URL`
  (Supabase → Connect → Transaction pooler, port 6543, med databaslösenordet; lägg till `?sslmode=require`).
  Sätt dem bara för Production.
- **Vercel-plan:** Hobby. Därför schemaläggs jobben från Supabase (se ovan).
- **Att bekräfta vid första deployen:** att Supabase-anropen kommer fram trots Vercel Authentication
  (deployment protection är på). Antingen skapa en "Protection Bypass for Automation"-token och lägg den i
  Vault som `guldagenten_bypass`, eller stäng av skyddet för Production (funktionerna kräver ändå CRON_SECRET).
- **Hobby-villkor:** Vercel Hobby är avsett för icke-kommersiellt personligt bruk.

## Deploy (aldrig från feature-gren)

1. Granska grenen (PR mot `main`), alla tester gröna: `pip install -r requirements-dev.txt && pytest`
   (med `TEST_DATABASE_URL` mot en testdatabas körs även databastesterna).
2. Merga till `main`.
3. Tagga: `git tag -a v1.1.0 -m "gld-v1.1" && git push origin v1.1.0`.
4. Kör `sql/schema.sql` mot produktionsdatabasen (idempotent).
5. Deploya exakt den taggade commiten till Production:
   `git checkout v1.1.0 && vercel deploy --prod --env GIT_SHA=$(git rev-parse HEAD)`.
   (Projektet är inte Git-kopplat, så Vercel sätter ingen commit-SHA själv; `GIT_SHA` gör det.)
   Kontrollera att SHA:n i loggarna = taggens (sparas i `agent_state.git_sha`, `order_intents.git_sha`,
   `events.git_sha`).
5b. Första gången: lägg hemligheterna i Vault och kör `sql/schedule.sql` i Supabase. Kontrollera med
   `select status_code, content from net._http_response order by created desc limit 5;` att svaren är 200.
6. Starta i `EXECUTION_MODE=DRY_RUN` i minst 2 veckor och tills tillräckligt många signaler/livscykler
   har passerat. Inga strategiändringar under perioden.
7. Integrationsaffär: sätt `EXECUTION_MODE=REAL_MICRO`, `REAL_MICRO_INTEGRATION=1`, deploya om samma tagg.
   Efter första fyllningen stoppas nya öppningar automatiskt. Kör `python -m agent.report` och granska.
8. Först efter godkänd rapport: sätt `REAL_MICRO_INTEGRATION=0` i Vercel, deploya om samma tagg och kör
   `update control set halt_new_entries = false;`. Så länge `REAL_MICRO_INTEGRATION=1` blockerar riskmotorn
   alla nya öppningar när en riktig öppning finns i `order_intents`, oavsett halt-flaggan.
