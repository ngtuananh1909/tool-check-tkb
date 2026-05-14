#!/usr/bin/env python3
"""Drop old constraint from elearning_deadlines table."""
import os
from dotenv import load_dotenv
load_dotenv()

from supabase import create_client, Client

url = os.environ.get('SUPABASE_URL', 'https://cnmvukglrzbumhcwpfxj.supabase.co/rest/v1/').rstrip('/rest/v1')
key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
client = create_client(url, key)

# Drop old constraint via raw SQL using raw_query
try:
    # Use raw_query method
    result = client.raw_query('''
alter table public.elearning_deadlines
    drop constraint if exists uq_elearning_deadlines_course;

alter table public.elearning_deadlines
    drop constraint if exists uq_elearning_deadlines;
''').execute()
    print('Success:', result)
except Exception as e:
    print(f'Error: {e}')
    print('Manual SQL required: Run the following in Supabase SQL Editor:')
    print('''
alter table public.elearning_deadlines
    drop constraint if exists uq_elearning_deadlines_course;

alter table public.elearning_deadlines
    drop constraint if exists uq_elearning_deadlines;
''')
