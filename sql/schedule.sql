-- Schemaläggning i Supabase (fungerar på Vercel Hobby, som bara tillåter dagliga Vercel-cron).
-- pg_cron väcker jobben, pg_net gör HTTP-anropet till Vercel med CRON_SECRET.
-- Körs efter första deployen, när deploy-adressen är känd. Tider i UTC.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- 1) Hemligheter i Supabase Vault (en gång; byt värdena). Bypass-token behövs bara om Vercel
--    Authentication är på för Production (Vercel → Settings → Deployment Protection → Protection Bypass
--    for Automation). Annars: lämna tomt.
-- select vault.create_secret('https://guldagenten.vercel.app', 'guldagenten_base_url');
-- select vault.create_secret('<CRON_SECRET>', 'guldagenten_cron_secret');
-- select vault.create_secret('', 'guldagenten_bypass');

-- 2) Anropsfunktion. Bara databasägaren får köra den.
create or replace function public.guldagenten_call(job text) returns bigint
language sql security definer set search_path = public, extensions as $$
  select net.http_get(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'guldagenten_base_url')
           || '/api/' || job,
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets
                                     where name = 'guldagenten_cron_secret'),
      'x-vercel-protection-bypass', coalesce((select decrypted_secret from vault.decrypted_secrets
                                              where name = 'guldagenten_bypass'), '')),
    timeout_milliseconds := 60000
  );
$$;
revoke all on function public.guldagenten_call(text) from public, anon, authenticated;

-- 3) Scheman. Överlappande anrop är ofarliga: databaslåset släpper bara in ett jobb i taget.
select cron.schedule('guldagenten-monitor',  '* * * * *',              $$select public.guldagenten_call('monitor')$$);
select cron.schedule('guldagenten-strategy', '2 0,4,8,12,16,20 * * *', $$select public.guldagenten_call('strategy')$$);

-- Pausa allt:      update cron.job set active = false where jobname like 'guldagenten-%';
-- Återuppta:       update cron.job set active = true  where jobname like 'guldagenten-%';
-- Senaste svar:    select status_code, content, created from net._http_response order by created desc limit 20;
-- Körhistorik:     select * from cron.job_run_details order by start_time desc limit 20;
