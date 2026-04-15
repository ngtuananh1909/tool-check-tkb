-- Supabase bootstrap script for schedule bot + personal appointments
-- Run this in Supabase SQL Editor.

begin;

-- ---------------------------------------------------------------------------
-- 1) Weekly class schedule table (kept for compatibility)
-- ---------------------------------------------------------------------------
create table if not exists public.schedules (
    id bigserial primary key,
    student_id text not null,
    subject_name text not null,
    room text,
    day_of_week text not null,
    start_period integer not null,
    end_period integer not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_schedules_student_day
    on public.schedules (student_id, day_of_week, start_period);

-- ---------------------------------------------------------------------------
-- 2) Personal appointments table
-- ---------------------------------------------------------------------------
create table if not exists public.appointments (
    id bigserial primary key,
    student_id text not null,
    title text not null,
    appointment_date date not null,
    start_time time,
    end_time time,
    location text,
    note text,
    raw_user_input text,
    gemini_confidence double precision,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint chk_appointments_time_range
        check (end_time is null or start_time is null or end_time >= start_time)
);

create index if not exists idx_appointments_student_date
    on public.appointments (student_id, appointment_date, start_time);

-- ---------------------------------------------------------------------------
-- 3) Actual class sessions (per-week concrete instances)
-- ---------------------------------------------------------------------------
create table if not exists public.class_sessions (
    id bigserial primary key,
    student_id text not null,
    session_date date not null,
    subject_name text not null,
    room text,
    start_period integer not null,
    end_period integer not null,
    start_time time,
    end_time time,
    status text not null default 'scheduled',
    source_signature text not null,
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint chk_class_sessions_time_range
        check (end_time is null or start_time is null or end_time >= start_time),
    constraint chk_class_sessions_period_range
        check (start_period >= 1 and end_period >= start_period),
    constraint chk_class_sessions_status
        check (status in ('scheduled', 'makeup', 'absent', 'cancelled', 'moved')),
    constraint uq_class_sessions_signature unique (student_id, source_signature)
);

create index if not exists idx_class_sessions_student_date
    on public.class_sessions (student_id, session_date, start_time);

create index if not exists idx_class_sessions_student_status
    on public.class_sessions (student_id, status, session_date);

create index if not exists idx_class_sessions_student_signature
    on public.class_sessions (student_id, source_signature);

-- ---------------------------------------------------------------------------
-- 4) Calendar sync state
-- ---------------------------------------------------------------------------
create table if not exists public.calendar_sync_state (
    id bigserial primary key,
    student_id text not null,
    source_type text not null,
    source_key text not null,
    source_hash text not null,
    uploaded boolean not null default false,
    calendar_event_id text,
    calendar_event_link text,
    calendar_synced_at timestamptz,
    last_seen_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint uq_calendar_sync_state unique (student_id, source_type, source_key)
);

create index if not exists idx_calendar_sync_state_student_uploaded
    on public.calendar_sync_state (student_id, uploaded, source_type);

-- ---------------------------------------------------------------------------
-- 5) Notification audit log
-- ---------------------------------------------------------------------------
create table if not exists public.notification_log (
    id bigserial primary key,
    student_id text not null,
    notification_type text not null,
    telegram_message_id text,
    status text not null default 'sent',
    payload jsonb,
    error_message text,
    sent_at timestamptz not null default now()
);

create index if not exists idx_notification_log_student_sent_at
    on public.notification_log (student_id, sent_at desc);

-- ---------------------------------------------------------------------------
-- 6) Triggers for updated_at
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_appointments_set_updated_at on public.appointments;
create trigger trg_appointments_set_updated_at
before update on public.appointments
for each row
execute function public.set_updated_at();

drop trigger if exists trg_class_sessions_set_updated_at on public.class_sessions;
create trigger trg_class_sessions_set_updated_at
before update on public.class_sessions
for each row
execute function public.set_updated_at();

drop trigger if exists trg_calendar_sync_state_set_updated_at on public.calendar_sync_state;
create trigger trg_calendar_sync_state_set_updated_at
before update on public.calendar_sync_state
for each row
execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- 7) RLS baseline (optional but recommended)
-- Service role can still bypass RLS for backend jobs.
-- ---------------------------------------------------------------------------
alter table public.schedules enable row level security;
alter table public.appointments enable row level security;
alter table public.class_sessions enable row level security;
alter table public.calendar_sync_state enable row level security;
alter table public.notification_log enable row level security;

-- Read-only policy for authenticated users (adjust later if needed).
do $$
begin
    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'schedules'
          and policyname = 'authenticated_read_schedules'
    ) then
        create policy authenticated_read_schedules
            on public.schedules
            for select
            to authenticated
            using (true);
    end if;

    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'appointments'
          and policyname = 'authenticated_read_appointments'
    ) then
        create policy authenticated_read_appointments
            on public.appointments
            for select
            to authenticated
            using (true);
    end if;

    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'class_sessions'
          and policyname = 'authenticated_read_class_sessions'
    ) then
        create policy authenticated_read_class_sessions
            on public.class_sessions
            for select
            to authenticated
            using (true);
    end if;

    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'calendar_sync_state'
          and policyname = 'authenticated_read_calendar_sync_state'
    ) then
        create policy authenticated_read_calendar_sync_state
            on public.calendar_sync_state
            for select
            to authenticated
            using (true);
    end if;

    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'notification_log'
          and policyname = 'authenticated_read_notification_log'
    ) then
        create policy authenticated_read_notification_log
            on public.notification_log
            for select
            to authenticated
            using (true);
    end if;
end;
$$;

commit;
