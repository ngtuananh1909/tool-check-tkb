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
import hashlib
import socket
import time

import httpx
from postgrest.exceptions import APIError
from supabase import create_client, Client

from time_utils import local_today

logger = logging.getLogger(__name__)

TABLE_NAME = "schedules"
APPOINTMENTS_TABLE = "appointments"
CLASS_SESSIONS_TABLE = "class_sessions"
CALENDAR_SYNC_STATE_TABLE = "calendar_sync_state"
_RETRYABLE_NETWORK_ERRORS = (httpx.RequestError, OSError, socket.gaierror)

_PERIOD_START: dict[int, str] = {
    1: "07:00",
    2: "07:50",
    3: "08:40",
    4: "09:30",
    5: "10:20",
    6: "11:10",
    7: "12:30",
    8: "13:20",
    9: "14:10",
    10: "15:00",
    11: "15:50",
    12: "16:40",
    13: "17:30",
    14: "18:00",
    15: "18:50",
    16: "19:40",
}

_WEEKDAY_TO_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


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

    schedule_rows = _normalize_schedule_rows(schedule, student_id=sid)
    if not schedule_rows:
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
        logger.info("Inserting %d new schedule rows for student_id=%s", len(schedule_rows), sid)
        insert_resp = _execute_with_retry(
            "Supabase schedule insert",
            lambda: (
                client.table(TABLE_NAME)
                .insert(schedule_rows)
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


def upsert_actual_class_sessions(schedule_rows: list[dict], student_id: str | None = None) -> int:
    """Upsert concrete class sessions from crawler rows containing session_date."""
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    payload_rows: list[dict] = []
    signatures: set[str] = set()
    session_dates: list[datetime.date] = []

    for row in schedule_rows:
        session_date = _parse_iso_date(row.get("session_date"))
        if session_date is None:
            continue

        subject = str(row.get("subject_name") or "").strip()
        if not subject:
            continue

        room = str(row.get("room") or "").strip() or None
        start_period = _to_int(row.get("start_period"))
        end_period = _to_int(row.get("end_period"))
        start_time, end_time = _period_time_range(start_period, end_period)

        signature = _session_signature(
            sid,
            session_date,
            subject,
            room,
            start_period,
            end_period,
            prefix="crawler",
        )

        payload_rows.append(
            {
                "student_id": sid,
                "session_date": session_date.isoformat(),
                "subject_name": subject,
                "room": room,
                "start_period": start_period,
                "end_period": end_period,
                "start_time": start_time,
                "end_time": end_time,
                "status": "scheduled",
                "source_signature": signature,
            }
        )
        signatures.add(signature)
        session_dates.append(session_date)

    if not payload_rows:
        logger.warning("No concrete session_date rows from crawler; skip actual class_sessions upsert.")
        return 0

    client = _get_client(for_write=True)
    try:
        _execute_with_retry(
            "Supabase actual class session upsert",
            lambda: (
                client.table(CLASS_SESSIONS_TABLE)
                .upsert(payload_rows, on_conflict="student_id,source_signature")
                .execute()
            ),
        )

        _cleanup_stale_crawler_sessions(
            client=client,
            student_id=sid,
            signatures=signatures,
            min_date=min(session_dates),
            max_date=max(session_dates),
        )
    except APIError as exc:
        detail = str(exc)
        if "PGRST205" in detail or "Could not find the table" in detail:
            logger.warning(
                "Table '%s' is not available yet. Run supabase/init_tables.sql to enable class sessions.",
                CLASS_SESSIONS_TABLE,
            )
            return 0
        if "42501" in detail or "row-level security" in detail.lower():
            raise RuntimeError(
                "Supabase write blocked by RLS for class sessions. "
                "Set SUPABASE_SERVICE_ROLE_KEY or create INSERT/UPDATE/DELETE policies on 'class_sessions'."
            ) from exc
        raise

    logger.info("Upserted %d actual class session row(s) from crawler.", len(payload_rows))
    return len(payload_rows)


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


def get_all_schedule(student_id: str | None = None) -> list[dict]:
    """Return every schedule row for the given student."""
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    logger.info("Querying full schedule for student_id=%s", sid)

    client = _get_client(for_write=False)
    response = _execute_with_retry(
        "Supabase full schedule query",
        lambda: (
            client.table(TABLE_NAME)
            .select("subject_name, room, day_of_week, start_period, end_period")
            .eq("student_id", sid)
            .order("day_of_week", desc=False)
            .order("start_period", desc=False)
            .execute()
        ),
    )

    rows: list[dict] = response.data or []
    logger.info("Found %d total schedule row(s).", len(rows))
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


def get_all_appointments(student_id: str | None = None) -> list[dict]:
    """Return every appointment for the given student."""
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    logger.info("Querying all appointments for student_id=%s", sid)

    client = _get_client(for_write=False)
    try:
        response = _execute_with_retry(
            "Supabase full appointment query",
            lambda: (
                client.table(APPOINTMENTS_TABLE)
                .select("id, title, appointment_date, start_time, end_time, location, note")
                .eq("student_id", sid)
                .order("appointment_date", desc=False)
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
    logger.info("Found %d total appointment(s).", len(rows))
    return rows


def materialize_class_sessions(
    schedule_rows: list[dict],
    student_id: str | None = None,
    weeks_ahead: int | None = None,
) -> int:
    """Build concrete class sessions from weekly schedule patterns."""
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    if not schedule_rows:
        logger.info("No schedule rows to materialize into class sessions.")
        return 0

    target_weeks = weeks_ahead or _resolve_session_weeks_ahead()
    today = local_today()
    signatures: set[str] = set()
    payload_rows: list[dict] = []

    for row in schedule_rows:
        subject = str(row.get("subject_name") or "").strip()
        if not subject:
            continue

        room = str(row.get("room") or "").strip() or None
        day_of_week = str(row.get("day_of_week") or "").strip()
        start_period = _to_int(row.get("start_period"))
        end_period = _to_int(row.get("end_period"))
        first_date = _next_weekday(today, day_of_week)

        start_time, end_time = _period_time_range(start_period, end_period)
        for week_offset in range(max(1, target_weeks)):
            session_date = first_date + datetime.timedelta(days=7 * week_offset)
            signature = _session_signature(
                sid,
                session_date,
                subject,
                room,
                start_period,
                end_period,
            )
            signatures.add(signature)
            payload_rows.append(
                {
                    "student_id": sid,
                    "session_date": session_date.isoformat(),
                    "subject_name": subject,
                    "room": room,
                    "start_period": start_period,
                    "end_period": end_period,
                    "start_time": start_time,
                    "end_time": end_time,
                    "status": "scheduled",
                    "source_signature": signature,
                }
            )

    if not payload_rows:
        logger.info("No valid rows produced while materializing class sessions.")
        return 0

    client = _get_client(for_write=True)
    try:
        _execute_with_retry(
            "Supabase class session upsert",
            lambda: (
                client.table(CLASS_SESSIONS_TABLE)
                .upsert(payload_rows, on_conflict="student_id,source_signature")
                .execute()
            ),
        )

        _cleanup_stale_class_sessions(client, sid, signatures)
    except APIError as exc:
        detail = str(exc)
        if "PGRST205" in detail or "Could not find the table" in detail:
            logger.warning(
                "Table '%s' is not available yet. Run supabase/init_tables.sql to enable class sessions.",
                CLASS_SESSIONS_TABLE,
            )
            return 0
        if "42501" in detail or "row-level security" in detail.lower():
            raise RuntimeError(
                "Supabase write blocked by RLS for class sessions. "
                "Set SUPABASE_SERVICE_ROLE_KEY or create INSERT/UPDATE/DELETE policies on 'class_sessions'."
            ) from exc
        raise

    logger.info("Materialized %d class session row(s).", len(payload_rows))
    return len(payload_rows)


def get_today_class_sessions(
    student_id: str | None = None,
    target_date: datetime.date | None = None,
) -> list[dict]:
    """Return concrete class sessions for a specific date (default: today)."""
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    date_value = (target_date or local_today()).isoformat()
    client = _get_client(for_write=False)

    try:
        crawler_rows = _execute_with_retry(
            "Supabase class session today query (crawler)",
            lambda: (
                client.table(CLASS_SESSIONS_TABLE)
                .select(
                    "id, subject_name, room, session_date, start_period, end_period, "
                    "start_time, end_time, status, notes"
                )
                .eq("student_id", sid)
                .eq("session_date", date_value)
                .neq("status", "cancelled")
                .like("source_signature", "crawler:%")
                .order("start_time", desc=False)
                .execute()
            ),
        )
        crawler_data: list[dict] = crawler_rows.data or []
        if crawler_data:
            logger.info("Found %d crawler-backed class session(s) on %s.", len(crawler_data), date_value)
            return crawler_data

        response = _execute_with_retry(
            "Supabase class session today query (fallback)",
            lambda: (
                client.table(CLASS_SESSIONS_TABLE)
                .select(
                    "id, subject_name, room, session_date, start_period, end_period, "
                    "start_time, end_time, status, notes"
                )
                .eq("student_id", sid)
                .eq("session_date", date_value)
                .neq("status", "cancelled")
                .order("start_time", desc=False)
                .execute()
            ),
        )
    except APIError as exc:
        detail = str(exc)
        if "PGRST205" in detail or "Could not find the table" in detail:
            logger.warning(
                "Table '%s' is not available yet. Run supabase/init_tables.sql to enable class sessions.",
                CLASS_SESSIONS_TABLE,
            )
            return []
        raise

    rows: list[dict] = response.data or []
    logger.info("Found %d class session(s) on %s.", len(rows), date_value)
    return rows


def get_all_class_sessions(student_id: str | None = None) -> list[dict]:
    """Return all concrete class sessions for the given student."""
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    client = _get_client(for_write=False)
    try:
        crawler_rows = _execute_with_retry(
            "Supabase full class session query (crawler)",
            lambda: (
                client.table(CLASS_SESSIONS_TABLE)
                .select(
                    "id, subject_name, room, session_date, start_period, end_period, "
                    "start_time, end_time, status, notes"
                )
                .eq("student_id", sid)
                .neq("status", "cancelled")
                .like("source_signature", "crawler:%")
                .order("session_date", desc=False)
                .order("start_time", desc=False)
                .execute()
            ),
        )
        crawler_data: list[dict] = crawler_rows.data or []
        if crawler_data:
            logger.info("Found %d crawler-backed class session(s).", len(crawler_data))
            return crawler_data

        response = _execute_with_retry(
            "Supabase full class session query (fallback)",
            lambda: (
                client.table(CLASS_SESSIONS_TABLE)
                .select(
                    "id, subject_name, room, session_date, start_period, end_period, "
                    "start_time, end_time, status, notes"
                )
                .eq("student_id", sid)
                .neq("status", "cancelled")
                .order("session_date", desc=False)
                .order("start_time", desc=False)
                .execute()
            ),
        )
    except APIError as exc:
        detail = str(exc)
        if "PGRST205" in detail or "Could not find the table" in detail:
            logger.warning(
                "Table '%s' is not available yet. Run supabase/init_tables.sql to enable class sessions.",
                CLASS_SESSIONS_TABLE,
            )
            return []
        raise

    rows: list[dict] = response.data or []
    logger.info("Found %d total class session(s).", len(rows))
    return rows


def _cleanup_stale_class_sessions(client: Client, student_id: str, signatures: set[str]) -> None:
    """Delete session rows no longer present in the latest generated signature set."""
    if not signatures:
        return

    response = _execute_with_retry(
        "Supabase class session signature query",
        lambda: (
            client.table(CLASS_SESSIONS_TABLE)
            .select("source_signature")
            .eq("student_id", student_id)
            .execute()
        ),
    )
    existing_rows: list[dict] = response.data or []
    existing_signatures = {
        str(row.get("source_signature") or "").strip() for row in existing_rows if row.get("source_signature")
    }

    stale = sorted(existing_signatures - signatures)
    if not stale:
        return

    for signature in stale:
        _execute_with_retry(
            "Supabase class session stale delete",
            lambda signature=signature: (
                client.table(CLASS_SESSIONS_TABLE)
                .delete()
                .eq("student_id", student_id)
                .eq("source_signature", signature)
                .execute()
            ),
        )


def _resolve_session_weeks_ahead() -> int:
    raw = os.environ.get("CLASS_SESSION_WEEKS_AHEAD", "10")
    try:
        weeks = int(raw)
    except ValueError:
        weeks = 10
    return max(1, weeks)


def _next_weekday(reference_date: datetime.date, day_of_week: str) -> datetime.date:
    weekday_index = _WEEKDAY_TO_INDEX.get(str(day_of_week or "").strip().lower())
    if weekday_index is None:
        return reference_date
    delta_days = (weekday_index - reference_date.weekday()) % 7
    return reference_date + datetime.timedelta(days=delta_days)


def _period_time_range(start_period: int, end_period: int) -> tuple[str, str]:
    start = _PERIOD_START.get(start_period, _fallback_period_time(start_period))
    end_start = _PERIOD_START.get(end_period, _fallback_period_time(end_period))
    base_date = datetime.date.today()
    end_dt = _to_datetime(base_date, end_start) + datetime.timedelta(minutes=50)
    return f"{start}:00", end_dt.strftime("%H:%M:%S")


def _fallback_period_time(period: int) -> str:
    baseline = datetime.datetime.combine(datetime.date.today(), datetime.time(hour=7, minute=0))
    computed = baseline + datetime.timedelta(minutes=max(period - 1, 0) * 50)
    return computed.strftime("%H:%M")


def _to_datetime(target_date: datetime.date, hhmm: str) -> datetime.datetime:
    hour = int(hhmm[:2])
    minute = int(hhmm[3:5])
    return datetime.datetime.combine(target_date, datetime.time(hour=hour, minute=minute))


def _session_signature(
    student_id: str,
    session_date: datetime.date,
    subject_name: str,
    room: str | None,
    start_period: int,
    end_period: int,
    prefix: str = "generated",
) -> str:
    payload = {
        "student_id": student_id,
        "session_date": session_date.isoformat(),
        "subject_name": subject_name,
        "room": room or "",
        "start_period": start_period,
        "end_period": end_period,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _normalize_schedule_rows(schedule_rows: list[dict], student_id: str) -> list[dict]:
    normalized: list[dict] = []
    for row in schedule_rows:
        subject = str(row.get("subject_name") or "").strip()
        day_of_week = str(row.get("day_of_week") or "").strip()
        if not subject or not day_of_week:
            continue

        normalized.append(
            {
                "student_id": student_id,
                "subject_name": subject,
                "room": str(row.get("room") or "").strip() or None,
                "day_of_week": day_of_week,
                "start_period": _to_int(row.get("start_period")),
                "end_period": _to_int(row.get("end_period")),
            }
        )
    return normalized


def _parse_iso_date(value: object) -> datetime.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _cleanup_stale_crawler_sessions(
    client: Client,
    student_id: str,
    signatures: set[str],
    min_date: datetime.date,
    max_date: datetime.date,
) -> None:
    response = _execute_with_retry(
        "Supabase crawler class session signature query",
        lambda: (
            client.table(CLASS_SESSIONS_TABLE)
            .select("source_signature")
            .eq("student_id", student_id)
            .gte("session_date", min_date.isoformat())
            .lte("session_date", max_date.isoformat())
            .like("source_signature", "crawler:%")
            .execute()
        ),
    )

    existing_rows: list[dict] = response.data or []
    existing_signatures = {
        str(row.get("source_signature") or "").strip() for row in existing_rows if row.get("source_signature")
    }
    stale = sorted(existing_signatures - signatures)
    for signature in stale:
        _execute_with_retry(
            "Supabase stale crawler class session delete",
            lambda signature=signature: (
                client.table(CLASS_SESSIONS_TABLE)
                .delete()
                .eq("student_id", student_id)
                .eq("source_signature", signature)
                .execute()
            ),
        )


def _to_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def get_calendar_sync_state(student_id: str | None = None) -> list[dict]:
    """Return sync-state rows for the given student, if the table exists."""
    sid = student_id or os.environ.get("STUDENT_ID")
    if not sid:
        raise ValueError("student_id is required; set STUDENT_ID env var.")

    client = _get_client(for_write=False)
    try:
        response = _execute_with_retry(
            "Supabase calendar sync state query",
            lambda: (
                client.table(CALENDAR_SYNC_STATE_TABLE)
                .select(
                    "student_id, source_type, source_key, source_hash, uploaded, "
                    "calendar_event_id, calendar_event_link, calendar_synced_at, last_seen_at"
                )
                .eq("student_id", sid)
                .execute()
            ),
        )
    except APIError as exc:
        detail = str(exc)
        if "PGRST205" in detail or "Could not find the table" in detail:
            logger.warning(
                "Table '%s' is not available yet. Run supabase/init_tables.sql to enable calendar sync state.",
                CALENDAR_SYNC_STATE_TABLE,
            )
            return []
        raise

    rows: list[dict] = response.data or []
    logger.info("Found %d calendar sync state row(s).", len(rows))
    return rows


def upsert_calendar_sync_state(rows: list[dict]) -> None:
    """Upsert calendar sync state rows when the table exists."""
    if not rows:
        return

    client = _get_client(for_write=True)
    try:
        _execute_with_retry(
            "Supabase calendar sync state upsert",
            lambda: (
                client.table(CALENDAR_SYNC_STATE_TABLE)
                .upsert(rows, on_conflict="student_id,source_type,source_key")
                .execute()
            ),
        )
    except APIError as exc:
        detail = str(exc)
        if "PGRST205" in detail or "Could not find the table" in detail:
            logger.warning(
                "Table '%s' is not available yet. Run supabase/init_tables.sql to persist uploaded state.",
                CALENDAR_SYNC_STATE_TABLE,
            )
            return
        if "42501" in detail or "row-level security" in detail.lower():
            raise RuntimeError(
                "Supabase write blocked by RLS for calendar sync state. "
                "Set SUPABASE_SERVICE_ROLE_KEY or create INSERT/UPDATE policies on 'calendar_sync_state'."
            ) from exc
        raise
