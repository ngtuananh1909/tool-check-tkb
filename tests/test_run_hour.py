"""
Unit tests for run_hour.py hourly sync orchestrator and snapshot fallback handling.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import calendar_sync
import run_hour
from tdtu.snapshot import FetchResult, PortalSnapshot


class RunHourlySyncTests(unittest.TestCase):

    @patch.dict(os.environ, {"STUDENT_ID": "TEST_STUDENT_001", "PASSWORD": "TEST_PASSWORD_NOT_A_SECRET"})
    @patch("tdtu.fetch_portal_snapshot")
    @patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=("", True))
    @patch("crawler.fetch_elearning_deadlines", return_value=[])
    @patch.object(run_hour, "_load_dotenv")
    def test_snapshot_both_successful_sends_data_to_calendar(
        self, mock_dotenv: MagicMock, mock_deadlines: MagicMock, mock_sync: MagicMock, mock_snapshot: MagicMock
    ) -> None:
        mock_snapshot.return_value = PortalSnapshot(
            semester=FetchResult(success=True, data="HK1/2026-2027"),
            schedule=FetchResult(success=True, data=[{"subject_name": "CSDL"}]),
            exams=FetchResult(success=True, data=[{"subject_name": "Exam CSDL"}]),
        )

        run_hour.run_hourly_sync()

        mock_sync.assert_called_once_with(
            [{"subject_name": "CSDL"}],
            [{"subject_name": "Exam CSDL"}],
            student_id="TEST_STUDENT_001",
            deadlines=[],
        )

    @patch.dict(os.environ, {"STUDENT_ID": "TEST_STUDENT_001", "PASSWORD": "TEST_PASSWORD_NOT_A_SECRET"})
    @patch("tdtu.fetch_portal_snapshot")
    @patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=("", True))
    @patch("crawler.fetch_elearning_deadlines", return_value=[])
    @patch.object(run_hour, "_load_dotenv")
    def test_snapshot_both_empty_sends_empty_lists_no_fallback(
        self, mock_dotenv: MagicMock, mock_deadlines: MagicMock, mock_sync: MagicMock, mock_snapshot: MagicMock
    ) -> None:
        mock_snapshot.return_value = PortalSnapshot(
            semester=FetchResult(success=True, data="HK1/2026-2027"),
            schedule=FetchResult(success=True, data=[]),
            exams=FetchResult(success=True, data=[]),
        )

        run_hour.run_hourly_sync()

        mock_sync.assert_called_once_with(
            [],
            [],
            student_id="TEST_STUDENT_001",
            deadlines=[],
        )

    @patch.dict(os.environ, {"STUDENT_ID": "TEST_STUDENT_001", "PASSWORD": "TEST_PASSWORD_NOT_A_SECRET"})
    @patch("crawler._fetch_schedule_playwright", return_value=[{"subject_name": "Fallback Class"}])
    @patch("tdtu.fetch_portal_snapshot")
    @patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=("", True))
    @patch("crawler.fetch_elearning_deadlines", return_value=[])
    @patch.object(run_hour, "_load_dotenv")
    def test_snapshot_schedule_failed_triggers_schedule_fallback_only(
        self, mock_dotenv: MagicMock, mock_deadlines: MagicMock, mock_sync: MagicMock, mock_snapshot: MagicMock, mock_pw_sched: MagicMock
    ) -> None:
        mock_snapshot.return_value = PortalSnapshot(
            semester=FetchResult(success=True, data="HK1/2026-2027"),
            schedule=FetchResult(success=False, data=None, error="Timeout"),
            exams=FetchResult(success=True, data=[{"subject_name": "Exam CSDL"}]),
        )

        run_hour.run_hourly_sync()

        mock_pw_sched.assert_called_once()
        mock_sync.assert_called_once_with(
            [{"subject_name": "Fallback Class"}],
            [{"subject_name": "Exam CSDL"}],
            student_id="TEST_STUDENT_001",
            deadlines=[],
        )

    @patch.dict(os.environ, {"STUDENT_ID": "TEST_STUDENT_001", "PASSWORD": "TEST_PASSWORD_NOT_A_SECRET"})
    @patch("crawler._fetch_exam_schedule_from_portal", return_value=[{"subject_name": "Fallback Exam"}])
    @patch("tdtu.fetch_portal_snapshot")
    @patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=("", True))
    @patch("crawler.fetch_elearning_deadlines", return_value=[])
    @patch.object(run_hour, "_load_dotenv")
    def test_snapshot_exam_failed_triggers_exam_fallback_only(
        self, mock_dotenv: MagicMock, mock_deadlines: MagicMock, mock_sync: MagicMock, mock_snapshot: MagicMock, mock_pw_exam: MagicMock
    ) -> None:
        mock_snapshot.return_value = PortalSnapshot(
            semester=FetchResult(success=True, data="HK1/2026-2027"),
            schedule=FetchResult(success=True, data=[{"subject_name": "CSDL"}]),
            exams=FetchResult(success=False, data=None, error="Timeout"),
        )

        run_hour.run_hourly_sync()

        mock_pw_exam.assert_called_once()
        mock_sync.assert_called_once_with(
            [{"subject_name": "CSDL"}],
            [{"subject_name": "Fallback Exam"}],
            student_id="TEST_STUDENT_001",
            deadlines=[],
        )

    @patch.dict(os.environ, {"STUDENT_ID": "TEST_STUDENT_001", "PASSWORD": "TEST_PASSWORD_NOT_A_SECRET"})
    @patch("crawler._fetch_schedule_playwright", side_effect=RuntimeError("Playwright failed"))
    @patch("crawler._fetch_exam_schedule_from_portal", side_effect=RuntimeError("Playwright failed"))
    @patch("tdtu.fetch_portal_snapshot")
    @patch.object(calendar_sync, "sync_crawled_data_to_google_calendar", return_value=("", True))
    @patch("crawler.fetch_elearning_deadlines", return_value=[])
    @patch.object(run_hour, "_load_dotenv")
    def test_snapshot_and_fallback_failed_passes_none_to_calendar(
        self, mock_dotenv: MagicMock, mock_deadlines: MagicMock, mock_sync: MagicMock, mock_snapshot: MagicMock, mock_pw_exam: MagicMock, mock_pw_sched: MagicMock
    ) -> None:
        mock_snapshot.return_value = PortalSnapshot(
            semester=FetchResult(success=False, data=None, error="HTTP fail"),
            schedule=FetchResult(success=False, data=None, error="HTTP fail"),
            exams=FetchResult(success=False, data=None, error="HTTP fail"),
        )

        run_hour.run_hourly_sync()

        # Both schedule and exams failed: passes None to calendar so events are NOT deleted! (Addresses Blocker 20)
        mock_sync.assert_called_once_with(
            None,
            None,
            student_id="TEST_STUDENT_001",
            deadlines=[],
        )


if __name__ == "__main__":
    unittest.main()
