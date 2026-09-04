"""
Regression and backward compatibility tests for crawler.py facade and tdtu package.
"""

import unittest
from unittest.mock import MagicMock, patch

from tdtu.snapshot import FetchResult, PortalSnapshot, fetch_portal_snapshot


class TestCrawlerCompatibility(unittest.TestCase):

    @patch("tdtu.snapshot.TDTUClient")
    @patch("tdtu.snapshot.get_current_semester_http")
    @patch("tdtu.snapshot.fetch_schedule_http")
    @patch("tdtu.snapshot.fetch_exam_schedule_http")
    def test_portal_snapshot_orchestration(
        self,
        mock_fetch_exams: MagicMock,
        mock_fetch_sched: MagicMock,
        mock_get_sem: MagicMock,
        mock_client_cls: MagicMock,
    ) -> None:
        mock_client_instance = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client_instance

        mock_get_sem.return_value = "HK1/2026-2027"
        mock_fetch_sched.return_value = [{"subject_name": "Math"}]
        mock_fetch_exams.return_value = [{"subject_name": "Math Exam"}]

        snapshot = fetch_portal_snapshot("52500028", "pass123")

        self.assertTrue(snapshot.schedule.success)
        self.assertTrue(snapshot.exams.success)
        self.assertEqual(snapshot.semester.data, "HK1/2026-2027")
        self.assertEqual(len(snapshot.schedule.data), 1)
        self.assertEqual(len(snapshot.exams.data), 1)

        # Confirm client entered context once and fetched all 3 components
        mock_client_cls.assert_called_once_with(student_id="52500028", password="pass123")
        mock_get_sem.assert_called_once()
        mock_fetch_sched.assert_called_once()
        mock_fetch_exams.assert_called_once()


if __name__ == "__main__":
    unittest.main()
