import unittest
from unittest.mock import MagicMock, patch

from crawler import fetch_exam_schedule, fetch_schedule
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

        snapshot = fetch_portal_snapshot("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET")

        self.assertTrue(snapshot.schedule.success)
        self.assertTrue(snapshot.exams.success)
        self.assertEqual(snapshot.semester.data, "HK1/2026-2027")
        self.assertEqual(len(snapshot.schedule.data), 1)
        self.assertEqual(len(snapshot.exams.data), 1)

        # Confirm client entered context once and fetched all 3 components
        mock_client_cls.assert_called_once_with(student_id="TEST_STUDENT_001", password="TEST_PASSWORD_NOT_A_SECRET")
        mock_get_sem.assert_called_once()
        mock_fetch_sched.assert_called_once()
        mock_fetch_exams.assert_called_once()

    @patch("crawler._fetch_schedule_playwright")
    @patch("crawler.fetch_schedule_http")
    @patch("crawler.TDTUClient")
    def test_fetch_schedule_http_success_no_playwright(self, mock_client, mock_http, mock_playwright):
        mock_http.return_value = [{"subject_name": "Physics"}]
        res = fetch_schedule("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET")
        self.assertEqual(res, [{"subject_name": "Physics"}])
        mock_playwright.assert_not_called()

    @patch("crawler._fetch_schedule_playwright")
    @patch("crawler.fetch_schedule_http")
    @patch("crawler.TDTUClient")
    def test_fetch_schedule_http_failure_triggers_playwright(self, mock_client, mock_http, mock_playwright):
        mock_http.side_effect = Exception("HTTP error")
        mock_playwright.return_value = [{"subject_name": "Physics"}]
        res = fetch_schedule("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET")
        self.assertEqual(res, [{"subject_name": "Physics"}])
        mock_playwright.assert_called_once()

    @patch("crawler._fetch_schedule_playwright")
    @patch("crawler.fetch_schedule_http")
    @patch("crawler.TDTUClient")
    def test_fetch_schedule_http_empty_success_no_playwright(self, mock_client, mock_http, mock_playwright):
        mock_http.return_value = []
        res = fetch_schedule("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET")
        self.assertEqual(res, [])
        mock_playwright.assert_not_called()

    @patch("crawler._fetch_exam_schedule_from_portal")
    @patch("crawler.fetch_exam_schedule_http")
    @patch("crawler.TDTUClient")
    def test_fetch_exam_schedule_http_success_no_fallback(self, mock_client, mock_http, mock_playwright):
        mock_http.return_value = [{"subject_name": "Math Exam"}]
        res = fetch_exam_schedule("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET")
        self.assertEqual(res, [{"subject_name": "Math Exam"}])
        mock_playwright.assert_not_called()

    @patch("crawler._fetch_exam_schedule_from_elearning")
    @patch("crawler._fetch_exam_schedule_from_stdportal_announcements")
    @patch("crawler._fetch_exam_schedule_from_portal")
    @patch("crawler.fetch_exam_schedule_http")
    @patch("crawler.TDTUClient")
    def test_fetch_exam_schedule_http_empty_returns_empty_no_fallback(
        self, mock_client, mock_http, mock_portal, mock_ann, mock_elearning
    ):
        mock_http.return_value = []
        res = fetch_exam_schedule("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET")
        self.assertEqual(res, [])
        mock_portal.assert_not_called()
        mock_ann.assert_not_called()
        mock_elearning.assert_not_called()

    @patch("crawler._fetch_exam_schedule_from_portal")
    @patch("crawler.fetch_exam_schedule_http")
    @patch("crawler.TDTUClient")
    def test_fetch_exam_schedule_http_failure_triggers_portal_playwright(self, mock_client, mock_http, mock_portal):
        mock_http.side_effect = Exception("HTTP fail")
        mock_portal.return_value = [{"subject_name": "Math Exam"}]
        res = fetch_exam_schedule("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET")
        self.assertEqual(res, [{"subject_name": "Math Exam"}])
        mock_portal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
