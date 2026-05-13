import unittest
import datetime as dt
import tempfile
from pathlib import Path
from unittest.mock import patch

from googleapiclient.errors import HttpError

import calendar_sync
from calendar_sync import _build_sync_items, _validate_calendar_target


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


if __name__ == "__main__":
    unittest.main()