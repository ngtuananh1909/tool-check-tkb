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
-- 3) Notification audit log
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
-- 4) Trigger for appointments.updated_at
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

-- ---------------------------------------------------------------------------
-- 5) RLS baseline (optional but recommended)
-- Service role can still bypass RLS for backend jobs.
-- ---------------------------------------------------------------------------
alter table public.schedules enable row level security;
alter table public.appointments enable row level security;
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
