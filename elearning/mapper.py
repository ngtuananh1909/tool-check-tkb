"""Mapper function for normalizing Moodle event JSON payloads into standard deadline dicts."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from elearning.exceptions import ElearningResponseError


def map_moodle_event(raw_event: dict, app_tz: ZoneInfo) -> dict | None:
    """Map a raw Moodle API event JSON dict into a normalized deadline dict.

    STRICT ACTIONABLE RULE: Only events where ``action.actionable is True`` are
    mapped. Missing or non-True ``actionable`` values are strictly ignored
    (return ``None``) and never defaulted to ``True``.

    Returns ``None`` if the event is non-actionable.
    Raises ``ElearningResponseError`` if required fields ('id', 'timesort') are missing.
    """
    event_id = str(raw_event.get("id") or "").strip()
    if not event_id:
        raise ElearningResponseError("Moodle event missing required 'id' field")

    # Strict Actionable Check:
    action = raw_event.get("action")
    if not isinstance(action, dict):
        raise ElearningResponseError(f"Moodle event {event_id} has invalid 'action' payload")

    if "actionable" not in action:
        raise ElearningResponseError(f"Moodle event {event_id} missing required 'action.actionable'")

    if action["actionable"] is not True:
        return None

    timesort = raw_event.get("timesort")
    if timesort is None:
        raise ElearningResponseError(f"Moodle event {event_id} missing required 'timesort' field")

    try:
        timesort_int = int(timesort)
    except (ValueError, TypeError) as exc:
        raise ElearningResponseError(f"Moodle event {event_id} has invalid 'timesort': {timesort!r}") from exc

    # Parse epoch timestamp into aware datetime in app_tz
    due_dt = datetime.fromtimestamp(timesort_int, tz=timezone.utc).astimezone(app_tz)
    course = raw_event.get("course")
    course_dict = course if isinstance(course, dict) else {}

    return {
        "moodle_event_id": event_id,
        "course_id": str(course_dict.get("id") or "").strip(),
        "course_name": str(course_dict.get("fullname") or "").strip() or "Môn học",
        "activity_name": str(raw_event.get("name") or "").strip() or "Deadline",
        "due_date": due_dt.isoformat(),
        "activity_url": str(raw_event.get("url") or "").strip(),
        "completion_status": "incomplete",
        "event_kind": str(raw_event.get("eventtype") or "due").strip(),
        "source_signature": f"moodle_event:{event_id}",
    }
