-- Run once in Supabase SQL Editor to allow multiple eLearning deadlines per course.

begin;

alter table public.elearning_deadlines
    drop constraint if exists uq_elearning_deadlines_course;

alter table public.elearning_deadlines
    drop constraint if exists uq_elearning_deadlines;

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'uq_elearning_deadlines_signature'
          and conrelid = 'public.elearning_deadlines'::regclass
    ) then
        alter table public.elearning_deadlines
            add constraint uq_elearning_deadlines_signature
            unique (student_id, source_signature);
    end if;
end $$;

commit;
