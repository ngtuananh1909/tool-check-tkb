import unittest
from unittest.mock import patch

import crawler
import calendar_sync
import database
import run_hour


class RunHourlySyncTests(unittest.TestCase):
    def test_calendar_sync_runs_before_supabase_upserts(self) -> None:
        call_order: list[str] = []
        with (
            patch.object(run_hour, "_load_dotenv"),
            patch.object(crawler, "get_current_semester", return_value="HK2/2025-2026"),
            patch.object(crawler, "fetch_schedule", return_value=[]),
            patch.object(crawler, "fetch_exam_schedule", return_value=[]),
            patch.object(crawler, "fetch_elearning_progress", return_value=[]),
            patch.object(crawler, "fetch_elearning_deadlines", return_value=[]),
            patch.object(
                calendar_sync,
                "sync_crawled_data_to_google_calendar",
                side_effect=lambda *args, **kwargs: (call_order.append("calendar"), ("", True))[1],
            ),
            patch.object(
                database,
                "upsert_elearning_progress",
                side_effect=lambda *args, **kwargs: call_order.append("progress"),
            ),
            patch.object(
                database,
                "upsert_elearning_deadlines",
                side_effect=lambda *args, **kwargs: call_order.append("deadlines"),
            ),
        ):
            run_hour.run_hourly_sync()

        self.assertEqual(call_order, ["calendar", "progress", "deadlines"])

    def test_elearning_progress_failure_does_not_block_calendar_or_other_elearning_upsert(self) -> None:
        with (
            patch.object(run_hour, "_load_dotenv"),
            patch.object(crawler, "get_current_semester", return_value="HK2/2025-2026"),
            patch.object(crawler, "fetch_schedule", return_value=[]),
            patch.object(crawler, "fetch_exam_schedule", return_value=[]),
            patch.object(crawler, "fetch_elearning_progress", side_effect=RuntimeError("eLearning unavailable")),
            patch.object(crawler, "fetch_elearning_deadlines", return_value=[]),
            patch.object(database, "upsert_elearning_progress") as upsert_progress,
            patch.object(database, "upsert_elearning_deadlines", return_value=0) as upsert_deadlines,
            patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=([], True)) as sync_calendar,
        ):
            run_hour.run_hourly_sync()

        upsert_progress.assert_not_called()
        upsert_deadlines.assert_called_once_with([], student_id=None)
        sync_calendar.assert_called_once_with([], [], student_id=None, deadlines=[])

    def test_elearning_deadline_failure_skips_only_deadline_upsert(self) -> None:
        with (
            patch.object(run_hour, "_load_dotenv"),
            patch.object(crawler, "get_current_semester", return_value="HK2/2025-2026"),
            patch.object(crawler, "fetch_schedule", return_value=[]),
            patch.object(crawler, "fetch_exam_schedule", return_value=[]),
            patch.object(crawler, "fetch_elearning_progress", return_value=[]),
            patch.object(crawler, "fetch_elearning_deadlines", side_effect=RuntimeError("eLearning unavailable")),
            patch.object(database, "upsert_elearning_progress", return_value=0) as upsert_progress,
            patch.object(database, "upsert_elearning_deadlines") as upsert_deadlines,
            patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=([], True)) as sync_calendar,
        ):
            run_hour.run_hourly_sync()

        upsert_progress.assert_called_once_with([], student_id=None)
        upsert_deadlines.assert_not_called()
        sync_calendar.assert_called_once_with([], [], student_id=None, deadlines=None)

    def test_schedule_failure_keeps_exam_sync_without_deleting_schedule_events(self) -> None:
        with (
            patch.object(run_hour, "_load_dotenv"),
            patch.object(crawler, "get_current_semester", return_value="HK2/2025-2026"),
            patch.object(crawler, "fetch_schedule", side_effect=RuntimeError("portal timeout")),
            patch.object(crawler, "fetch_exam_schedule", return_value=[]),
            patch.object(crawler, "fetch_elearning_progress", return_value=[]),
            patch.object(crawler, "fetch_elearning_deadlines", return_value=[]),
            patch.object(database, "upsert_elearning_progress", return_value=0),
            patch.object(database, "upsert_elearning_deadlines", return_value=0),
            patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=("", True)) as sync_calendar,
        ):
            run_hour.run_hourly_sync()

        sync_calendar.assert_called_once_with(None, [], student_id=None, deadlines=[])
