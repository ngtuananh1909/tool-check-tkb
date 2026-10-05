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
    _replace_bot_events_for_range,
    fetch_tagged_calendar_events,
)


class _Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _Events:
    def __init__(self, events, page_size=None):
        self.events = events
        self.page_size = page_size
        self.deleted = []
        self.inserted = []
        self.patched = []
        self.list_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        events = self.events
        property_filter = kwargs.get("privateExtendedProperty")
        if property_filter:
            property_name, property_value = property_filter.split("=", 1)
            events = [
                event for event in events
                if (event.get("extendedProperties") or {}).get("private", {}).get(property_name) == property_value
            ]

        def event_time(event, field):
            value = (event.get(field) or {}).get("dateTime")
            if value:
                try:
                    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return None
            value = (event.get(field) or {}).get("date")
            if value:
                try:
                    return dt.datetime.combine(dt.date.fromisoformat(value), dt.time.min, dt.timezone.utc)
                except ValueError:
                    return None
            return None

        lower = dt.datetime.fromisoformat(kwargs["timeMin"]) if kwargs.get("timeMin") else None
        upper = dt.datetime.fromisoformat(kwargs["timeMax"]) if kwargs.get("timeMax") else None

        def in_time_range(event):
            start = event_time(event, "start")
            end = event_time(event, "end") or (start + dt.timedelta(hours=1) if start else None)
            if lower is not None and end is not None and end <= lower:
                return False
            if upper is not None and start is not None and start >= upper:
                return False
            return True

        events = [event for event in events if in_time_range(event)]

        offset = int(kwargs.get("pageToken") or 0)
        page_size = self.page_size or len(events) or 1
        page = events[offset:offset + page_size]
        result = {"items": page}
        if offset + page_size < len(events):
            result["nextPageToken"] = str(offset + page_size)
        return _Request(result)

    def insert(self, **kwargs):
        self.inserted.append(kwargs["body"])
        return _Request({"id": "new-event"})

    def patch(self, **kwargs):
        self.patched.append(kwargs)
        return _Request({"id": kwargs["eventId"]})

    def delete(self, **kwargs):
        self.deleted.append(kwargs["eventId"])
        return _Request({})


class _Service:
    def __init__(self, events, page_size=None):
        self._events = _Events(events, page_size=page_size)

    def events(self):
        return self._events


def _event(event_id, source_type, start, source_key=None, source_hash="", source=calendar_sync.BOT_SOURCE_TAG):
    return {
        "id": event_id,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + dt.timedelta(hours=1)).isoformat()},
        "extendedProperties": {
            "private": {
                "source": source,
                "source_type": source_type,
                "source_key": source_key or f"{source_type}:{event_id}",
                "source_hash": source_hash,
            }
        },
    }


class CalendarOnlySyncTests(unittest.TestCase):
    def test_class_reconciliation_deletes_only_inside_schedule_window(self) -> None:
        start = dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc)
        service = _Service([
            _event("before", SYNC_SOURCE_CLASS_SESSION, start - dt.timedelta(minutes=30)),
            _event("at-start", SYNC_SOURCE_CLASS_SESSION, start),
            _event("inside", SYNC_SOURCE_CLASS_SESSION, start + dt.timedelta(days=7)),
            _event("at-end", SYNC_SOURCE_CLASS_SESSION, end),
            _event("future", SYNC_SOURCE_CLASS_SESSION, end + dt.timedelta(days=7)),
            _event("appointment", "appointment", start + dt.timedelta(days=1)),
            _event("exam", SYNC_SOURCE_EXAM, start + dt.timedelta(days=1)),
            _event("deadline", SYNC_SOURCE_DEADLINE, start + dt.timedelta(days=1)),
            _event("unowned", SYNC_SOURCE_CLASS_SESSION, start + dt.timedelta(days=1), source="someone-else"),
        ])

        _replace_bot_events_for_range(
            service, "cal-id", [], None, {SYNC_SOURCE_CLASS_SESSION}, schedule_window=(start, end)
        )

        self.assertEqual(sorted(service._events.deleted), ["at-start", "inside"])
        self.assertEqual(len(service._events.list_calls), 1)
        self.assertEqual(service._events.list_calls[0]["timeMin"], start.isoformat())
        self.assertEqual(service._events.list_calls[0]["timeMax"], end.isoformat())

    def test_class_reconciliation_processes_all_bounded_pages(self) -> None:
        start = dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(weeks=3)
        service = _Service([
            _event("first", SYNC_SOURCE_CLASS_SESSION, start + dt.timedelta(days=1)),
            _event("second", SYNC_SOURCE_CLASS_SESSION, start + dt.timedelta(days=2)),
        ], page_size=1)

        _replace_bot_events_for_range(
            service, "cal-id", [], None, {SYNC_SOURCE_CLASS_SESSION}, schedule_window=(start, end)
        )

        self.assertEqual(sorted(service._events.deleted), ["first", "second"])
        self.assertEqual([call["pageToken"] for call in service._events.list_calls], [None, "1"])
        self.assertTrue(all(call["timeMin"] == start.isoformat() and call["timeMax"] == end.isoformat()
                            for call in service._events.list_calls))

    def test_matching_class_is_kept_and_patched_only_when_hash_changes(self) -> None:
        start = dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(weeks=3)
        service = _Service([_event("same", SYNC_SOURCE_CLASS_SESSION, start, source_hash="old")])
        item = {
            "source_type": SYNC_SOURCE_CLASS_SESSION,
            "source_key": "class_session:same",
            "source_hash": "old",
            "payload": {"summary": "Class"},
        }

        _replace_bot_events_for_range(
            service, "cal-id", [item], None, {SYNC_SOURCE_CLASS_SESSION}, schedule_window=(start, end)
        )
        self.assertEqual(service._events.deleted, [])
        self.assertEqual(service._events.patched, [])

        item["source_hash"] = "new"
        _replace_bot_events_for_range(
            service, "cal-id", [item], None, {SYNC_SOURCE_CLASS_SESSION}, schedule_window=(start, end)
        )
        self.assertEqual([call["eventId"] for call in service._events.patched], ["same"])

    def test_exam_lookup_patches_matching_owned_key_without_deleting_history(self) -> None:
        old_date = dt.datetime(2025, 10, 1, tzinfo=dt.timezone.utc)
        service = _Service([
            _event("historical", SYNC_SOURCE_EXAM, old_date),
            _event("current", SYNC_SOURCE_EXAM, old_date, source_hash="old"),
            _event("other-owner", SYNC_SOURCE_EXAM, old_date, source_key="exam:current", source="other"),
        ])
        item = {
            "source_type": SYNC_SOURCE_EXAM,
            "source_key": "exam:current",
            "source_hash": "new",
            "payload": {"summary": "Updated exam"},
        }

        _replace_bot_events_for_range(service, "cal-id", [item], None, {SYNC_SOURCE_EXAM})

        self.assertEqual(service._events.deleted, [])
        self.assertEqual([call["eventId"] for call in service._events.patched], ["current"])
        self.assertEqual(len(service._events.list_calls), 1)
        self.assertEqual(service._events.list_calls[0]["privateExtendedProperty"], "source_key=exam:current")
        self.assertNotIn("timeMin", service._events.list_calls[0])

    def test_class_none_is_not_reconciled_during_deadline_sync(self) -> None:
        start = dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(weeks=3)
        service = _Service([_event("old-class", SYNC_SOURCE_CLASS_SESSION, start + dt.timedelta(days=1))])
        with (
            patch.dict(os.environ, {"GOOGLE_CALENDAR_ID": "cal-id", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False),
            patch.object(calendar_sync, "_build_calendar_service", return_value=(service, "svc@example.com")),
            patch.object(calendar_sync, "_validate_calendar_target"),
        ):
            calendar_sync.sync_crawled_data_to_google_calendar(
                None, None, deadlines=[], deadline_window=(start, end)
            )

        self.assertEqual(service._events.deleted, [])
        self.assertTrue(all(call["timeMin"] == start.isoformat() for call in service._events.list_calls))

    def test_schedule_list_requires_valid_authoritative_window(self) -> None:
        start = dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(weeks=3)
        invalid_windows = [None, [start, end], (start,), ("2026-09-28", end),
                           (start.replace(tzinfo=None), end), (start, start), (end, start)]
        for window in invalid_windows:
            with self.subTest(window=window), self.assertRaises(ValueError):
                calendar_sync.sync_crawled_data_to_google_calendar([], None, schedule_window=window)

        with self.assertRaises(ValueError):
            calendar_sync.sync_crawled_data_to_google_calendar(None, None, schedule_window=(start, end))

    def test_class_row_outside_window_fails_before_calendar_access(self) -> None:
        start = dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(weeks=3)
        with patch.object(calendar_sync, "_build_calendar_service") as build_service:
            with self.assertRaises(ValueError):
                calendar_sync.sync_crawled_data_to_google_calendar(
                    [{"id": "outside", "session_date": "2026-10-19", "start_time": "08:00", "end_time": "09:00"}],
                    None,
                    schedule_window=(start, end),
                )
        build_service.assert_not_called()

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

        self.assertEqual(service._events.deleted, [])
        self.assertEqual(service._events.list_calls, [])


    def test_deadlines_list_without_window_raises_error(self) -> None:
        with self.assertRaises(ValueError):
            calendar_sync.sync_crawled_data_to_google_calendar(None, None, deadlines=[], deadline_window=None)

    def test_deadlines_none_with_window_raises_error(self) -> None:
        tz = dt.timezone.utc
        start = dt.datetime.now(tz)
        end = start + dt.timedelta(days=30)
        with self.assertRaises(ValueError):
            calendar_sync.sync_crawled_data_to_google_calendar(None, None, deadlines=None, deadline_window=(start, end))

    def test_window_validation_rejects_non_tuple(self) -> None:
        tz = dt.timezone.utc
        start = dt.datetime.now(tz)
        end = start + dt.timedelta(days=30)
        with self.assertRaises(ValueError):
            calendar_sync.sync_crawled_data_to_google_calendar(None, None, deadlines=[], deadline_window=[start, end])  # type: ignore

    def test_window_validation_rejects_invalid_tuple_length(self) -> None:
        tz = dt.timezone.utc
        start = dt.datetime.now(tz)
        with self.assertRaises(ValueError):
            calendar_sync.sync_crawled_data_to_google_calendar(None, None, deadlines=[], deadline_window=(start,))  # type: ignore

    def test_window_validation_rejects_non_datetime_elements(self) -> None:
        with self.assertRaises(ValueError):
            calendar_sync.sync_crawled_data_to_google_calendar(None, None, deadlines=[], deadline_window=("2026-09-01", "2026-10-01"))  # type: ignore

    def test_window_validation_rejects_naive_datetime(self) -> None:
        start = dt.datetime(2026, 9, 1, 0, 0, 0)  # naive
        end = dt.datetime(2026, 10, 1, 0, 0, 0)    # naive
        with self.assertRaises(ValueError):
            calendar_sync.sync_crawled_data_to_google_calendar(None, None, deadlines=[], deadline_window=(start, end))

    def test_window_validation_rejects_inverted_range(self) -> None:
        tz = dt.timezone.utc
        start = dt.datetime.now(tz)
        end = start - dt.timedelta(days=1)
        with self.assertRaises(ValueError):
            calendar_sync.sync_crawled_data_to_google_calendar(None, None, deadlines=[], deadline_window=(start, end))

        # Equal start and end also rejected
        with self.assertRaises(ValueError):
            calendar_sync.sync_crawled_data_to_google_calendar(None, None, deadlines=[], deadline_window=(start, start))

    def test_deadline_window_boundary_preserves_out_of_window_events(self) -> None:
        tz = dt.timezone.utc
        now = dt.datetime.now(tz)
        window_start = now
        window_end = now + dt.timedelta(days=30)
        deadline_window = (window_start, window_end)

        # Existing events:
        # 1. Inside window (now + 10d) -> deleted if absent from crawl
        # 2. Before window (now - 5d) -> preserved
        # 3. At window_end (now + 30d) -> preserved (half-open [start, end))
        # 4. At window_start (now) -> deleted if absent from crawl
        events = [
            {
                "id": "inside-id",
                "start": {"dateTime": (now + dt.timedelta(days=10)).isoformat()},
                "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "deadline", "source_key": "deadline:moodle_event:1"}},
            },
            {
                "id": "before-id",
                "start": {"dateTime": (now - dt.timedelta(days=5)).isoformat()},
                "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "deadline", "source_key": "deadline:moodle_event:2"}},
            },
            {
                "id": "at-end-id",
                "start": {"dateTime": window_end.isoformat()},
                "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "deadline", "source_key": "deadline:moodle_event:3"}},
            },
            {
                "id": "at-start-id",
                "start": {"dateTime": window_start.isoformat()},
                "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "deadline", "source_key": "deadline:moodle_event:4"}},
            },
        ]
        service = _Service(events)

        # Reconcile empty deadlines list [] inside [window_start, window_end)
        _replace_bot_events_for_range(service, "cal-id", [], None, {SYNC_SOURCE_DEADLINE}, deadline_window=deadline_window)

        # Only inside-id and at-start-id should be deleted. before-id and at-end-id are preserved.
        self.assertEqual(sorted(service._events.deleted), ["at-start-id", "inside-id"])
        self.assertEqual(service._events.list_calls[0]["timeMin"], window_start.isoformat())
        self.assertEqual(service._events.list_calls[0]["timeMax"], window_end.isoformat())

    def test_deadline_missing_start_is_preserved(self) -> None:
        tz = dt.timezone.utc
        now = dt.datetime.now(tz)
        deadline_window = (now, now + dt.timedelta(days=30))
        events = [
            {
                "id": "missing-start-id",
                "start": {},
                "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "deadline", "source_key": "deadline:moodle_event:missing"}},
            }
        ]
        service = _Service(events)
        _replace_bot_events_for_range(service, "cal-id", [], None, {SYNC_SOURCE_DEADLINE}, deadline_window=deadline_window)
        self.assertEqual(service._events.deleted, [])

    def test_deadline_invalid_datetime_is_preserved(self) -> None:
        tz = dt.timezone.utc
        now = dt.datetime.now(tz)
        deadline_window = (now, now + dt.timedelta(days=30))
        events = [
            {
                "id": "bad-datetime-id",
                "start": {"dateTime": "invalid-iso-string"},
                "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "deadline", "source_key": "deadline:moodle_event:bad"}},
            }
        ]
        service = _Service(events)
        _replace_bot_events_for_range(service, "cal-id", [], None, {SYNC_SOURCE_DEADLINE}, deadline_window=deadline_window)
        self.assertEqual(service._events.deleted, [])

    def test_deadline_invalid_date_is_preserved(self) -> None:
        tz = dt.timezone.utc
        now = dt.datetime.now(tz)
        deadline_window = (now, now + dt.timedelta(days=30))
        events = [
            {
                "id": "bad-date-id",
                "start": {"date": "invalid-date-string"},
                "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "deadline", "source_key": "deadline:moodle_event:baddate"}},
            }
        ]
        service = _Service(events)
        _replace_bot_events_for_range(service, "cal-id", [], None, {SYNC_SOURCE_DEADLINE}, deadline_window=deadline_window)
        self.assertEqual(service._events.deleted, [])

    def test_failed_crawl_preserves_all_deadlines(self) -> None:
        target = dt.date.today()
        start = f"{target.isoformat()}T08:00:00+07:00"
        events = [
            {
                "id": "deadline-id",
                "start": {"dateTime": start},
                "extendedProperties": {"private": {"source": calendar_sync.BOT_SOURCE_TAG, "source_type": "deadline", "source_key": "deadline:moodle_event:keep"}},
            }
        ]
        service = _Service(events)
        with (
            patch.dict(os.environ, {"GOOGLE_CALENDAR_ID": "cal-id", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False),
            patch.object(calendar_sync, "_build_calendar_service", return_value=(service, "svc@example.com")),
            patch.object(calendar_sync, "_validate_calendar_target", return_value=None),
        ):
            # deadlines=None means crawl failed -> managed types will only be class_sessions / exams if provided
            calendar_sync.sync_crawled_data_to_google_calendar(
                class_sessions=None, exams=[], student_id="test", deadlines=None, deadline_window=None
            )
        self.assertEqual(service._events.deleted, [])

    def test_fetch_events_from_calendar_reads_period_and_status_metadata(self) -> None:
        target = dt.date.today()
        start_str = f"{target.isoformat()}T06:50:00+07:00"
        end_str = f"{target.isoformat()}T09:20:00+07:00"
        service = _Service([
            {
                "summary": "Web Programming",
                "location": "C204",
                "start": {"dateTime": start_str},
                "end": {"dateTime": end_str},
                "extendedProperties": {
                    "private": {
                        "source": calendar_sync.BOT_SOURCE_TAG,
                        "source_type": SYNC_SOURCE_CLASS_SESSION,
                        "start_period": "1",
                        "end_period": "3",
                        "class_status": "makeup",
                    }
                },
            }
        ])
        with (
            patch.dict(os.environ, {"GOOGLE_CALENDAR_ID": "cal-id", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False),
            patch.object(calendar_sync, "_build_calendar_service", return_value=(service, "svc@example.com")),
        ):
            classes, _, _ = calendar_sync.fetch_events_from_calendar(target)

        self.assertEqual(len(classes), 1)
        row = classes[0]
        self.assertEqual(row["subject_name"], "Web Programming")
        self.assertEqual(row["start_period"], 1)
        self.assertEqual(row["end_period"], 3)
        self.assertEqual(row["status"], "makeup")
        self.assertEqual(row["start_time"], "06:50")
        self.assertEqual(row["end_time"], "09:20")

    def test_build_sync_items_from_sessions_includes_period_and_status(self) -> None:
        target = dt.date.today()
        items = _build_sync_items_from_sessions(
            [
                {
                    "id": "class-1",
                    "subject_name": "Web Programming",
                    "room": "C204",
                    "session_date": target.isoformat(),
                    "start_time": "06:50",
                    "end_time": "09:20",
                    "start_period": 1,
                    "end_period": 3,
                    "status": "scheduled",
                }
            ],
            [],
            target,
        )
        self.assertEqual(len(items), 1)
        props = items[0]["payload"]["extendedProperties"]["private"]
        self.assertEqual(props["start_period"], "1")
        self.assertEqual(props["end_period"], "3")
        self.assertEqual(props["class_status"], "scheduled")

    def test_build_sync_items_periods_2_to_6_time_calculation(self) -> None:
        target = dt.date(2026, 10, 6)
        items = _build_sync_items_from_sessions(
            [
                {
                    "subject_name": "Những kỹ năng thiết yếu",
                    "room": "C411-A",
                    "session_date": target.isoformat(),
                    "start_period": 2,
                    "end_period": 6,
                    "status": "scheduled",
                }
            ],
            [],
            target,
        )
        self.assertEqual(len(items), 1)
        payload = items[0]["payload"]
        self.assertTrue(payload["start"]["dateTime"].endswith("07:40:00+07:00"))
        self.assertTrue(payload["end"]["dateTime"].endswith("12:00:00+07:00"))

    def test_build_sync_items_overlapping_sessions_distinct_source_keys(self) -> None:
        target = dt.date(2026, 10, 5)
        sessions = [
            {
                "subject_name": "GDTC 1 - Taekwondo",
                "room": "TRET-NTD-4",
                "session_date": target.isoformat(),
                "start_period": 1,
                "end_period": 3,
                "status": "scheduled",
            },
            {
                "subject_name": "Kinh tế chính trị Mác-Lênin",
                "room": "D0301-A",
                "session_date": target.isoformat(),
                "start_period": 1,
                "end_period": 3,
                "status": "makeup",
            },
        ]
        items = _build_sync_items_from_sessions(sessions, [], target)
        self.assertEqual(len(items), 2)
        # Verify both start and end at 06:50 - 09:20
        for item in items:
            self.assertTrue(item["payload"]["start"]["dateTime"].endswith("06:50:00+07:00"))
            self.assertTrue(item["payload"]["end"]["dateTime"].endswith("09:20:00+07:00"))

        # Verify distinct source keys
        keys = [item["source_key"] for item in items]
        self.assertEqual(len(set(keys)), 2)

        # Verify makeup colorId is "7"
        makeup_item = next(i for i in items if "kinh tế chính trị" in i["payload"]["summary"].lower())
        self.assertEqual(makeup_item["payload"].get("colorId"), "7")


if __name__ == "__main__":
    unittest.main()
