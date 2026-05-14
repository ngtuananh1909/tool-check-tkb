-- Drop old constraint and create new one based on source_signature
-- Run this in Supabase SQL Editor

begin;

-- Drop old constraint if exists
alter table public.elearning_deadlines
    drop constraint if exists uq_elearning_deadlines_course;

alter table public.elearning_deadlines
    drop constraint if exists uq_elearning_deadlines;

-- Create new unique constraint based on source_signature
alter table public.elearning_deadlines
    add constraint uq_elearning_deadlines_signature
    unique (student_id, source_signature);

commit;
