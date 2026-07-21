import datetime as dt
import os
import unittest
from unittest.mock import patch

import calendar_sync
from calendar_sync import (
    SYNC_SOURCE_CLASS_SESSION,
    SYNC_SOURCE_DEADLINE,
    SYNC_SOURCE_EXAM,
    _build_sync_items_from_sessions,
    _managed_source_types_for_crawler_sync,
    _replace_bot_events_for_range,
    fetch_tagged_calendar_events,
)


class _Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _Events:
    def __init__(self, events):
        self.events = events
        self.deleted = []
        self.inserted = []

    def list(self, **_kwargs):
        return _Request({"items": self.events})

    def insert(self, **kwargs):
        self.inserted.append(kwargs["body"])
        return _Request({"id": "new-event"})

    def patch(self, **kwargs):
        return _Request({"id": kwargs["eventId"]})

    def delete(self, **kwargs):
        self.deleted.append(kwargs["eventId"])
        return _Request({})


class _Service:
    def __init__(self, events):
        self._events = _Events(events)

    def events(self):
        return self._events


class CalendarOnlySyncTests(unittest.TestCase):
    def test_total_crawl_failure_skips_calendar(self) -> None:
        with patch.object(calendar_sync, "_build_calendar_service") as build_service:
            _, did_sync = calendar_sync.sync_crawled_data_to_google_calendar(None, None)

        self.assertFalse(did_sync)
        build_service.assert_not_called()

    def test_partial_crawl_reconciles_only_the_successful_source(self) -> None:
        with (
            patch.dict(os.environ, {"GOOGLE_CALENDAR_ID": "cal-id", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False),
            patch.object(calendar_sync, "_build_calendar_service", return_value=(object(), "svc@example.com")),
            patch.object(calendar_sync, "_validate_calendar_target"),
            patch.object(calendar_sync, "_replace_bot_events_for_range") as replace_events,
        ):
            _, did_sync = calendar_sync.sync_crawled_data_to_google_calendar(None, [])

        self.assertTrue(did_sync)
        self.assertEqual(replace_events.call_args.args[4], {SYNC_SOURCE_EXAM})

    def test_crawled_items_are_tagged_for_class_exam_and_deadline(self) -> None:
        target = dt.date.today() + dt.timedelta(days=2)
        items = _build_sync_items_from_sessions(
            [{"id": "class-1", "subject_name": "Math", "session_date": target.isoformat(), "start_time": "08:00", "end_time": "09:00"}],
            [{"id": "exam-1", "subject_name": "OS", "exam_date": target.isoformat(), "start_time": "10:00", "end_time": "12:00"}],
            target,
            deadlines=[{"source_signature": "deadline-1", "course_name": "OS", "activity_name": "Report", "due_date": f"{target.isoformat()}T23:59:00+07:00"}],
        )

        self.assertEqual({item["source_type"] for item in items}, {SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_EXAM, SYNC_SOURCE_DEADLINE})
        for item in items:
            props = item["payload"]["extendedProperties"]["private"]
            self.assertEqual(props["source"], calendar_sync.BOT_SOURCE_TAG)
            self.assertEqual(props["source_type"], item["source_type"])
            self.assertTrue(props["source_key"])

    def test_fetch_tagged_events_excludes_unowned_events(self) -> None:
        target = dt.date.today() + dt.timedelta(days=1)
        start = f"{target.isoformat()}T08:00:00+07:00"
        service = _Service([
            {"summary": "[EXAM] OS", "start": {"dateTime": start}, "end": {"dateTime": start}, "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "exam", "source_key": "exam:1"}}},
            {"summary": "Other", "start": {"dateTime": start}, "extendedProperties": {"private": {"source": "other", "source_type": "exam", "source_key": "exam:2"}}},
        ])
        with (
            patch.dict(os.environ, {"GOOGLE_CALENDAR_ID": "cal-id", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False),
            patch.object(calendar_sync, "_build_calendar_service", return_value=(service, "svc@example.com")),
        ):
            rows = fetch_tagged_calendar_events("exam", target_date=target, days_ahead=2)

        self.assertEqual(rows, [{"title": "[EXAM] OS", "start": start, "end": start, "location": "", "notes": "", "html_link": "", "source_key": "exam:1"}])

    def test_reconciliation_never_deletes_telegram_appointments(self) -> None:
        service = _Service([
            {"id": "appointment-id", "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "appointment", "source_key": "appointment:1"}}},
            {"id": "exam-id", "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "exam", "source_key": "exam:old"}}},
        ])
        _replace_bot_events_for_range(service, "cal-id", [], None, {SYNC_SOURCE_EXAM})

        self.assertEqual(service._events.deleted, ["exam-id"])

    def test_crawler_owns_only_crawled_source_types(self) -> None:
        self.assertEqual(_managed_source_types_for_crawler_sync(), {SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_EXAM, SYNC_SOURCE_DEADLINE})


if __name__ == "__main__":
    unittest.main()
