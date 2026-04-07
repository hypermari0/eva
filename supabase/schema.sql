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

-- Enable RLS (optional — disable if using service key)
-- alter table messages enable row level security;
-- alter table reminders enable row level security;
