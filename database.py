"""
database.py – Supabase integration for storing and querying schedule data.

Required environment variables:
    SUPABASE_URL  – Your project URL, e.g. https://xxxx.supabase.co
    SUPABASE_KEY  – Service-role or anon key with INSERT / DELETE permissions
    STUDENT_ID    – Used to scope queries to the current student

Target table schema (SQL):
    CREATE TABLE schedules (
        id            BIGSERIAL PRIMARY KEY,
        student_id    TEXT        NOT NULL,
        subject_name  TEXT        NOT NULL,
        room          TEXT,
        day_of_week   TEXT        NOT NULL,  -- e.g. "Monday"
        start_period  INTEGER     NOT NULL,
        end_period    INTEGER     NOT NULL
    );
"""

import logging
import os
import base64
import json
import datetime
import socket
import time

import httpx
from postgrest.exceptions import APIError
from supabase import create_client, Client

from time_utils import local_today

logger = logging.getLogger(__name__)

TABLE_NAME = "schedules"
APPOINTMENTS_TABLE = "appointments"
_RETRYABLE_NETWORK_ERRORS = (httpx.RequestError, OSError, socket.gaierror)


def _execute_with_retry(operation_name: str, action, max_attempts: int = 3):
    """Run a Supabase action with simple exponential backoff for network errors."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return action()
        except _RETRYABLE_NETWORK_ERRORS as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = 2 ** (attempt - 1)
            logger.warning(
                "%s failed due to network issue (%s). Retrying in %ss (%d/%d).",
                operation_name,
                exc,
                delay,
                attempt,
                max_attempts,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


def _decode_jwt_role(jwt_token: str) -> str | None:
    """Best-effort decode of JWT role claim; returns None if token is not JWT."""
    try:
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return None

        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
        data = json.loads(decoded)
        role = data.get("role")
        return str(role) if role else None
    except Exception:
        return None


def _resolve_supabase_key(for_write: bool) -> str:
    """Resolve the Supabase API key with write-safe priority order."""
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    standard_key = os.environ.get("SUPABASE_KEY")

    key = service_role_key or standard_key
    if not key:
        raise KeyError("SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY must be set.")

    if for_write and not service_role_key:
        role = _decode_jwt_role(key)
        if role == "anon":
            logger.warning(
                "Using anon SUPABASE_KEY for write operation. "
                "This usually fails with RLS (42501). "
                "Set SUPABASE_SERVICE_ROLE_KEY for server-side writes."
            )

    return key


def _get_client(for_write: bool = False) -> Client:
    """Create and return a Supabase client using env vars."""
    url = os.environ["SUPABASE_URL"]
    key = _resolve_supabase_key(for_write=for_write)
    return create_client(url, key)


def upsert_schedule(schedule: list[dict], student_id: str | None = None) -> None:
    """
    Replace the existing schedule for *student_id* with *schedule*.

    The operation is:
        1. DELETE all rows where student_id = <student_id>
        2. INSERT the new rows

    Parameters
    ----------
    schedule : list[dict]
        List of schedule entry dicts as returned by ``crawler.fetch_schedule``.
    student_id : str, optional
        Overrides the STUDENT_ID environment variable.

    Raises
    ------
    ValueError
        If student_id cannot be determined.
    RuntimeError
        If the Supabase operation fails.
    """
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    if not schedule:
        logger.warning("Empty schedule provided; skipping database update.")
        return

    client = _get_client(for_write=True)

    # -------------------------------------------------------------------------
    # Step 1 – Clear existing schedule for this student
    # -------------------------------------------------------------------------
    logger.info("Clearing existing schedule for student_id=%s", sid)
    try:
        delete_resp = _execute_with_retry(
            "Supabase schedule delete",
            lambda: (
                client.table(TABLE_NAME)
                .delete()
                .eq("student_id", sid)
                .execute()
            ),
        )
        logger.debug("Delete response: %s", delete_resp)

        # ---------------------------------------------------------------------
        # Step 2 – Insert fresh schedule rows
        # ---------------------------------------------------------------------
        logger.info("Inserting %d new schedule rows for student_id=%s", len(schedule), sid)
        insert_resp = _execute_with_retry(
            "Supabase schedule insert",
            lambda: (
                client.table(TABLE_NAME)
                .insert(schedule)
                .execute()
            ),
        )
        logger.debug("Insert response: %s", insert_resp)
    except APIError as exc:
        detail = str(exc)
        if "42501" in detail or "row-level security" in detail.lower():
            raise RuntimeError(
                "Supabase write blocked by RLS. "
                "Fix by setting SUPABASE_SERVICE_ROLE_KEY for backend writes, "
                "or create INSERT/DELETE policies on table 'schedules'. "
                f"Original error: {exc}"
            ) from exc
        raise

    logger.info("Schedule updated successfully in Supabase.")


def get_today_schedule(student_id: str | None = None, day_of_week: str | None = None) -> list[dict]:
    """
    Query the Supabase ``schedules`` table and return classes for *today*.

    Parameters
    ----------
    student_id : str, optional
        Overrides the STUDENT_ID environment variable.
    day_of_week : str, optional
        English weekday name (e.g. "Monday"). Defaults to today's weekday
        in the configured application timezone.

    Returns
    -------
    list[dict]
        Rows from the ``schedules`` table, sorted by start_period ascending.
    """
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    if day_of_week is None:
        day_of_week = local_today().strftime("%A")  # e.g. "Monday"

    logger.info("Querying schedule for student_id=%s, day=%s", sid, day_of_week)

    client = _get_client(for_write=False)
    response = _execute_with_retry(
        "Supabase schedule query",
        lambda: (
            client.table(TABLE_NAME)
            .select("subject_name, room, day_of_week, start_period, end_period")
            .eq("student_id", sid)
            .eq("day_of_week", day_of_week)
            .order("start_period", desc=False)
            .execute()
        ),
    )

    rows: list[dict] = response.data or []
    logger.info("Found %d classes today (%s).", len(rows), day_of_week)
    return rows


def create_appointment(
    title: str,
    appointment_date: datetime.date | str,
    student_id: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    location: str | None = None,
    note: str | None = None,
    raw_user_input: str | None = None,
    gemini_confidence: float | None = None,
) -> dict:
    """Create a personal appointment row in Supabase."""
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    if not title or not title.strip():
        raise ValueError("title is required.")

    if isinstance(appointment_date, datetime.date):
        date_value = appointment_date.isoformat()
    else:
        date_value = str(appointment_date).strip()

    payload: dict = {
        "student_id": sid,
        "title": title.strip(),
        "appointment_date": date_value,
        "start_time": start_time,
        "end_time": end_time,
        "location": location,
        "note": note,
        "raw_user_input": raw_user_input,
        "gemini_confidence": gemini_confidence,
    }

    client = _get_client(for_write=True)
    try:
        response = _execute_with_retry(
            "Supabase appointment insert",
            lambda: (
                client.table(APPOINTMENTS_TABLE)
                .insert(payload)
                .execute()
            ),
        )
    except APIError as exc:
        detail = str(exc)
        if "42501" in detail or "row-level security" in detail.lower():
            raise RuntimeError(
                "Supabase write blocked by RLS for appointments. "
                "Set SUPABASE_SERVICE_ROLE_KEY or create INSERT policy on 'appointments'."
            ) from exc
        raise

    rows: list[dict] = response.data or []
    created = rows[0] if rows else payload
    logger.info("Created appointment for student_id=%s on %s", sid, date_value)
    return created


def get_today_appointments(
    student_id: str | None = None,
    target_date: datetime.date | None = None,
) -> list[dict]:
    """Return appointments for the given date (default: today)."""
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    date_value = (target_date or local_today()).isoformat()
    logger.info("Querying appointments for student_id=%s, date=%s", sid, date_value)

    client = _get_client(for_write=False)
    try:
        response = _execute_with_retry(
            "Supabase appointment query",
            lambda: (
                client.table(APPOINTMENTS_TABLE)
                .select("id, title, appointment_date, start_time, end_time, location, note")
                .eq("student_id", sid)
                .eq("appointment_date", date_value)
                .order("start_time", desc=False)
                .execute()
            ),
        )
    except APIError as exc:
        detail = str(exc)
        if "PGRST205" in detail or "Could not find the table" in detail:
            logger.warning(
                "Table '%s' is not available yet. Run supabase/init_tables.sql to enable appointments.",
                APPOINTMENTS_TABLE,
            )
            return []
        raise

    rows: list[dict] = response.data or []
    logger.info("Found %d appointment(s) on %s.", len(rows), date_value)
    return rows
