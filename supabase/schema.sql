-- Messages table: stores conversation history per user
create table if not exists messages (
    id bigint generated always as identity primary key,
    user_id bigint not null,
    role text not null check (role in ('user', 'assistant', 'tool')),
    content text not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_messages_user_id_created on messages (user_id, created_at desc);

-- Reminders table: stores scheduled reminders per user
create table if not exists reminders (
    id bigint generated always as identity primary key,
    user_id bigint not null,
    message text not null,
    remind_at timestamptz not null,
    sent boolean not null default false,
    created_at timestamptz not null default now()
);

create index if not exists idx_reminders_pending on reminders (remind_at) where sent = false;

-- User profiles: persistent knowledge about each user
create table if not exists user_profiles (
    user_id bigint primary key,
    profile text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Dynamic skills: LLM-generated tools stored in the DB
create table if not exists dynamic_skills (
    id bigint generated always as identity primary key,
    name text unique not null,
    description text not null,
    parameters jsonb not null default '{}',
    code text not null,
    enabled boolean not null default true,
    status text not null default 'pending' check (status in ('pending', 'approved')),
    created_by bigint not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- RLS enabled on all tables. service_role bypasses RLS; anon is blocked.
alter table messages enable row level security;
alter table reminders enable row level security;
alter table user_profiles enable row level security;
alter table dynamic_skills enable row level security;

create policy "Deny anon access to messages" on messages for all to anon using (false);
create policy "Deny anon access to reminders" on reminders for all to anon using (false);
create policy "Deny anon access to user_profiles" on user_profiles for all to anon using (false);
create policy "Deny anon access to dynamic_skills" on dynamic_skills for all to anon using (false);
