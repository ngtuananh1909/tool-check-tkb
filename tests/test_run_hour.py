import unittest
from unittest.mock import patch

import calendar_sync
import crawler
import run_hour


class RunHourlySyncTests(unittest.TestCase):
    def test_crawler_results_are_sent_straight_to_calendar(self) -> None:
        with (
            patch.object(run_hour, "_load_dotenv"),
            patch.object(crawler, "get_current_semester", return_value="HK2/2025-2026"),
            patch.object(crawler, "fetch_schedule", return_value=[{"id": "class-1"}]),
            patch.object(crawler, "fetch_exam_schedule", return_value=[{"id": "exam-1"}]),
            patch.object(crawler, "fetch_elearning_deadlines", return_value=[{"activity_name": "Report"}]),
            patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=("", True)) as sync_calendar,
        ):
            run_hour.run_hourly_sync()

        sync_calendar.assert_called_once_with(
            [{"id": "class-1"}], [{"id": "exam-1"}], student_id=None,
            deadlines=[{"activity_name": "Report"}],
        )

    def test_failed_schedule_does_not_allow_calendar_to_delete_existing_classes(self) -> None:
        with (
            patch.object(run_hour, "_load_dotenv"),
            patch.object(crawler, "get_current_semester", return_value="HK2/2025-2026"),
            patch.object(crawler, "fetch_schedule", side_effect=RuntimeError("portal timeout")),
            patch.object(crawler, "fetch_exam_schedule", return_value=[]),
            patch.object(crawler, "fetch_elearning_deadlines", return_value=[]),
            patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=("", True)) as sync_calendar,
        ):
            run_hour.run_hourly_sync()

        sync_calendar.assert_called_once_with(None, [], student_id=None, deadlines=[])

    def test_failed_deadline_crawl_preserves_existing_deadline_events(self) -> None:
        with (
            patch.object(run_hour, "_load_dotenv"),
            patch.object(crawler, "get_current_semester", return_value="HK2/2025-2026"),
            patch.object(crawler, "fetch_schedule", return_value=[]),
            patch.object(crawler, "fetch_exam_schedule", return_value=[]),
            patch.object(crawler, "fetch_elearning_deadlines", side_effect=RuntimeError("eLearning unavailable")),
            patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=("", True)) as sync_calendar,
        ):
            run_hour.run_hourly_sync()

        sync_calendar.assert_called_once_with([], [], student_id=None, deadlines=None)


if __name__ == "__main__":
    unittest.main()
