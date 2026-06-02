create table if not exists public.agentic_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  email text,
  lemon_customer_id text,
  lemon_subscription_id text not null unique,
  lemon_order_id text,
  variant_id text,
  plan text not null default 'pro',
  status text not null default 'inactive',
  renews_at timestamptz,
  ends_at timestamptz,
  raw_event_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists agentic_subscriptions_user_id_idx
  on public.agentic_subscriptions(user_id);

create index if not exists agentic_subscriptions_status_idx
  on public.agentic_subscriptions(status);

create table if not exists public.agentic_usage_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  build_status text not null,
  tool_capability_tier text not null default 'unknown',
  file_count integer not null default 0,
  artifact_count integer not null default 0,
  run_seconds integer,
  created_at timestamptz not null default now()
);

create index if not exists agentic_usage_events_user_id_idx
  on public.agentic_usage_events(user_id);

alter table public.agentic_subscriptions enable row level security;
alter table public.agentic_usage_events enable row level security;

drop policy if exists "Users can read own AgentIC subscription" on public.agentic_subscriptions;
create policy "Users can read own AgentIC subscription"
  on public.agentic_subscriptions
  for select
  to authenticated
  using (auth.uid() = user_id);

drop policy if exists "Users can read own AgentIC usage" on public.agentic_usage_events;
create policy "Users can read own AgentIC usage"
  on public.agentic_usage_events
  for select
  to authenticated
  using (auth.uid() = user_id);
