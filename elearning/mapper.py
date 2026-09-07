"""Mapper and normalizer for Moodle calendar DOM events."""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from elearning.exceptions import ElearningCrawlError


def classify_event_kind(title: str, url: str) -> str:
    """Classify Moodle calendar event by activity URL and title patterns.

    Returns one of:
    - 'open': opening lifecycle event (not a deadline, excluded)
    - 'due': assignment due deadline (supported in v1)
    - 'quiz_close': quiz close deadline (unsupported in v1)
    - 'completion': generic activity completion requirement (unsupported in v1)
    - 'unknown': unrecognized event pattern
    """
    raw_title = str(title or "").strip()
    raw_url = str(url or "").strip()
    lower_title = raw_title.lower()

    if re.search(r"(?i)(bắt đầu|opens)", raw_title):
        return "open"

    if "/mod/assign/" in raw_url:
        if any(token in lower_title for token in ["is due", "đến hạn", "hết hạn", "due"]):
            return "due"

    if "/mod/quiz/" in raw_url or any(token in lower_title for token in ["closes", "đóng", "kết thúc"]):
        return "quiz_close"

    if any(token in lower_title for token in ["should be completed", "cần hoàn thành"]):
        return "completion"

    return "unknown"


def clean_activity_name(title: str) -> str:
    """Clean calendar event title to extract activity name."""
    raw = str(title or "").strip()
    cleaned = re.sub(
        r"(?i)\s+(is due|đến hạn|hết hạn|should be completed|cần hoàn thành|bắt đầu|opens)$",
        "",
        raw,
    ).strip()
    return cleaned or raw or "Deadline"


def normalize_deadline_item(raw_item: dict, app_tz: ZoneInfo) -> dict:
    """Normalize raw crawler candidate into standardized deadline dict contract."""
    event_id = str(raw_item.get("moodle_event_id") or "").strip()
    if not event_id or not event_id.isdigit():
        raise ElearningCrawlError(f"Missing or invalid numeric moodle_event_id: {event_id!r}")

    due_ts = raw_item.get("due_timestamp")
    if due_ts is None:
        raise ElearningCrawlError(f"Missing due_timestamp for moodle_event #{event_id}")

    try:
        ts_int = int(due_ts)
        due_date_dt = dt.datetime.fromtimestamp(ts_int, tz=app_tz)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ElearningCrawlError(f"Invalid due_timestamp {due_ts!r} for moodle_event #{event_id}") from exc

    course_id = str(raw_item.get("course_id") or "").strip()
    course_name = str(raw_item.get("course_name") or "").strip() or "Môn học"
    activity_name = str(raw_item.get("activity_name") or "").strip() or "Deadline"
    activity_url = str(raw_item.get("activity_url") or "").strip()
    completion_status = str(raw_item.get("completion_status") or "incomplete").strip()
    event_kind = str(raw_item.get("event_kind") or "due").strip()

    return {
        "moodle_event_id": event_id,
        "course_id": course_id,
        "course_name": course_name,
        "activity_name": activity_name,
        "due_date": due_date_dt.isoformat(),
        "due_date_dt": due_date_dt,
        "activity_url": activity_url,
        "completion_status": completion_status,
        "event_kind": event_kind,
        "source_signature": f"moodle_event:{event_id}",
    }
