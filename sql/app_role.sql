-- Egen databasroll för agenten (inte postgres-superanvändaren). Rättigheter bara till agentens tabeller.
-- Lösenordet sätts vid körning och lagras bara i Vercels DATABASE_URL:
--   postgresql://guldagenten_app.<project-ref>:<lösenord>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'guldagenten_app') then
    create role guldagenten_app login password '<SÄTT-LÖSENORD>';
  end if;
end $$;
grant usage on schema public to guldagenten_app;
grant select, insert, update, delete on agent_state, job_locks, order_intents, events, incidents, control
  to guldagenten_app;
grant usage, select on all sequences in schema public to guldagenten_app;
-- RLS är på för alla tabeller; rollen får allt, REST-API-rollerna (anon, authenticated) inget.
create policy app_all on agent_state   for all to guldagenten_app using (true) with check (true);
create policy app_all on job_locks     for all to guldagenten_app using (true) with check (true);
create policy app_all on order_intents for all to guldagenten_app using (true) with check (true);
create policy app_all on events        for all to guldagenten_app using (true) with check (true);
create policy app_all on incidents     for all to guldagenten_app using (true) with check (true);
create policy app_all on control       for all to guldagenten_app using (true) with check (true);
