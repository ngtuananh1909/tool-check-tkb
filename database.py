"""
database.py
-----------
Supabase integration for storing and querying the student timetable.

Required environment variables:
    SUPABASE_URL  – project URL (e.g. https://xxxx.supabase.co)
    SUPABASE_KEY  – service-role or anon key

Target table schema (`schedules`):
    student_id    TEXT
    subject_name  TEXT
    room          TEXT
    day_of_week   TEXT
    start_period  TEXT
    end_period    TEXT
"""

import logging
import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logger = logging.getLogger(__name__)

_TABLE = "schedules"


def _get_client() -> Client:
    """Create and return an authenticated Supabase client."""
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def upsert_schedule(schedule: list[dict]) -> None:
    """
    Replace the stored schedule for the student with fresh data.

    Steps:
        1. Delete all existing rows for this student_id.
        2. Insert the new schedule rows in bulk.

    Args:
        schedule: List of schedule dicts produced by the crawler module.
                  Each dict must contain 'student_id' as a key.
    """
    if not schedule:
        logger.warning("Empty schedule received – nothing to upsert.")
        return

    client = _get_client()
    student_id = schedule[0]["student_id"]

    # Step 1 – Clear existing schedule for this student
    delete_response = (
        client.table(_TABLE).delete().eq("student_id", student_id).execute()
    )
    logger.info(
        "Deleted existing schedule rows for student %s: %s",
        student_id,
        delete_response,
    )

    # Step 2 – Insert fresh schedule data
    insert_response = client.table(_TABLE).insert(schedule).execute()
    logger.info(
        "Inserted %d schedule rows for student %s",
        len(schedule),
        student_id,
    )
    return insert_response


def get_todays_classes(student_id: str, day_of_week: str) -> list[dict]:
    """
    Query the database for today's classes for a given student.

    Args:
        student_id:  The student's ID used to filter rows.
        day_of_week: Vietnamese day string, e.g. 'Thứ 2', 'Thứ 3', …

    Returns:
        List of schedule dicts for today, sorted by start_period.
    """
    client = _get_client()

    response = (
        client.table(_TABLE)
        .select("*")
        .eq("student_id", student_id)
        .eq("day_of_week", day_of_week)
        .order("start_period")
        .execute()
    )

    classes = response.data or []
    logger.info(
        "Found %d class(es) for student %s on %s",
        len(classes),
        student_id,
        day_of_week,
    )
    return classes
