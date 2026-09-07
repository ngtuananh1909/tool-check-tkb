"""Unit and fixture tests for Playwright eLearning crawler and mapper."""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from elearning.crawler import (
    DeadlineCrawlResult,
    PlaywrightElearningCrawler,
    compute_crawl_window,
)
from elearning.exceptions import (
    ElearningAuthError,
    ElearningCrawlError,
    ElearningError,
)
from elearning.mapper import (
    classify_event_kind,
    clean_activity_name,
    normalize_deadline_item,
)


class TestElearningWindowAndMapper(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Asia/Ho_Chi_Minh")

    def test_compute_crawl_window_standard(self) -> None:
        ref_dt = dt.datetime(2026, 9, 7, 8, 30, 15, 123456, tzinfo=self.tz)
        w_start, w_end, month_ts = compute_crawl_window(ref_dt, self.tz)

        # Microseconds zeroed
        self.assertEqual(w_start, dt.datetime(2026, 9, 7, 8, 30, 15, tzinfo=self.tz))
        # Window end is 1st day of month after next month (Nov 1) at 00:00:00
        self.assertEqual(w_end, dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz))
        self.assertEqual(len(month_ts), 2)
        # Month 0: Sept 1
        self.assertEqual(dt.datetime.fromtimestamp(month_ts[0], tz=self.tz), dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=self.tz))
        # Month 1: Oct 1
        self.assertEqual(dt.datetime.fromtimestamp(month_ts[1], tz=self.tz), dt.datetime(2026, 10, 1, 0, 0, 0, tzinfo=self.tz))

    def test_compute_crawl_window_year_rollover(self) -> None:
        ref_dt = dt.datetime(2026, 11, 20, 12, 0, 0, tzinfo=self.tz)
        w_start, w_end, month_ts = compute_crawl_window(ref_dt, self.tz)
        # Month 0: Nov 2026, Month 1: Dec 2026 -> window_end: Jan 1 2027
        self.assertEqual(w_end, dt.datetime(2027, 1, 1, 0, 0, 0, tzinfo=self.tz))

        ref_dt_dec = dt.datetime(2026, 12, 10, 0, 0, 0, tzinfo=self.tz)
        _, w_end_dec, _ = compute_crawl_window(ref_dt_dec, self.tz)
        # Month 0: Dec 2026, Month 1: Jan 2027 -> window_end: Feb 1 2027
        self.assertEqual(w_end_dec, dt.datetime(2027, 2, 1, 0, 0, 0, tzinfo=self.tz))

    def test_classify_event_kind(self) -> None:
        self.assertEqual(classify_event_kind("Bài tập 1 bắt đầu", "https://elearning.tdtu.edu.vn/mod/quiz/view.php?id=1"), "open")
        self.assertEqual(classify_event_kind("Lab 1 opens", "https://elearning.tdtu.edu.vn/mod/assign/view.php?id=1"), "open")
        self.assertEqual(classify_event_kind("Assignment 1 is due", "https://elearning.tdtu.edu.vn/mod/assign/view.php?id=1"), "due")
        self.assertEqual(classify_event_kind("Bài tập lớn đến hạn", "https://elearning.tdtu.edu.vn/mod/assign/view.php?id=2"), "due")
        self.assertEqual(classify_event_kind("Quiz 1 closes", "https://elearning.tdtu.edu.vn/mod/quiz/view.php?id=3"), "quiz_close")
        self.assertEqual(classify_event_kind("Trắc nghiệm kết thúc", "https://elearning.tdtu.edu.vn/mod/quiz/view.php?id=4"), "quiz_close")
        self.assertEqual(classify_event_kind("Task should be completed", "https://elearning.tdtu.edu.vn/mod/page/view.php?id=5"), "completion")
        self.assertEqual(classify_event_kind("Unknown activity", "https://elearning.tdtu.edu.vn/mod/forum/view.php?id=6"), "unknown")

    def test_clean_activity_name(self) -> None:
        self.assertEqual(clean_activity_name("Python 1 is due"), "Python 1")
        self.assertEqual(clean_activity_name("Báo cáo giữa kỳ đến hạn"), "Báo cáo giữa kỳ")
        self.assertEqual(clean_activity_name("Project should be completed"), "Project")

    def test_normalize_deadline_item(self) -> None:
        ts = int(dt.datetime(2026, 9, 15, 23, 59, 0, tzinfo=self.tz).timestamp())
        item = {
            "moodle_event_id": "100001",
            "course_id": "50001",
            "course_name": "CS101",
            "activity_name": "Assignment 1",
            "due_timestamp": ts,
            "activity_url": "https://elearning.tdtu.edu.vn/mod/assign/view.php?id=90001",
            "completion_status": "incomplete",
            "event_kind": "due",
        }
        res = normalize_deadline_item(item, self.tz)
        self.assertEqual(res["moodle_event_id"], "100001")
        self.assertEqual(res["source_signature"], "moodle_event:100001")
        self.assertEqual(res["due_date_dt"], dt.datetime(2026, 9, 15, 23, 59, 0, tzinfo=self.tz))
        self.assertEqual(res["due_date"], "2026-09-15T23:59:00+07:00")

    def test_normalize_missing_or_invalid_id_raises_crawl_error(self) -> None:
        with self.assertRaises(ElearningCrawlError):
            normalize_deadline_item({"moodle_event_id": ""}, self.tz)
        with self.assertRaises(ElearningCrawlError):
            normalize_deadline_item({"moodle_event_id": "not-a-number"}, self.tz)

    def test_normalize_missing_or_invalid_timestamp_raises_crawl_error(self) -> None:
        with self.assertRaises(ElearningCrawlError):
            normalize_deadline_item({"moodle_event_id": "100001", "due_timestamp": None}, self.tz)
        with self.assertRaises(ElearningCrawlError):
            normalize_deadline_item({"moodle_event_id": "100001", "due_timestamp": "bad"}, self.tz)


class TestPlaywrightCrawlerDOMFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.context = cls.browser.new_context()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.context.close()
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.crawler = PlaywrightElearningCrawler()
        self.page = self.context.new_page()
        self.tz = ZoneInfo("Asia/Ho_Chi_Minh")

    def tearDown(self) -> None:
        self.page.close()

    def test_missing_stable_event_id_fails_closed(self) -> None:
        # DOM event lacking numeric data-event-id
        html = """
        <div class="calendar-controls"><h2 class="current">September 2026</h2></div>
        <table class="calendarmonth">
            <tr>
                <td class="day" data-day-timestamp="1788973200">
                    <a data-event-id="" href="https://elearning.tdtu.edu.vn/mod/assign/view.php?id=90001">Assignment 1 is due</a>
                </td>
            </tr>
        </table>
        """
        self.page.set_content(html)
        w_start = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=self.tz)
        w_end = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz)

        with patch.object(self.page, "goto"):
            with self.assertRaises(ElearningCrawlError) as ctx:
                self.crawler._crawl_month_page(self.page, 1788195600, w_start, w_end)
        self.assertIn("missing required data-event-id", str(ctx.exception))

    def test_unknown_event_kind_in_window_fails_closed(self) -> None:
        # Event title does not match recognized pattern inside authority window
        html = """
        <div class="calendar-controls"><h2 class="current">September 2026</h2></div>
        <table class="calendarmonth">
            <tr>
                <td class="day" data-day-timestamp="1788973200">
                    <a data-event-id="100001" href="https://elearning.tdtu.edu.vn/mod/forum/view.php?id=90001">Random Event</a>
                </td>
            </tr>
        </table>
        """
        self.page.set_content(html)
        w_start = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=self.tz)
        w_end = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz)

        with patch.object(self.page, "goto"):
            with self.assertRaises(ElearningCrawlError) as ctx:
                self.crawler._crawl_month_page(self.page, 1788195600, w_start, w_end)
        self.assertIn("unrecognized event pattern", str(ctx.exception).lower())

    def test_unsupported_deadline_module_in_window_fails_closed(self) -> None:
        # Quiz close event inside authority window
        html = """
        <div class="calendar-controls"><h2 class="current">September 2026</h2></div>
        <table class="calendarmonth">
            <tr>
                <td class="day" data-day-timestamp="1788973200">
                    <a data-event-id="100002" href="https://elearning.tdtu.edu.vn/mod/quiz/view.php?id=90002">Quiz 1 closes</a>
                </td>
            </tr>
        </table>
        """
        self.page.set_content(html)
        w_start = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=self.tz)
        w_end = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz)

        with patch.object(self.page, "goto"):
            with self.assertRaises(ElearningCrawlError) as ctx:
                self.crawler._crawl_month_page(self.page, 1788195600, w_start, w_end)
        self.assertIn("unsupported deadline event kind", str(ctx.exception))

    def test_open_event_is_safely_excluded(self) -> None:
        # Quiz opening event is excluded and does not raise an error
        html = """
        <div class="calendar-controls"><h2 class="current">September 2026</h2></div>
        <table class="calendarmonth">
            <tr>
                <td class="day" data-day-timestamp="1788973200">
                    <a data-event-id="100003" href="https://elearning.tdtu.edu.vn/mod/quiz/view.php?id=90003">Quiz 1 bắt đầu</a>
                </td>
            </tr>
        </table>
        """
        self.page.set_content(html)
        w_start = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=self.tz)
        w_end = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz)

        with patch.object(self.page, "goto"):
            candidates = self.crawler._crawl_month_page(self.page, 1788195600, w_start, w_end)
        self.assertEqual(candidates, [])

    def test_missing_month_table_fails_closed(self) -> None:
        html = """<div>No table here</div>"""
        self.page.set_content(html)
        w_start = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=self.tz)
        w_end = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz)

        with patch.object(self.page, "goto"):
            with self.assertRaises(ElearningCrawlError):
                self.crawler._crawl_month_page(self.page, 1788195600, w_start, w_end)

    def test_empty_authoritative_month_returns_empty_list(self) -> None:
        # Table and heading present, 0 events
        html = """
        <div class="calendar-controls"><h2 class="current">October 2026</h2></div>
        <table class="calendarmonth">
            <tr><td class="day" data-day-timestamp="1790787600"></td></tr>
        </table>
        """
        self.page.set_content(html)
        w_start = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=self.tz)
        w_end = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz)

        with patch.object(self.page, "goto"):
            candidates = self.crawler._crawl_month_page(self.page, 1790787600, w_start, w_end)
        self.assertEqual(candidates, [])

    def test_activity_submission_detection_submitted_is_excluded(self) -> None:
        # Activity page with td.submissionstatussubmitted
        html = """
        <table class="generaltable submissionstatustable">
            <tr>
                <th>Submission status</th>
                <td class="submissionstatussubmitted cell c1 lastcol">Submitted for grading</td>
            </tr>
        </table>
        """
        self.page.set_content(html)
        candidate = {"activity_url": "https://elearning.tdtu.edu.vn/mod/assign/view.php?id=90001"}

        with patch.object(self.page, "goto"):
            is_actionable = self.crawler._check_assignment_actionable(self.page, candidate)
        self.assertFalse(is_actionable)

    def test_activity_submission_detection_unsubmitted_is_included(self) -> None:
        # Activity page without submitted class
        html = """
        <h1>Assignment 1</h1>
        <a href="https://elearning.tdtu.edu.vn/course/view.php?id=50001">Computer Science 101</a>
        <table class="generaltable submissionstatustable">
            <tr>
                <th>Submission status</th>
                <td class="submissionnotgraded cell c1 lastcol">No attempt</td>
            </tr>
        </table>
        """
        self.page.set_content(html)
        candidate = {"activity_url": "https://elearning.tdtu.edu.vn/mod/assign/view.php?id=90001"}

        with patch.object(self.page, "goto"):
            is_actionable = self.crawler._check_assignment_actionable(self.page, candidate)
        self.assertTrue(is_actionable)
        self.assertEqual(candidate.get("course_name"), "Computer Science 101")
        self.assertEqual(candidate.get("course_id"), "50001")
        self.assertEqual(candidate.get("activity_name"), "Assignment 1")

    def test_activity_missing_submission_table_fails_closed(self) -> None:
        html = """<div>Not an assignment page</div>"""
        self.page.set_content(html)
        candidate = {"activity_url": "https://elearning.tdtu.edu.vn/mod/assign/view.php?id=90001"}

        with patch.object(self.page, "goto"):
            with self.assertRaises(ElearningCrawlError):
                self.crawler._check_assignment_actionable(self.page, candidate)

    def test_window_filtering_excludes_past_events(self) -> None:
        # Event in the past (before window_start)
        past_ts = int(dt.datetime(2026, 9, 5, 0, 0, 0, tzinfo=self.tz).timestamp())
        html = f"""
        <div class="calendar-controls"><h2 class="current">September 2026</h2></div>
        <table class="calendarmonth">
            <tr>
                <td class="day" data-day-timestamp="{past_ts}">
                    <a data-event-id="100004" href="https://elearning.tdtu.edu.vn/mod/assign/view.php?id=90004">Past Lab is due</a>
                </td>
            </tr>
        </table>
        """
        self.page.set_content(html)
        w_start = dt.datetime(2026, 9, 7, 8, 0, 0, tzinfo=self.tz)
        w_end = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz)

        with patch.object(self.page, "goto"):
            candidates = self.crawler._crawl_month_page(self.page, 1788195600, w_start, w_end)
        self.assertEqual(candidates, [])

    def test_window_filtering_boundary_at_window_end_is_excluded(self) -> None:
        # Event exactly at window_end (e.g. 2026-11-01 00:00:00) is excluded (half-open [start, end))
        end_ts = int(dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz).timestamp())
        html = f"""
        <div class="calendar-controls"><h2 class="current">October 2026</h2></div>
        <table class="calendarmonth">
            <tr>
                <td class="day" data-day-timestamp="{end_ts}">
                    <a data-event-id="100005" href="https://elearning.tdtu.edu.vn/mod/assign/view.php?id=90005">Boundary Assignment is due</a>
                </td>
            </tr>
        </table>
        """
        self.page.set_content(html)
        w_start = dt.datetime(2026, 9, 7, 8, 0, 0, tzinfo=self.tz)
        w_end = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz)

        with patch.object(self.page, "goto"):
            candidates = self.crawler._crawl_month_page(self.page, 1790787600, w_start, w_end)
        self.assertEqual(candidates, [])

    def test_window_filtering_boundary_before_window_end_is_included(self) -> None:
        # Event 1 second before window_end is included
        before_end_ts = int(dt.datetime(2026, 10, 31, 23, 59, 59, tzinfo=self.tz).timestamp())
        html = f"""
        <div class="calendar-controls"><h2 class="current">October 2026</h2></div>
        <table class="calendarmonth">
            <tr>
                <td class="day" data-day-timestamp="{before_end_ts}">
                    <a data-event-id="100006" href="https://elearning.tdtu.edu.vn/mod/assign/view.php?id=90006">Valid Assignment is due</a>
                </td>
            </tr>
        </table>
        """
        self.page.set_content(html)
        w_start = dt.datetime(2026, 9, 7, 8, 0, 0, tzinfo=self.tz)
        w_end = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz)

        with patch.object(self.page, "goto"):
            candidates = self.crawler._crawl_month_page(self.page, 1790787600, w_start, w_end)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["moodle_event_id"], "100006")

    def test_session_expired_redirect_raises_auth_error(self) -> None:
        w_start = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=self.tz)
        w_end = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=self.tz)

        def mock_goto(*_args, **_kwargs):
            # Simulate navigation redirecting to login
            self.page.set_content("<div>Login required</div>")
            # Override url property
            return None

        with (
            patch.object(self.page, "goto", side_effect=mock_goto),
            patch.object(type(self.page), "url", new_callable=lambda: "https://elearning.tdtu.edu.vn/login/index.php"),
        ):
            with self.assertRaises(ElearningAuthError):
                self.crawler._crawl_month_page(self.page, 1788195600, w_start, w_end)


if __name__ == "__main__":
    unittest.main()
