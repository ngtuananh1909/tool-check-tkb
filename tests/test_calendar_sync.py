import unittest
import datetime as dt
import tempfile
from pathlib import Path
from unittest.mock import patch

from googleapiclient.errors import HttpError

import calendar_sync
from calendar_sync import (
    SYNC_SOURCE_APPOINTMENT,
    SYNC_SOURCE_CLASS_SESSION,
    SYNC_SOURCE_EXAM,
    SYNC_SOURCE_SCHEDULE,
    _build_sync_items,
    _managed_source_types_for_crawler_sync,
    _managed_source_types_for_database_sync,
    _replace_bot_events_for_range,
    _validate_calendar_target,
)


class _FakeCalendarsGet:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeCalendarsResource:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def get(self, calendarId):
        return _FakeCalendarsGet(self._result, self._error)


class _FakeService:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def calendars(self):
        return _FakeCalendarsResource(self._result, self._error)


class CalendarSyncValidationTests(unittest.TestCase):
    def test_validate_calendar_target_ignores_timeout(self) -> None:
        service = _FakeService(error=TimeoutError("timed out"))

        _validate_calendar_target(service, "test-calendar", "svc@example.com")

    def test_validate_calendar_target_still_rejects_primary(self) -> None:
        service = _FakeService(result={"id": "primary"})

        with self.assertRaises(RuntimeError):
            _validate_calendar_target(service, "primary", "svc@example.com")

    def test_validate_calendar_target_still_rejects_forbidden_calendar(self) -> None:
        class _Resp:
            status = 403
            reason = "Forbidden"

        error = HttpError(resp=_Resp(), content=b"forbidden")
        service = _FakeService(error=error)

        with self.assertRaises(RuntimeError):
            _validate_calendar_target(service, "shared-calendar", "svc@example.com")

    def test_exam_sync_items_are_red_with_one_week_reminder(self) -> None:
        target_date = dt.date(2026, 4, 25)
        exams = [
            {
                "id": "exam-1",
                "subject_name": "Toan Roi Rac",
                "exam_date": "2026-05-02",
                "start_time": "08:00",
                "end_time": "10:00",
                "exam_room": "A101",
                "notes": "Thi cuoi ky",
            }
        ]

        items = _build_sync_items([], [], exams, target_date)

        self.assertEqual(len(items), 1)
        payload = items[0]["payload"]
        self.assertEqual(payload.get("colorId"), "11")
        overrides = (payload.get("reminders") or {}).get("overrides") or []
        self.assertTrue(overrides)
        self.assertEqual(overrides[0].get("method"), "popup")
        self.assertEqual(overrides[0].get("minutes"), 10080)

    def test_class_sessions_add_all_contacts_as_attendees(self) -> None:
        target_date = dt.date(2026, 4, 25)
        schedule_rows = [
            {
                "subject_name": "Co So Du Lieu",
                "room": "B201",
                "day_of_week": "monday",
                "start_period": 1,
                "end_period": 2,
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            contact_file = Path(tmpdir) / "contact.txt"
            contact_file.write_text(
                "LMH - leminhhieu@gmail.com\nNVA - nguyenvana@gmail.com\n",
                encoding="utf-8",
            )
            with patch.object(calendar_sync, "CONTACT_FILE", str(contact_file)):
                items = _build_sync_items(schedule_rows, [], [], target_date)

        self.assertEqual(len(items), 1)
        attendees = items[0]["payload"].get("attendees") or []
        self.assertEqual({attendee["email"] for attendee in attendees}, {
            "leminhhieu@gmail.com",
            "nguyenvana@gmail.com",
        })

    def test_personal_appointments_do_not_add_contacts(self) -> None:
        target_date = dt.date(2026, 4, 25)
        appointments = [
            {
                "id": "appt-1",
                "title": "Hop nhom cung LMH",
                "appointment_date": "2026-04-25",
                "start_time": "14:00",
                "end_time": "15:00",
                "location": "Room 1",
                "note": "Trao doi bai tap",
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            contact_file = Path(tmpdir) / "contact.txt"
            contact_file.write_text(
                "LMH - leminhhieu@gmail.com\nNVA - nguyenvana@gmail.com\n",
                encoding="utf-8",
            )
            with patch.object(calendar_sync, "CONTACT_FILE", str(contact_file)):
                items = _build_sync_items([], appointments, [], target_date)

        self.assertEqual(len(items), 1)
        self.assertNotIn("attendees", items[0]["payload"])

    def test_class_session_description_uses_scraped_status_label(self) -> None:
        target_date = dt.date(2026, 4, 25)
        sessions = [
            {
                "id": "session-1",
                "subject_name": "Co So Du Lieu",
                "room": "B201",
                "session_date": "2026-04-25",
                "start_period": 1,
                "end_period": 2,
                "status": "makeup",
                "source_signature": "session-1",
            }
        ]

        items = _build_sync_items(sessions, [], [], target_date)

        self.assertEqual(items[0]["payload"]["description"], "Học bù")


# ---------------------------------------------------------------------------
# Fakes for _replace_bot_events_for_range tests
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.inserts: list[str] = []
        self.insert_bodies: list[dict] = []
        self.patches: list[str] = []
        self.deletes: list[str] = []


class _FakeEventsList:
    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def execute(self) -> dict:
        return {"items": self._items, "nextPageToken": None}


class _FakeEventsInsert:
    def __init__(self, recorder: _Recorder, body: dict) -> None:
        self._recorder = recorder
        self._body = body

    def execute(self) -> dict:
        eid = f"new-{len(self._recorder.inserts)}"
        self._recorder.inserts.append(eid)
        self._recorder.insert_bodies.append(self._body)
        return {"id": eid, "htmlLink": f"https://example.com/{eid}"}


class _FakeEventsPatch:
    def __init__(self, recorder: _Recorder, event_id: str, body: dict) -> None:
        self._recorder = recorder
        self._event_id = event_id
        self._body = body

    def execute(self) -> dict:
        self._recorder.patches.append(self._event_id)
        return {"id": self._event_id, "htmlLink": f"https://example.com/{self._event_id}"}


class _FakeEventsDelete:
    def __init__(self, recorder: _Recorder, event_id: str) -> None:
        self._recorder = recorder
        self._event_id = event_id

    def execute(self) -> dict:
        self._recorder.deletes.append(self._event_id)
        return {}


class _FakeEventsResource:
    def __init__(self, recorder: _Recorder, items: list[dict]) -> None:
        self._recorder = recorder
        self._items = items

    def list(self, **_kwargs) -> _FakeEventsList:
        return _FakeEventsList(self._items)

    def insert(self, calendarId: str, body: dict) -> _FakeEventsInsert:
        return _FakeEventsInsert(self._recorder, body)

    def patch(self, calendarId: str, eventId: str, body: dict) -> _FakeEventsPatch:
        return _FakeEventsPatch(self._recorder, eventId, body)

    def delete(self, calendarId: str, eventId: str) -> _FakeEventsDelete:
        return _FakeEventsDelete(self._recorder, eventId)


class _OwnershipFakeCalendarsGet:
    def execute(self) -> dict:
        return {"id": "cal-id"}


class _OwnershipFakeCalendarsResource:
    def get(self, calendarId: str) -> _OwnershipFakeCalendarsGet:
        return _OwnershipFakeCalendarsGet()


class _OwnershipFakeService:
    def __init__(self, recorder: _Recorder, items: list[dict]) -> None:
        self._recorder = recorder
        self._events = _FakeEventsResource(recorder, items)

    def events(self) -> _FakeEventsResource:
        return self._events

    def calendars(self) -> _OwnershipFakeCalendarsResource:
        return _OwnershipFakeCalendarsResource()


def _event(eid: str, source_type: str, source_key: str) -> dict:
    return {
        "id": eid,
        "extendedProperties": {
            "private": {
                "source": "tool-check-tkb",
                "source_type": source_type,
                "source_key": source_key,
            }
        },
    }


def _sync_item(source_type: str, source_key: str) -> dict:
    return {
        "source_type": source_type,
        "source_key": source_key,
        "source_hash": "h",
        "payload": {"summary": "x"},
    }


class ManagedSourceTypesHelperTests(unittest.TestCase):
    def test_crawler_sync_owns_class_session_and_exam(self) -> None:
        self.assertEqual(
            _managed_source_types_for_crawler_sync(),
            frozenset({SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_EXAM}),
        )

    def test_database_sync_with_class_sessions(self) -> None:
        self.assertEqual(
            _managed_source_types_for_database_sync(use_class_sessions=True),
            frozenset(
                {SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_APPOINTMENT, SYNC_SOURCE_EXAM}
            ),
        )

    def test_database_sync_with_schedule(self) -> None:
        self.assertEqual(
            _managed_source_types_for_database_sync(use_class_sessions=False),
            frozenset(
                {SYNC_SOURCE_SCHEDULE, SYNC_SOURCE_APPOINTMENT, SYNC_SOURCE_EXAM}
            ),
        )


class ReplaceBotEventsOwnershipTests(unittest.TestCase):
    """Regression tests for the /add deletion bug."""

    def test_empty_sync_items_keeps_appointments(self) -> None:
        """Bug gốc: TKB rỗng không được xóa appointments do /add tạo."""
        existing = [
            _event("evt-appt-1", SYNC_SOURCE_APPOINTMENT, "appointment:2026-06-22:Họp nhóm"),
        ]
        recorder = _Recorder()
        service = _OwnershipFakeService(recorder, existing)

        _replace_bot_events_for_range(
            service,
            "cal",
            [],
            None,
            frozenset({SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_EXAM}),
        )

        self.assertEqual(recorder.deletes, [])
        self.assertEqual(recorder.inserts, [])
        self.assertEqual(recorder.patches, [])

    def test_only_managed_source_type_is_deleted(self) -> None:
        """Khi sync class_session mới, chỉ class_session cũ bị xóa."""
        existing = [
            _event("evt-appt", SYNC_SOURCE_APPOINTMENT, "appointment:2026-06-22:Họp"),
            _event("evt-cs-old", SYNC_SOURCE_CLASS_SESSION, "class_session:abc"),
        ]
        recorder = _Recorder()
        service = _OwnershipFakeService(recorder, existing)
        sync_items = [_sync_item(SYNC_SOURCE_CLASS_SESSION, "class_session:new")]

        _replace_bot_events_for_range(
            service,
            "cal",
            sync_items,
            None,
            frozenset({SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_EXAM}),
        )

        self.assertIn("evt-cs-old", recorder.deletes)
        self.assertNotIn("evt-appt", recorder.deletes)

    def test_exam_events_deleted_by_crawler_sync(self) -> None:
        """Crawler sync owns exam → xóa được exam cũ (không thuộc current_keys)."""
        existing = [
            _event("evt-exam-old", SYNC_SOURCE_EXAM, "exam:xyz"),
        ]
        recorder = _Recorder()
        service = _OwnershipFakeService(recorder, existing)

        _replace_bot_events_for_range(
            service,
            "cal",
            [],
            None,
            frozenset({SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_EXAM}),
        )

        self.assertEqual(recorder.deletes, ["evt-exam-old"])

    def test_schedule_events_skipped_by_crawler_sync(self) -> None:
        """Crawler sync KHÔNG owns schedule → schedule events được bảo vệ."""
        existing = [
            _event("evt-sched", SYNC_SOURCE_SCHEDULE, "schedule:monday-1-2"),
        ]
        recorder = _Recorder()
        service = _OwnershipFakeService(recorder, existing)

        with self.assertLogs(calendar_sync.logger, level="WARNING") as cm:
            _replace_bot_events_for_range(
                service,
                "cal",
                [],
                None,
                frozenset({SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_EXAM}),
            )

        self.assertEqual(recorder.deletes, [])
        self.assertTrue(
            any("owned by other source types" in m for m in cm.output),
            cm.output,
        )

    def test_legacy_event_without_source_type_not_deleted(self) -> None:
        """Event không có source_type → chỉ log warning, không xóa."""
        existing = [
            {"id": "evt-legacy", "extendedProperties": {"private": {"source": "tool-check-tkb"}}},
        ]
        recorder = _Recorder()
        service = _OwnershipFakeService(recorder, existing)

        with self.assertLogs(calendar_sync.logger, level="WARNING") as cm:
            _replace_bot_events_for_range(
                service,
                "cal",
                [],
                None,
                frozenset({SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_EXAM}),
            )

        self.assertEqual(recorder.deletes, [])
        self.assertTrue(
            any("legacy" in m.lower() for m in cm.output),
            cm.output,
        )

    def test_database_sync_deletes_appointments(self) -> None:
        """DB sync owns appointment → xóa appointment cũ khi sync mới không có nó."""
        existing = [
            _event("evt-appt", SYNC_SOURCE_APPOINTMENT, "appointment:old"),
        ]
        recorder = _Recorder()
        service = _OwnershipFakeService(recorder, existing)

        _replace_bot_events_for_range(
            service,
            "cal",
            [],
            None,
            frozenset(
                {SYNC_SOURCE_SCHEDULE, SYNC_SOURCE_APPOINTMENT, SYNC_SOURCE_EXAM}
            ),
        )

        self.assertEqual(recorder.deletes, ["evt-appt"])

    def test_matching_source_key_is_kept_via_patch(self) -> None:
        """Event đã có source_key khớp sync_items → patch (không xóa)."""
        existing = [
            _event("evt-cs", SYNC_SOURCE_CLASS_SESSION, "class_session:abc"),
        ]
        recorder = _Recorder()
        service = _OwnershipFakeService(recorder, existing)
        sync_items = [_sync_item(SYNC_SOURCE_CLASS_SESSION, "class_session:abc")]

        _replace_bot_events_for_range(
            service,
            "cal",
            sync_items,
            None,
            frozenset({SYNC_SOURCE_CLASS_SESSION, SYNC_SOURCE_EXAM}),
        )

        self.assertEqual(recorder.deletes, [])
        # patch hoặc skip (nếu hash giống). Không quan trọng insert ở đây.
        self.assertEqual(len(recorder.inserts) + len(recorder.patches), 1)


if __name__ == "__main__":
    unittest.main()