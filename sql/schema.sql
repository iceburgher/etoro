-- Guldagenten: all varaktig data. Körs en gång mot Supabase/PostgreSQL (idempotent).

-- Agentens tillstånd, en rad.
create table if not exists agent_state (
    id          int primary key default 1 check (id = 1),
    state       jsonb not null,
    git_sha     text,
    updated_at  timestamptz not null default now()
);

-- Distribuerat lås med lease. Ett jobb i taget får handla; ett kraschat jobbs lås går ut av sig självt.
create table if not exists job_locks (
    name         text primary key,
    holder       text not null,
    acquired_at  timestamptz not null,
    expires_at   timestamptz not null
);

-- Orderavsikter. Skrivs INNAN ordern skickas. Primärnyckeln = idempotensnyckeln = eToros x-request-id,
-- så samma beslut kan aldrig ge två ordrar, oavsett omförsök, timeout eller parallella jobb.
create table if not exists order_intents (
    idempotency_key  text primary key,
    action           text not null check (action in ('OPEN_LONG','CLOSE_LONG','OPEN_SHORT','CLOSE_SHORT')),
    instrument       int  not null,
    status           text not null check (status in ('created','submitted','filled','rejected','unknown','lost','closed')),
    payload          jsonb not null,
    broker_order_id  bigint,
    error            text,
    git_sha          text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

-- Händelselogg och affärslogg (allt som loggas, med git-SHA).
create table if not exists events (
    id       bigserial primary key,
    ts       timestamptz not null default now(),
    job      text,
    event    text not null,
    payload  jsonb,
    git_sha  text
);
create index if not exists events_ts on events (ts);
create index if not exists events_event on events (event);

-- Incidenter (ogiltigt läge m.m.). Öppen incident blockerar nya affärer.
create table if not exists incidents (
    id           bigserial primary key,
    kind         text not null,
    opened_at    timestamptz not null default now(),
    resolved_at  timestamptz,
    details      jsonb
);

-- Mänsklig styrning utan omdeploy.
create table if not exists control (
    id                int primary key default 1 check (id = 1),
    kill_switch       boolean not null default false,
    halt_new_entries  boolean not null default false,
    updated_at        timestamptz not null default now()
);
insert into control (id) values (1) on conflict do nothing;

-- Supabase exponerar public-schemat via sitt REST-API. RLS utan policyer stänger det helt;
-- agenten ansluter direkt som databasägare och påverkas inte.
alter table agent_state   enable row level security;
alter table job_locks     enable row level security;
alter table order_intents enable row level security;
alter table events        enable row level security;
alter table incidents     enable row level security;
alter table control       enable row level security;
