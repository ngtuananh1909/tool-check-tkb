"""Synchronize crawled TDTU data with, and read from, Google Calendar."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import socket
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from time_utils import local_today

logger = logging.getLogger(__name__)

CALENDAR_SCOPE = ["https://www.googleapis.com/auth/calendar"]
BOT_SOURCE_TAG = "tool-check-tkb"
SYNC_SOURCE_CLASS_SESSION = "class_session"
SYNC_SOURCE_APPOINTMENT = "appointment"
SYNC_SOURCE_EXAM = "exam"
SYNC_SOURCE_DEADLINE = "deadline"


def _managed_source_types_for_crawler_sync() -> frozenset[str]:
    """Source types owned by ``sync_crawled_data_to_google_calendar``.

    Crawler sync only manages ``class_sessions``, ``exams``, and ``deadlines``. It must never
    delete events created by Telegram ``/add`` (``source_type=appointment``) or
    by Telegram ``/add`` (``source_type=appointment``).
    """
    return frozenset({SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_EXAM, SYNC_SOURCE_DEADLINE})
CALENDAR_API_MAX_ATTEMPTS = 4
CALENDAR_API_RETRY_STATUSES = {429, 500, 502, 503, 504}

# Google Calendar colorId mapping for special class session statuses.
# See https://developers.google.com/calendar/api/v3/reference/colors/get
# "absent" (báo vắng) → Graphite (grey), "makeup" (học bù) → Peacock (blue).
SESSION_STATUS_COLOR_ID: dict[str, str] = {
    "absent": "8",   # Graphite – grey
    "makeup": "7",   # Peacock – blue
}
EXAM_EVENT_COLOR_ID = "11"  # Tomato - red (nổi bật)
DEFAULT_EVENT_REMINDER_MINUTES = 60
EXAM_EVENT_REMINDER_MINUTES = 7 * 24 * 60

# Path to contact list file for auto-adding attendees to schedule events.
CONTACT_FILE = "contact.txt"

# Official TDTU period start times from the provided timetable image.
PERIOD_START: dict[int, str] = {
    1: "06:50",
    2: "07:40",
    3: "08:30",
    4: "09:30",
    5: "10:20",
    6: "11:10",
    7: "12:45",
    8: "13:35",
    9: "14:25",
    10: "15:25",
    11: "16:15",
    12: "17:05",
    13: "18:05",
    14: "18:55",
    15: "19:45",
}


# ---------------------------------------------------------------------------
# Contact loading – reads contact.txt for timetable/class-session attendee auto-add
# ---------------------------------------------------------------------------

def _load_contacts() -> list[dict]:
    """Load contacts from contact.txt.

    Returns a list of dicts with keys 'name' (str) and 'email' (str).
    Returns an empty list if the file does not exist or is empty.
    """
    contact_path = Path(CONTACT_FILE)
    if not contact_path.is_file():
        # Also check relative to this script's directory.
        contact_path = Path(__file__).resolve().parent / CONTACT_FILE
        if not contact_path.is_file():
            return []

    contacts: list[dict] = []
    try:
        with open(contact_path, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                # Expected format: name - email
                if " - " in line:
                    parts = line.split(" - ", 1)
                    name = parts[0].strip()
                    email = parts[1].strip()
                elif "@" in line:
                    # Bare email without name
                    email = line.strip()
                    name = email.split("@")[0]
                else:
                    continue

                if email and "@" in email:
                    contacts.append({"name": name, "email": email})
    except Exception as exc:
        logger.warning("Failed to load contact.txt: %s", exc)
        return []

    if contacts:
        logger.info("Loaded %d contact(s) from %s.", len(contacts), contact_path)
    return contacts


def _contacts_to_attendees(contacts: list[dict]) -> list[dict]:
    """Convert a list of contact dicts to Google Calendar attendee format."""
    attendees: list[dict] = []
    seen: set[str] = set()
    for c in contacts:
        email = c.get("email", "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            name = c.get("name", "").strip()
            entry: dict = {"email": email}
            if name:
                entry["displayName"] = name
            attendees.append(entry)
    return attendees


def sync_crawled_data_to_google_calendar(
    class_sessions: list[dict] | None,
    exams: list[dict] | None,
    student_id: str | None = None,
    deadlines: list[dict] | None = None,
) -> tuple[str, bool]:
    """Sync raw crawled data without deleting sources whose crawl failed.

    ``None`` means that source could not be collected; an empty list means it
    was collected successfully and genuinely has no rows.  Only successfully
    collected source types are reconciled, protecting existing Calendar events
    during a partial crawler outage.
    """
    target_date = local_today()

    session_rows = class_sessions or []
    exam_rows = exams or []
    deadline_rows = deadlines or []
    sync_items = _build_sync_items_from_sessions(
        session_rows, exam_rows, target_date, deadlines=deadline_rows
    )
    managed: set[str] = set()
    if class_sessions is not None:
        managed.add(SYNC_SOURCE_CLASS_SESSION)
    if exams is not None:
        managed.add(SYNC_SOURCE_EXAM)
    if deadlines is not None:
        managed.add(SYNC_SOURCE_DEADLINE)
    if not managed:
        logger.warning("Calendar sync skipped because no crawler data was collected.")
        return "", False

    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    events = [item["payload"] for item in sync_items]
    
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    calendar_required = os.environ.get("GOOGLE_CALENDAR_REQUIRED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not calendar_id or (not service_account_json and not service_account_file):
        if calendar_required:
            raise RuntimeError(
                "Google Calendar sync is required but credentials are missing. "
                "Set GOOGLE_CALENDAR_ID and GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE."
            )
        logger.warning(
            "Google Calendar sync skipped. Missing GOOGLE_CALENDAR_ID and Google credentials."
        )
        return "", False

    service, service_account_email = _build_calendar_service(service_account_json, service_account_file)
    _validate_calendar_target(service, calendar_id, service_account_email)
    _replace_bot_events_for_range(service, calendar_id, sync_items, student_id, managed)

    logger.info(
        "Synced %d event(s) to Google Calendar '%s' directly from crawler (managed=%s).",
        len(events),
        calendar_id,
        sorted(managed),
    )
    return "", True


def insert_calendar_event(title: str, appointment_date: dt.date, start_time: str | None, end_time: str | None, location: str | None, note: str | None) -> str:
    """Insert a single event directly into Google Calendar (used by Telegram /add)."""
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    
    if not calendar_id or (not service_account_json and not service_account_file):
        logger.warning("Skipping calendar insert: Missing credentials.")
        return ""

    service, _ = _build_calendar_service(service_account_json, service_account_file)
    timezone = os.environ.get("APP_TIMEZONE", "Asia/Ho_Chi_Minh")

    if start_time:
        start_dt = _to_datetime(appointment_date, start_time, timezone)
        if end_time:
            end_dt = _to_datetime(appointment_date, end_time, timezone)
        else:
            end_dt = start_dt + dt.timedelta(hours=1)
        payload = {
            "summary": title,
            "location": location,
            "description": note,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
        }
        _apply_default_reminder(payload)
    else:
        payload = {
            "summary": title,
            "location": location,
            "description": note,
            "start": {"date": appointment_date.isoformat()},
            "end": {"date": (appointment_date + dt.timedelta(days=1)).isoformat()},
        }
        
    source_key = f"{SYNC_SOURCE_APPOINTMENT}:{appointment_date.isoformat()}:{start_time or ''}:{end_time or ''}:{title}"
    payload["extendedProperties"] = {
        "private": {
            "source": BOT_SOURCE_TAG,
            "source_type": SYNC_SOURCE_APPOINTMENT,
            "source_key": source_key,
            "source_hash": _sync_hash(payload),
        }
    }
    
    resp = _execute_calendar_request(
        "calendar insert event",
        lambda: service.events().insert(calendarId=calendar_id, body=payload).execute(),
    )
    return str(resp.get("id") or "")


def fetch_events_from_calendar(target_date: dt.date, days_ahead: int = 0) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch Calendar events and group classes, appointments, and exams."""
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()

    if not calendar_id or (not service_account_json and not service_account_file):
        logger.warning("Missing credentials, cannot fetch from Google Calendar.")
        return [], [], []

    service, _ = _build_calendar_service(service_account_json, service_account_file)
    timezone = os.environ.get("APP_TIMEZONE", "Asia/Ho_Chi_Minh")
    tz = ZoneInfo(timezone)
    
    time_min = dt.datetime.combine(target_date, dt.time.min).replace(tzinfo=tz)
    time_max = time_min + dt.timedelta(days=1 + days_ahead)
    
    events_result = _execute_calendar_request(
        "calendar fetch todays events",
        lambda: service.events().list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime"
        ).execute()
    )
    
    events = events_result.get("items", [])
    
    classes = []
    appointments = []
    exams = []
    
    for event in events:
        start = event.get("start", {})
        end = event.get("end", {})
        start_time = ""
        end_time = ""
        event_date = target_date
        if start.get("dateTime"):
            dt_obj = dt.datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
            dt_obj = dt_obj.replace(tzinfo=tz) if dt_obj.tzinfo is None else dt_obj.astimezone(tz)
            start_time = dt_obj.strftime("%H:%M")
            event_date = dt_obj.date()
        elif start.get("date"):
            try:
                event_date = dt.date.fromisoformat(start["date"])
            except ValueError:
                event_date = target_date
        if end.get("dateTime"):
            dt_obj = dt.datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
            dt_obj = dt_obj.replace(tzinfo=tz) if dt_obj.tzinfo is None else dt_obj.astimezone(tz)
            end_time = dt_obj.strftime("%H:%M")

        title = event.get("summary", "")
        location = event.get("location", "")
        notes = event.get("description", "")
        source_type = _event_source_type(event)
        row = {
            "title": title,
            "subject_name": title,
            "start_time": start_time,
            "end_time": end_time,
            "location": location,
            "room": location,
            "exam_room": location,
            "note": notes,
            "notes": notes,
            "appointment_date": event_date.isoformat(),
        }
        if source_type == SYNC_SOURCE_CLASS_SESSION and row["appointment_date"] == target_date.isoformat():
            classes.append(row)
        elif source_type == SYNC_SOURCE_EXAM:
            exams.append(row)
        elif source_type in {SYNC_SOURCE_APPOINTMENT, "legacy"} and row["appointment_date"] == target_date.isoformat():
            appointments.append(row)

    logger.info("Fetched %d event(s) from Google Calendar for %s.", len(events), target_date)
    return classes, appointments, exams


def fetch_tagged_calendar_events(
    source_type: str,
    target_date: dt.date | None = None,
    days_ahead: int = 90,
) -> list[dict]:
    """Return bot-created tagged events from tomorrow onward by default."""
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not calendar_id or (not service_account_json and not service_account_file):
        logger.warning("Missing credentials, cannot fetch tagged Calendar events.")
        return []

    service, _ = _build_calendar_service(service_account_json, service_account_file)
    timezone = os.environ.get("APP_TIMEZONE", "Asia/Ho_Chi_Minh")
    tz = ZoneInfo(timezone)
    start_date = target_date or (local_today() + dt.timedelta(days=1))
    time_min = dt.datetime.combine(start_date, dt.time.min).replace(tzinfo=tz)
    time_max = time_min + dt.timedelta(days=max(1, days_ahead))
    events_result = _execute_calendar_request(
        f"calendar fetch {source_type} events",
        lambda: service.events().list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            privateExtendedProperty=f"source_type={source_type}",
        ).execute(),
    )

    rows: list[dict] = []
    for event in events_result.get("items", []):
        props = ((event.get("extendedProperties") or {}).get("private") or {})
        if props.get("source") != BOT_SOURCE_TAG or _event_source_type(event) != source_type:
            continue
        start = event.get("start") or {}
        end = event.get("end") or {}
        rows.append(
            {
                "title": str(event.get("summary") or "").strip(),
                "start": start.get("dateTime") or start.get("date") or "",
                "end": end.get("dateTime") or end.get("date") or "",
                "location": str(event.get("location") or "").strip(),
                "notes": str(event.get("description") or "").strip(),
                "html_link": str(event.get("htmlLink") or "").strip(),
                "source_key": _event_source_key(event),
            }
        )
    return rows


def find_tagged_calendar_event(source_type: str, source_key: str) -> dict | None:
    """Find a future tagged event selected through a Telegram callback."""
    return next(
        (
            event
            for event in fetch_tagged_calendar_events(source_type)
            if str(event.get("source_key") or "") == source_key
        ),
        None,
    )


def _build_calendar_service(service_account_json: str, service_account_file: str) -> tuple[Resource, str]:
    if service_account_json:
        try:
            info = json.loads(service_account_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. "
                "For GitHub Actions, store the full raw service-account JSON in secrets.GOOGLE_SERVICE_ACCOUNT_JSON."
            ) from exc
        credentials = service_account.Credentials.from_service_account_info(info, scopes=CALENDAR_SCOPE)
    else:
        if not service_account_file:
            raise RuntimeError(
                "Google Calendar credentials are missing. "
                "Set GOOGLE_SERVICE_ACCOUNT_JSON (recommended for GitHub Actions) "
                "or GOOGLE_SERVICE_ACCOUNT_FILE (for local file-based setup)."
            )
        if not os.path.isfile(service_account_file):
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_FILE points to a missing file: "
                f"'{service_account_file}'. "
                "In GitHub Actions, use GOOGLE_SERVICE_ACCOUNT_JSON secret instead of a local absolute path."
            )
        credentials = service_account.Credentials.from_service_account_file(
            service_account_file,
            scopes=CALENDAR_SCOPE,
        )
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    service_account_email = str(getattr(credentials, "service_account_email", "")).strip()
    return service, service_account_email


def _validate_calendar_target(service: Resource, calendar_id: str, service_account_email: str) -> None:
    if calendar_id == "primary":
        raise RuntimeError(
            "GOOGLE_CALENDAR_ID=primary points to the service account calendar, not your Gmail calendar. "
            "Set GOOGLE_CALENDAR_ID to your real calendar ID (e.g. your Gmail address) and share that "
            f"calendar with {service_account_email} using 'Make changes to events'."
        )

    try:
        service.calendars().get(calendarId=calendar_id).execute()
    except TimeoutError:
        logger.warning(
            "Timed out validating Google Calendar '%s'. Continuing with sync; "
            "the calendar may still be reachable for event writes.",
            calendar_id,
        )
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        if status in {403, 404}:
            raise RuntimeError(
                f"Service account {service_account_email} cannot access calendar '{calendar_id}'. "
                "Share this calendar with that service account and grant 'Make changes to events'."
            ) from exc
        raise


def _build_sync_items(
    schedule_rows: list[dict],
    appointments: list[dict],
    exams: list[dict],
    target_date: dt.date,
) -> list[dict]:
    timezone = os.environ.get("APP_TIMEZONE", "Asia/Ho_Chi_Minh")
    items: list[dict] = []

    sync_weeks = _calendar_sync_weeks()

    # Load contacts for attendee auto-add on schedule events.
    all_contacts = _load_contacts()
    all_attendees = _contacts_to_attendees(all_contacts)

    for cls in schedule_rows:
        subject = str(cls.get("subject_name") or "").strip() or "Lop hoc"
        room = str(cls.get("room") or "").strip() or None
        day_of_week = str(cls.get("day_of_week") or "").strip()
        first_date = _next_weekday_date(target_date, day_of_week)
        start_dt, end_dt = _class_datetimes(cls, first_date, timezone)
        recurrence = _class_recurrence(day_of_week, sync_weeks)
        source_key = _class_source_key(cls)

        payload = {
            "summary": subject,
            "location": room,
            "description": _class_session_status_description(cls),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
        }
        _apply_default_reminder(payload)
        if all_attendees:
            payload["attendees"] = all_attendees
        if recurrence:
            payload["recurrence"] = recurrence
        source_hash = _sync_hash({
            "source_type": SYNC_SOURCE_SCHEDULE,
            "subject_name": subject,
            "room": room,
            "day_of_week": day_of_week,
            "start_period": _to_int(cls.get("start_period")),
            "end_period": _to_int(cls.get("end_period")),
            "recurrence": recurrence,
            "payload": payload,
        })
        payload["extendedProperties"] = {
            "private": {
                "source": BOT_SOURCE_TAG,
                "source_type": SYNC_SOURCE_SCHEDULE,
                "source_key": source_key,
                "source_hash": source_hash,
            }
        }
        items.append(
            {
                "source_type": SYNC_SOURCE_SCHEDULE,
                "source_key": source_key,
                "source_hash": source_hash,
                "payload": payload,
            }
        )

    for appointment in appointments:
        appointment_id = appointment.get("id")
        title = str(appointment.get("title") or "").strip() or "Lich hen"
        location = str(appointment.get("location") or "").strip() or None
        note = str(appointment.get("note") or "").strip() or None
        appt_date = _parse_date(appointment.get("appointment_date"), target_date)
        source_key = _appointment_source_key(appointment)

        start_time = _display_time(appointment.get("start_time"))
        end_time = _display_time(appointment.get("end_time"))

        if start_time:
            start_dt = _to_datetime(appt_date, start_time, timezone)
            if end_time:
                end_dt = _to_datetime(appt_date, end_time, timezone)
            else:
                end_dt = start_dt + dt.timedelta(hours=1)
            payload = {
                "summary": title,
                "location": location,
                "description": note,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
            }
            _apply_default_reminder(payload)
        else:
            payload = {
                "summary": title,
                "location": location,
                "description": note,
                "start": {"date": appt_date.isoformat()},
                "end": {"date": (appt_date + dt.timedelta(days=1)).isoformat()},
            }

        source_hash = _sync_hash({
            "source_type": SYNC_SOURCE_APPOINTMENT,
            "appointment_id": appointment_id,
            "title": title,
            "appointment_date": appt_date.isoformat(),
            "start_time": start_time,
            "end_time": end_time,
            "location": location,
            "note": note,
            "payload": payload,
        })
        payload["extendedProperties"] = {
            "private": {
                "source": BOT_SOURCE_TAG,
                "source_type": SYNC_SOURCE_APPOINTMENT,
                "source_key": source_key,
                "source_hash": source_hash,
            }
        }
        items.append(
            {
                "source_type": SYNC_SOURCE_APPOINTMENT,
                "source_key": source_key,
                "source_hash": source_hash,
                "payload": payload,
            }
        )

    for exam in exams:
        exam_date = _parse_date(exam.get("exam_date"), target_date)
        title = _exam_calendar_title(exam)
        location = str(exam.get("exam_room") or "").strip() or None
        note = _exam_calendar_description(exam)
        start_time = _display_time(exam.get("start_time"))
        end_time = _display_time(exam.get("end_time"))
        exam_type = str(exam.get("exam_type") or "").strip() or None
        source_key = _exam_source_key(exam)

        if start_time:
            start_dt = _to_datetime(exam_date, start_time, timezone)
            if end_time:
                end_dt = _to_datetime(exam_date, end_time, timezone)
            else:
                end_dt = start_dt + dt.timedelta(hours=2)
            payload = {
                "summary": title,
                "location": location,
                "description": note,
                "colorId": EXAM_EVENT_COLOR_ID,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
            }
            _apply_default_reminder(payload, minutes=EXAM_EVENT_REMINDER_MINUTES)
        else:
            payload = {
                "summary": title,
                "location": location,
                "description": note,
                "colorId": EXAM_EVENT_COLOR_ID,
                "start": {"date": exam_date.isoformat()},
                "end": {"date": (exam_date + dt.timedelta(days=1)).isoformat()},
            }

        source_hash = _sync_hash(
            {
                "source_type": SYNC_SOURCE_EXAM,
                "exam_id": exam.get("id"),
                "subject_name": title,
                "exam_date": exam_date.isoformat(),
                "start_time": start_time,
                "end_time": end_time,
                "exam_room": location,
                "exam_type": exam_type,
                "payload": payload,
            }
        )
        payload["extendedProperties"] = {
            "private": {
                "source": BOT_SOURCE_TAG,
                "source_type": SYNC_SOURCE_EXAM,
                "source_key": source_key,
                "source_hash": source_hash,
            }
        }
        items.append(
            {
                "source_type": SYNC_SOURCE_EXAM,
                "source_key": source_key,
                "source_hash": source_hash,
                "payload": payload,
            }
        )

    return items


def _class_session_status_description(session: dict) -> str:
    labels = {
        "scheduled": "Học bình thường",
        "absent": "Báo vắng",
        "makeup": "Học bù",
        "cancelled": "Đã hủy",
        "moved": "Đã chuyển lịch",
    }
    status = str(session.get("status") or "scheduled").strip().lower()
    return labels.get(status, "Học bình thường")


def _build_sync_items_from_sessions(
    class_sessions: list[dict],
    exams: list[dict],
    target_date: dt.date,
    deadlines: list[dict] | None = None,
) -> list[dict]:
    timezone = os.environ.get("APP_TIMEZONE", "Asia/Ho_Chi_Minh")
    items: list[dict] = []
    appointments: list[dict] = []  # Appointments are inserted directly by Telegram /add.

    # Load contacts for attendee auto-add on class session events.
    all_contacts = _load_contacts()
    all_attendees = _contacts_to_attendees(all_contacts)

    for session in class_sessions:
        subject = str(session.get("subject_name") or "").strip() or "Lop hoc"
        room = str(session.get("room") or "").strip() or None
        notes = _class_session_status_description(session)
        session_date = _parse_date(session.get("session_date"), target_date)
        session_id = str(session.get("id") or "").strip()

        start_time = _display_time(session.get("start_time")) or PERIOD_START.get(
            _to_int(session.get("start_period")),
            _fallback_period_time(_to_int(session.get("start_period"))),
        )
        end_time = _display_time(session.get("end_time"))
        if not end_time:
            end_base = PERIOD_START.get(
                _to_int(session.get("end_period")),
                _fallback_period_time(_to_int(session.get("end_period"))),
            )
            end_dt = _to_datetime(session_date, end_base, timezone) + dt.timedelta(minutes=50)
            end_time = end_dt.strftime("%H:%M")

        start_dt = _to_datetime(session_date, start_time, timezone)
        end_dt = _to_datetime(session_date, end_time, timezone)
        if end_dt <= start_dt:
            end_dt = start_dt + dt.timedelta(minutes=50)

        source_key = _class_session_source_key(session)
        status = str(session.get("status") or "scheduled").strip()
        color_id = SESSION_STATUS_COLOR_ID.get(status)
        payload = {
            "summary": subject,
            "location": room,
            "description": notes,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
        }
        _apply_default_reminder(payload)
        if all_attendees:
            payload["attendees"] = all_attendees
        if color_id:
            payload["colorId"] = color_id
        source_hash = _sync_hash(
            {
                "source_type": SYNC_SOURCE_CLASS_SESSION,
                "session_id": session_id,
                "session_date": session_date.isoformat(),
                "subject_name": subject,
                "room": room,
                "start_time": start_time,
                "end_time": end_time,
                "status": status,
                "payload": payload,
            }
        )
        payload["extendedProperties"] = {
            "private": {
                "source": BOT_SOURCE_TAG,
                "source_type": SYNC_SOURCE_CLASS_SESSION,
                "source_key": source_key,
                "source_hash": source_hash,
            }
        }
        items.append(
            {
                "source_type": SYNC_SOURCE_CLASS_SESSION,
                "source_key": source_key,
                "source_hash": source_hash,
                "payload": payload,
            }
        )

    for appointment in appointments:
        appointment_id = appointment.get("id")
        title = str(appointment.get("title") or "").strip() or "Lich hen"
        location = str(appointment.get("location") or "").strip() or None
        note = str(appointment.get("note") or "").strip() or None
        appt_date = _parse_date(appointment.get("appointment_date"), target_date)
        source_key = _appointment_source_key(appointment)

        start_time = _display_time(appointment.get("start_time"))
        end_time = _display_time(appointment.get("end_time"))

        if start_time:
            start_dt = _to_datetime(appt_date, start_time, timezone)
            if end_time:
                end_dt = _to_datetime(appt_date, end_time, timezone)
            else:
                end_dt = start_dt + dt.timedelta(hours=1)
            payload = {
                "summary": title,
                "location": location,
                "description": note,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
            }
            _apply_default_reminder(payload)
        else:
            payload = {
                "summary": title,
                "location": location,
                "description": note,
                "start": {"date": appt_date.isoformat()},
                "end": {"date": (appt_date + dt.timedelta(days=1)).isoformat()},
            }

        source_hash = _sync_hash(
            {
                "source_type": SYNC_SOURCE_APPOINTMENT,
                "appointment_id": appointment_id,
                "title": title,
                "appointment_date": appt_date.isoformat(),
                "start_time": start_time,
                "end_time": end_time,
                "location": location,
                "note": note,
                "payload": payload,
            }
        )
        payload["extendedProperties"] = {
            "private": {
                "source": BOT_SOURCE_TAG,
                "source_type": SYNC_SOURCE_APPOINTMENT,
                "source_key": source_key,
                "source_hash": source_hash,
            }
        }
        items.append(
            {
                "source_type": SYNC_SOURCE_APPOINTMENT,
                "source_key": source_key,
                "source_hash": source_hash,
                "payload": payload,
            }
        )

    for exam in exams:
        exam_date = _parse_date(exam.get("exam_date"), target_date)
        title = _exam_calendar_title(exam)
        location = str(exam.get("exam_room") or "").strip() or None
        note = _exam_calendar_description(exam)
        start_time = _display_time(exam.get("start_time"))
        end_time = _display_time(exam.get("end_time"))
        exam_type = str(exam.get("exam_type") or "").strip() or None
        source_key = _exam_source_key(exam)

        if start_time:
            start_dt = _to_datetime(exam_date, start_time, timezone)
            if end_time:
                end_dt = _to_datetime(exam_date, end_time, timezone)
            else:
                end_dt = start_dt + dt.timedelta(hours=2)
            payload = {
                "summary": title,
                "location": location,
                "description": note,
                "colorId": EXAM_EVENT_COLOR_ID,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
            }
            _apply_default_reminder(payload, minutes=EXAM_EVENT_REMINDER_MINUTES)
        else:
            payload = {
                "summary": title,
                "location": location,
                "description": note,
                "colorId": EXAM_EVENT_COLOR_ID,
                "start": {"date": exam_date.isoformat()},
                "end": {"date": (exam_date + dt.timedelta(days=1)).isoformat()},
            }

        source_hash = _sync_hash(
            {
                "source_type": SYNC_SOURCE_EXAM,
                "exam_id": exam.get("id"),
                "subject_name": title,
                "exam_date": exam_date.isoformat(),
                "start_time": start_time,
                "end_time": end_time,
                "exam_room": location,
                "exam_type": exam_type,
                "payload": payload,
            }
        )
        payload["extendedProperties"] = {
            "private": {
                "source": BOT_SOURCE_TAG,
                "source_type": SYNC_SOURCE_EXAM,
                "source_key": source_key,
                "source_hash": source_hash,
            }
        }
        items.append(
            {
                "source_type": SYNC_SOURCE_EXAM,
                "source_key": source_key,
                "source_hash": source_hash,
                "payload": payload,
            }
        )

    items.extend(_build_deadline_sync_items(deadlines or [], timezone))
    return items


def _build_deadline_sync_items(deadlines: list[dict], timezone: str) -> list[dict]:
    """Build tagged Calendar events for incomplete eLearning deadlines."""
    items: list[dict] = []
    for deadline in deadlines:
        due_at = _parse_deadline_datetime(deadline.get("due_date"), timezone)
        if due_at is None:
            logger.warning("Skipping deadline with invalid due_date: %r", deadline.get("due_date"))
            continue
        if due_at < dt.datetime.now(due_at.tzinfo):
            continue

        course = str(deadline.get("course_name") or "").strip() or "Môn học"
        activity = str(deadline.get("activity_name") or "").strip() or "Deadline"
        activity_url = str(deadline.get("activity_url") or "").strip()
        source_key = _deadline_source_key(deadline)
        payload = {
            "summary": f"[DEADLINE] {course}: {activity}",
            "description": "\n".join(
                line
                for line in ("#deadline", f"Môn: {course}", f"Công việc: {activity}", activity_url)
                if line
            ),
            "colorId": "6",
            "start": {"dateTime": due_at.isoformat(), "timeZone": timezone},
            "end": {
                "dateTime": (due_at + dt.timedelta(minutes=30)).isoformat(),
                "timeZone": timezone,
            },
        }
        _apply_default_reminder(payload)
        source_hash = _sync_hash(
            {
                "source_type": SYNC_SOURCE_DEADLINE,
                "source_key": source_key,
                "course": course,
                "activity": activity,
                "due_date": due_at.isoformat(),
                "activity_url": activity_url,
                "payload": payload,
            }
        )
        payload["extendedProperties"] = {
            "private": {
                "source": BOT_SOURCE_TAG,
                "source_type": SYNC_SOURCE_DEADLINE,
                "source_key": source_key,
                "source_hash": source_hash,
            }
        }
        items.append(
            {
                "source_type": SYNC_SOURCE_DEADLINE,
                "source_key": source_key,
                "source_hash": source_hash,
                "payload": payload,
            }
        )
    return items


def _build_calendar_events(
    schedule_rows: list[dict],
    appointments: list[dict],
    target_date: dt.date,
) -> list[dict]:
    return [item["payload"] for item in _build_sync_items(schedule_rows, appointments, [], target_date)]


def _replace_bot_events_for_range(
    service: Resource,
    calendar_id: str,
    sync_items: list[dict],
    student_id: str | None,
    managed_source_types: frozenset[str] | set[str],
) -> None:
    """Reconcile bot-owned events with the desired ``sync_items``.

    Only events whose ``extendedProperties.private.source_type`` is in
    ``managed_source_types`` are eligible for deletion. This guarantees that
    one sync entry (e.g. the crawler sync) never purges events owned by
    another entry (e.g. appointments created by Telegram ``/add``).
    """
    existing_by_key, legacy_events = _list_bot_events(service, calendar_id)
    current_keys = {item["source_key"] for item in sync_items}
    managed = frozenset(managed_source_types)

    sid = student_id or os.environ.get("STUDENT_ID", "")
    now_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    state_rows: list[dict] = []

    for item in sync_items:
        existing = existing_by_key.get(item["source_key"])
        synced_event = _sync_calendar_item(service, calendar_id, item, existing)
        fallback_event_id = existing.get("id") if existing else ""
        fallback_event_link = existing.get("htmlLink") if existing else ""
        synced_event_id = str(synced_event.get("id") or fallback_event_id or "").strip() or None
        synced_event_link = str(synced_event.get("htmlLink") or fallback_event_link or "").strip() or None

        state_rows.append(
            {
                "student_id": sid,
                "source_type": item["source_type"],
                "source_key": item["source_key"],
                "source_hash": item["source_hash"],
                "uploaded": True,
                "calendar_event_id": synced_event_id,
                "calendar_event_link": synced_event_link,
                "calendar_synced_at": now_utc,
                "last_seen_at": now_utc,
            }
        )

    # Legacy events: tagged with `source=tool-check-tkb` but no `source_key`,
    # so we cannot determine their owner. Leave them untouched and warn.
    if legacy_events:
        logger.warning(
            "Found %d legacy bot event(s) without source_key; leaving untouched.",
            len(legacy_events),
        )

    deleted_count = 0
    skipped_other_owner: list[str] = []
    for source_key, event in existing_by_key.items():
        if source_key in current_keys:
            continue
        event_source_type = _event_source_type(event)
        if event_source_type not in managed:
            skipped_other_owner.append(source_key)
            continue
        event_id = str(event.get("id") or "").strip()
        if event_id:
            _safe_delete_calendar_event(service, calendar_id, event_id)
            deleted_count += 1
        state_rows.append(
            {
                "student_id": sid,
                "source_type": event_source_type,
                "source_key": source_key,
                "source_hash": _event_source_hash(event),
                "uploaded": False,
                "calendar_event_id": None,
                "calendar_event_link": None,
                "last_seen_at": now_utc,
            }
        )

    if skipped_other_owner:
        logger.warning(
            "Sync (managed=%s) skipped %d event(s) owned by other source types.",
            sorted(managed),
            len(skipped_other_owner),
        )
    if deleted_count:
        logger.info(
            "Sync (managed=%s) deleted %d stale event(s).",
            sorted(managed),
            deleted_count,
        )

    # Note: Bypassing DB, so we do not call upsert_calendar_sync_state here.


def _safe_delete_calendar_event(service: Resource, calendar_id: str, event_id: str) -> None:
    try:
        _execute_calendar_request(
            f"calendar delete {event_id}",
            lambda: service.events().delete(calendarId=calendar_id, eventId=event_id).execute(),
        )
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        if status not in {404, 410}:
            raise
        logger.warning("Calendar event %s was already deleted; continuing.", event_id)


def _list_bot_events(service: Resource, calendar_id: str) -> tuple[dict[str, dict], list[dict]]:
    existing_by_key: dict[str, dict] = {}
    legacy_events: list[dict] = []
    page_token: str | None = None

    while True:
        response = _execute_calendar_request(
            "calendar list bot events",
            lambda: (
                service.events()
                .list(
                    calendarId=calendar_id,
                    singleEvents=False,
                    privateExtendedProperty=f"source={BOT_SOURCE_TAG}",
                    maxResults=2500,
                    pageToken=page_token,
                )
                .execute()
            ),
        )
        for event in response.get("items", []):
            source_key = _event_source_key(event)
            if source_key:
                existing_by_key[source_key] = event
            else:
                legacy_events.append(event)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return existing_by_key, legacy_events


def _sync_calendar_item(
    service: Resource,
    calendar_id: str,
    item: dict,
    existing: dict | None,
) -> dict:
    payload = item["payload"]
    incoming_hash = str(item.get("source_hash") or "").strip()

    # Fast path: unchanged source hash means this event is already up-to-date.
    if existing and existing.get("id") and incoming_hash and incoming_hash == _event_source_hash(existing):
        return existing

    if existing and existing.get("id"):
        event_id = str(existing.get("id") or "").strip()
        try:
            return _execute_calendar_request(
                f"calendar patch {event_id}",
                lambda: service.events().patch(
                    calendarId=calendar_id,
                    eventId=event_id,
                    body=payload,
                ).execute(),
            )
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status not in {404, 410}:
                raise
            logger.warning("Calendar event %s was missing; recreating it.", event_id)

    return _execute_calendar_request(
        "calendar insert event",
        lambda: service.events().insert(calendarId=calendar_id, body=payload).execute(),
    )


def _execute_calendar_request(operation_name: str, action):
    last_exc: Exception | None = None
    for attempt in range(1, CALENDAR_API_MAX_ATTEMPTS + 1):
        try:
            return action()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status not in CALENDAR_API_RETRY_STATUSES or attempt == CALENDAR_API_MAX_ATTEMPTS:
                raise
            last_exc = exc
        except (TimeoutError, socket.timeout, OSError) as exc:
            if attempt == CALENDAR_API_MAX_ATTEMPTS:
                raise
            last_exc = exc

        delay = 2 ** (attempt - 1)
        logger.warning(
            "%s failed (%s). Retrying in %ss (%d/%d).",
            operation_name,
            last_exc,
            delay,
            attempt,
            CALENDAR_API_MAX_ATTEMPTS,
        )
        time.sleep(delay)

    assert last_exc is not None
    raise last_exc


def _event_source_type(event: dict) -> str:
    props = ((event.get("extendedProperties") or {}).get("private") or {})
    return str(props.get("source_type") or "legacy").strip() or "legacy"


def _event_source_key(event: dict) -> str:
    props = ((event.get("extendedProperties") or {}).get("private") or {})
    return str(props.get("source_key") or "").strip()


def _event_source_hash(event: dict) -> str:
    props = ((event.get("extendedProperties") or {}).get("private") or {})
    return str(props.get("source_hash") or "").strip()


def _class_session_source_key(session: dict) -> str:
    session_id = str(session.get("id") or "").strip()
    if session_id:
        return f"{SYNC_SOURCE_CLASS_SESSION}:{session_id}"

    subject = _normalize_value(session.get("subject_name"))
    session_date = _parse_date(session.get("session_date"), local_today()).isoformat()
    start_time = _display_time(session.get("start_time"))
    end_time = _display_time(session.get("end_time"))
    return f"{SYNC_SOURCE_CLASS_SESSION}:{session_date}:{start_time}:{end_time}:{subject}"


def _appointment_source_key(appointment: dict) -> str:
    appointment_id = str(appointment.get("id") or "").strip()
    if appointment_id:
        return f"{SYNC_SOURCE_APPOINTMENT}:{appointment_id}"

    title = _normalize_value(appointment.get("title"))
    appointment_date = _parse_date(appointment.get("appointment_date"), local_today()).isoformat()
    start_time = _display_time(appointment.get("start_time"))
    end_time = _display_time(appointment.get("end_time"))
    return f"{SYNC_SOURCE_APPOINTMENT}:{appointment_date}:{start_time}:{end_time}:{title}"


def _exam_source_key(exam: dict) -> str:
    exam_id = str(exam.get("id") or "").strip()
    if exam_id:
        return f"{SYNC_SOURCE_EXAM}:{exam_id}"

    subject = _normalize_value(exam.get("subject_name"))
    exam_date = _parse_date(exam.get("exam_date"), local_today()).isoformat()
    start_time = _display_time(exam.get("start_time"))
    end_time = _display_time(exam.get("end_time"))
    exam_type = _normalize_value(exam.get("exam_type"))
    return f"{SYNC_SOURCE_EXAM}:{exam_date}:{start_time}:{end_time}:{subject}:{exam_type}"


def _deadline_source_key(deadline: dict) -> str:
    source_signature = str(deadline.get("source_signature") or "").strip()
    if source_signature:
        return f"{SYNC_SOURCE_DEADLINE}:{source_signature}"

    activity_url = str(deadline.get("activity_url") or "").strip()
    if activity_url:
        return f"{SYNC_SOURCE_DEADLINE}:{activity_url}"

    payload = {
        "course_id": _normalize_value(deadline.get("course_id")),
        "activity_name": _normalize_value(deadline.get("activity_name")),
        "due_date": _normalize_value(deadline.get("due_date")),
    }
    return f"{SYNC_SOURCE_DEADLINE}:{_sync_hash(payload)}"


def _exam_calendar_type_label(exam_type: object) -> str:
    raw = str(exam_type or "").strip().lower()
    if not raw:
        return ""
    if any(token in raw for token in ["giua", "giữa", "mid"]):
        return "Giua ky"
    if any(token in raw for token in ["cuoi", "cuối", "final"]):
        return "Cuoi ky"
    return str(exam_type).strip()


def _exam_calendar_title(exam: dict) -> str:
    subject = str(exam.get("subject_name") or "").strip() or "Lich thi"
    label = _exam_calendar_type_label(exam.get("exam_type"))
    if not label:
        return f"[EXAM] {subject}"
    return f"[EXAM] {label} - {subject}"


def _exam_calendar_description(exam: dict) -> str:
    base_note = str(exam.get("notes") or "").strip() or "Lich thi dong bo tu cong TDTU."
    label = _exam_calendar_type_label(exam.get("exam_type"))
    if not label:
        return f"#exam\n{base_note}"
    return f"#exam\nLoai thi: {label}. {base_note}"


def _sync_hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_value(value: object) -> str:
    return str(value or "").strip().lower()


def _apply_default_reminder(payload: dict, minutes: int = DEFAULT_EVENT_REMINDER_MINUTES) -> None:
    """Attach a default popup reminder for timed events.

    Google Calendar supports reminders for events with dateTime fields.
    """
    start = payload.get("start") or {}
    end = payload.get("end") or {}
    if not isinstance(start, dict) or not isinstance(end, dict):
        return
    if "dateTime" not in start or "dateTime" not in end:
        return

    payload["reminders"] = {
        "useDefault": False,
        "overrides": [
            {
                "method": "popup",
                "minutes": max(0, int(minutes)),
            }
        ],
    }


def _fallback_period_time(period: int) -> str:
    baseline = dt.datetime.combine(dt.date.today(), dt.time(hour=7, minute=0))
    computed = baseline + dt.timedelta(minutes=max(period - 1, 0) * 50)
    return computed.strftime("%H:%M")


def _to_datetime(target_date: dt.date, hhmm: str, timezone: str) -> dt.datetime:
    hour = int(hhmm[:2])
    minute = int(hhmm[3:5])
    tz = ZoneInfo(timezone)
    return dt.datetime.combine(target_date, dt.time(hour=hour, minute=minute)).replace(tzinfo=tz)


def _display_time(value: object) -> str:
    text = str(value or "").strip()
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return ""


def _parse_date(value: object, default_date: dt.date) -> dt.date:
    text = str(value or "").strip()
    if not text:
        return default_date
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return default_date


def _parse_deadline_datetime(value: object, timezone: str) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    tz = ZoneInfo(timezone)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _to_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1
