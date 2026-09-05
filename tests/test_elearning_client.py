"""Unit tests for elearning package (ElearningClient, mapper, exceptions)."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import requests

from elearning.client import DeadlineCrawlResult, ElearningClient
from elearning.exceptions import (
    ElearningApiError,
    ElearningAuthError,
    ElearningPaginationError,
    ElearningResponseError,
)
from elearning.mapper import map_moodle_event

APP_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


class ResponseMock:
    def __init__(self, text: str = "", url: str = "", status_code: int = 200, json_data=None):
        self.text = text
        self.url = url
        self.status_code = status_code
        self._json_data = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def json(self):
        if self._json_data is not None:
            return self._json_data
        raise ValueError("No JSON data")


class TestElearningClient(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock(spec=requests.Session)
        self.session.cookies = {}
        self.session.headers = {}
        self.client = ElearningClient("student123", "password123", session=self.session)

    # -------------------------------------------------------------------
    # Client / Auth Tests
    # -------------------------------------------------------------------
    def test_pure_http_login_success(self):
        # 1. GET login page
        html_login = '<html><input name="logintoken" value="token123"/></html>'
        # 2. POST login page
        # 3. GET calendar page
        html_cal = '<html><script>var M = {"sesskey":"sess123"};</script></html>'

        self.session.get.side_effect = [
            ResponseMock(text=html_login, status_code=200),
            ResponseMock(text=html_cal, status_code=200),
        ]
        self.session.post.return_value = ResponseMock(
            text="dashboard", url="https://elearning.tdtu.edu.vn/course/index.php", status_code=200
        )
        self.session.cookies = {"MoodleSession": "cookie_val"}

        self.client.login()
        self.assertEqual(self.client.sesskey, "sess123")

    def test_login_token_missing(self):
        html_login = "<html><body>No token</body></html>"
        self.session.get.return_value = ResponseMock(text=html_login, status_code=200)

        with self.assertRaises(ElearningAuthError):
            self.client.login()

    def test_credentials_rejected(self):
        html_login = '<html><input name="logintoken" value="token123"/></html>'
        self.session.get.return_value = ResponseMock(text=html_login, status_code=200)
        self.session.post.return_value = ResponseMock(
            text="login page", url="https://elearning.tdtu.edu.vn/login/index.php", status_code=200
        )
        self.session.cookies = {"MoodleSession": "cookie_val"}

        with self.assertRaises(ElearningAuthError):
            self.client.login()

    def test_session_cookie_missing(self):
        html_login = '<html><input name="logintoken" value="token123"/></html>'
        self.session.get.return_value = ResponseMock(text=html_login, status_code=200)
        self.session.post.return_value = ResponseMock(
            text="dashboard", url="https://elearning.tdtu.edu.vn/course/index.php", status_code=200
        )
        self.session.cookies = {}  # missing MoodleSession

        with self.assertRaises(ElearningAuthError):
            self.client.login()

    def test_sesskey_missing(self):
        html_login = '<html><input name="logintoken" value="token123"/></html>'
        html_cal = "<html><body>No sesskey</body></html>"
        self.session.get.side_effect = [
            ResponseMock(text=html_login, status_code=200),
            ResponseMock(text=html_cal, status_code=200),
        ]
        self.session.post.return_value = ResponseMock(
            text="dashboard", url="https://elearning.tdtu.edu.vn/course/index.php", status_code=200
        )
        self.session.cookies = {"MoodleSession": "cookie_val"}

        with self.assertRaises(ElearningAuthError):
            self.client.login()

    # -------------------------------------------------------------------
    # Security & Secret Leak Protection Tests
    # -------------------------------------------------------------------
    def test_ajax_http_error_does_not_expose_sesskey(self):
        secret_sesskey = "VERY_SECRET_SESSKEY_123456"
        self.client.sesskey = secret_sesskey

        fake_response = ResponseMock(
            status_code=500,
            url=f"https://elearning.tdtu.edu.vn/lib/ajax/service.php?sesskey={secret_sesskey}",
        )
        http_err = requests.HTTPError(
            f"500 Server Error for url: https://elearning.tdtu.edu.vn/lib/ajax/service.php?sesskey={secret_sesskey}"
        )
        http_err.response = fake_response
        self.session.post.side_effect = http_err

        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)

        with self.assertRaises(ElearningApiError) as cm:
            self.client.fetch_action_events(start, end)

        err_msg = str(cm.exception)
        self.assertNotIn(secret_sesskey, err_msg)
        self.assertIsNone(cm.exception.__cause__)

    def test_ajax_json_decode_error_does_not_expose_sesskey(self):
        secret_sesskey = "VERY_SECRET_SESSKEY_123456"
        self.client.sesskey = secret_sesskey
        self.session.post.return_value = ResponseMock(text="<html>HTML Error page</html>", status_code=200)

        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)

        with self.assertRaises(ElearningResponseError) as cm:
            self.client.fetch_action_events(start, end)

        err_msg = str(cm.exception)
        self.assertNotIn(secret_sesskey, err_msg)
        self.assertIsNone(cm.exception.__cause__)

    def test_login_http_error_does_not_expose_credentials(self):
        html_login = '<html><input name="logintoken" value="token123"/></html>'
        self.session.get.return_value = ResponseMock(text=html_login, status_code=200)
        self.session.post.side_effect = requests.HTTPError("Login failed")

        with self.assertRaises(ElearningAuthError) as cm:
            self.client.login()

        self.assertIsNone(cm.exception.__cause__)

    # -------------------------------------------------------------------
    # Mapper & Actionable Filtering Tests
    # -------------------------------------------------------------------
    def test_actionable_true_mapped(self):
        raw = {
            "id": "101",
            "name": "Practice Lab 1",
            "timesort": 1788739200,
            "url": "https://elearning.tdtu.edu.vn/mod/assign/view.php?id=1",
            "eventtype": "due",
            "course": {"id": 501, "fullname": "Software Architecture"},
            "action": {"actionable": True},
        }
        res = map_moodle_event(raw, APP_TZ)
        self.assertIsNotNone(res)
        self.assertEqual(res["moodle_event_id"], "101")
        self.assertEqual(res["course_name"], "Software Architecture")
        self.assertEqual(res["activity_name"], "Practice Lab 1")
        self.assertEqual(res["source_signature"], "moodle_event:101")
        self.assertEqual(res["completion_status"], "incomplete")

    def test_actionable_false_ignored(self):
        raw = {
            "id": "102",
            "name": "Completed Quiz",
            "timesort": 1788739200,
            "action": {"actionable": False},
        }
        res = map_moodle_event(raw, APP_TZ)
        self.assertIsNone(res)

    def test_action_missing_raises_response_error(self):
        raw = {
            "id": "104",
            "name": "Announcement 2",
            "timesort": 1788739200,
        }
        with self.assertRaises(ElearningResponseError):
            map_moodle_event(raw, APP_TZ)

    def test_action_not_dict_raises_response_error(self):
        raw = {
            "id": "105",
            "name": "Malformed Action",
            "timesort": 1788739200,
            "action": "not a dict",
        }
        with self.assertRaises(ElearningResponseError):
            map_moodle_event(raw, APP_TZ)

    def test_actionable_missing_raises_response_error(self):
        raw = {
            "id": "103",
            "name": "Announcement",
            "timesort": 1788739200,
            "action": {},  # missing 'actionable'
        }
        with self.assertRaises(ElearningResponseError):
            map_moodle_event(raw, APP_TZ)

    def test_window_start_end_client_boundary_enforcement(self):
        self.client.sesskey = "sess123"
        start = datetime(2026, 9, 5, 10, 0, 0, tzinfo=APP_TZ)
        end = datetime(2026, 9, 10, 10, 0, 0, tzinfo=APP_TZ)

        # Event at start -> included
        # Event in middle -> included
        # Event at end -> excluded
        ts_start = int(start.timestamp())
        ts_mid = int((start + timedelta(days=2)).timestamp())
        ts_end = int(end.timestamp())

        events = [
            {"id": "1", "timesort": ts_start, "action": {"actionable": True}},
            {"id": "2", "timesort": ts_mid, "action": {"actionable": True}},
            {"id": "3", "timesort": ts_end, "action": {"actionable": True}},
        ]
        payload = [{"error": False, "data": {"events": events, "firstid": 1, "lastid": 3}}]
        self.session.post.return_value = ResponseMock(status_code=200, json_data=payload)

        with patch("elearning.client.datetime") as mock_dt:
            mock_dt.now.return_value = start
            mock_dt.fromisoformat = datetime.fromisoformat
            res = self.client.fetch_deadline_result(days_ahead=5)

        self.assertEqual([e["moodle_event_id"] for e in res.items], ["1", "2"])

    def test_invalid_window_inputs_raise_value_error(self):
        self.client.sesskey = "sess123"
        naive_dt = datetime(2026, 9, 5, 10, 0, 0)
        aware_dt = datetime(2026, 9, 5, 10, 0, 0, tzinfo=APP_TZ)

        # Naive start
        with self.assertRaises(ValueError):
            self.client.fetch_action_events(naive_dt, aware_dt)

        # Naive end
        with self.assertRaises(ValueError):
            self.client.fetch_action_events(aware_dt, naive_dt)

        # window_end <= window_start
        with self.assertRaises(ValueError):
            self.client.fetch_action_events(aware_dt, aware_dt)

        # page_size <= 0
        with self.assertRaises(ValueError):
            self.client.fetch_action_events(aware_dt, aware_dt + timedelta(days=1), page_size=0)

    def test_successful_empty_events(self):
        self.client.sesskey = "sess123"
        payload = [{"error": False, "data": {"events": [], "firstid": None, "lastid": None}}]
        self.session.post.return_value = ResponseMock(status_code=200, json_data=payload)

        res = self.client.fetch_deadline_result(days_ahead=30)
        self.assertIsInstance(res, DeadlineCrawlResult)
        self.assertEqual(res.items, [])

    def test_moodle_api_error_response(self):
        self.client.sesskey = "sess123"
        payload = [{"error": True, "exception": "Access denied"}]
        self.session.post.return_value = ResponseMock(status_code=200, json_data=payload)

        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)
        with self.assertRaises(ElearningApiError):
            self.client.fetch_action_events(start, end)

    def test_malformed_json_schema(self):
        self.client.sesskey = "sess123"
        # events missing 'timesort'
        payload = [{"error": False, "data": {"events": [{"id": "101", "action": {"actionable": True}}]}}]
        self.session.post.return_value = ResponseMock(status_code=200, json_data=payload)

        with self.assertRaises(ElearningResponseError):
            self.client.fetch_deadline_result(days_ahead=30)

    # -------------------------------------------------------------------
    # Pagination Tests
    # -------------------------------------------------------------------
    def test_single_partial_page(self):
        self.client.sesskey = "sess123"
        events = [{"id": "1", "timesort": 1000, "action": {"actionable": True}}]
        payload = [{"error": False, "data": {"events": events, "firstid": 1, "lastid": 1}}]
        self.session.post.return_value = ResponseMock(status_code=200, json_data=payload)

        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)
        res = self.client.fetch_action_events(start, end, page_size=50)
        self.assertEqual(len(res), 1)

    def test_exact_full_page_then_empty(self):
        self.client.sesskey = "sess123"
        page1_events = [{"id": str(i), "timesort": 1000 + i, "action": {"actionable": True}} for i in range(1, 3)]
        payload1 = [{"error": False, "data": {"events": page1_events, "firstid": 1, "lastid": 2}}]
        payload2 = [{"error": False, "data": {"events": [], "firstid": None, "lastid": None}}]

        self.session.post.side_effect = [
            ResponseMock(status_code=200, json_data=payload1),
            ResponseMock(status_code=200, json_data=payload2),
        ]

        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)
        res = self.client.fetch_action_events(start, end, page_size=2)
        self.assertEqual(len(res), 2)
        self.assertEqual([e["id"] for e in res], ["1", "2"])

    def test_aftereventid_uses_previous_lastid(self):
        self.client.sesskey = "sess123"
        page1_events = [{"id": "1", "timesort": 1000, "action": {"actionable": True}}, {"id": "2", "timesort": 1001, "action": {"actionable": True}}]
        payload1 = [{"error": False, "data": {"events": page1_events, "firstid": 1, "lastid": 2}}]
        payload2 = [{"error": False, "data": {"events": [], "firstid": None, "lastid": None}}]

        self.session.post.side_effect = [
            ResponseMock(status_code=200, json_data=payload1),
            ResponseMock(status_code=200, json_data=payload2),
        ]

        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)
        self.client.fetch_action_events(start, end, page_size=2)

        # Check call arguments of second request
        second_call_json = self.session.post.call_args_list[1].kwargs["json"]
        args = second_call_json[0]["args"]
        self.assertEqual(args.get("aftereventid"), 2)

    def test_full_page_missing_lastid(self):
        self.client.sesskey = "sess123"
        page1_events = [{"id": "1", "timesort": 1000, "action": {"actionable": True}}, {"id": "2", "timesort": 1001, "action": {"actionable": True}}]
        payload1 = [{"error": False, "data": {"events": page1_events, "firstid": 1, "lastid": None}}]  # missing lastid

        self.session.post.return_value = ResponseMock(status_code=200, json_data=payload1)
        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)

        with self.assertRaises(ElearningPaginationError):
            self.client.fetch_action_events(start, end, page_size=2)

    def test_cursor_fails_to_advance(self):
        self.client.sesskey = "sess123"
        page1_events = [{"id": "1", "timesort": 1000, "action": {"actionable": True}}, {"id": "2", "timesort": 1001, "action": {"actionable": True}}]
        page2_events = [{"id": "3", "timesort": 1002, "action": {"actionable": True}}, {"id": "4", "timesort": 1003, "action": {"actionable": True}}]
        payload1 = [{"error": False, "data": {"events": page1_events, "firstid": 1, "lastid": 2}}]
        payload2 = [{"error": False, "data": {"events": page2_events, "firstid": 3, "lastid": 2}}]  # lastid stays 2

        self.session.post.side_effect = [
            ResponseMock(status_code=200, json_data=payload1),
            ResponseMock(status_code=200, json_data=payload2),
        ]

        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)
        with self.assertRaises(ElearningPaginationError):
            self.client.fetch_action_events(start, end, page_size=2)

    def test_page_2_http_failure(self):
        self.client.sesskey = "sess123"
        page1_events = [{"id": "1", "timesort": 1000, "action": {"actionable": True}}, {"id": "2", "timesort": 1001, "action": {"actionable": True}}]
        payload1 = [{"error": False, "data": {"events": page1_events, "firstid": 1, "lastid": 2}}]

        self.session.post.side_effect = [
            ResponseMock(status_code=200, json_data=payload1),
            ResponseMock(status_code=500),
        ]

        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)
        with self.assertRaises(ElearningApiError):
            self.client.fetch_action_events(start, end, page_size=2)

    def test_page_2_api_error(self):
        self.client.sesskey = "sess123"
        page1_events = [{"id": "1", "timesort": 1000, "action": {"actionable": True}}, {"id": "2", "timesort": 1001, "action": {"actionable": True}}]
        payload1 = [{"error": False, "data": {"events": page1_events, "firstid": 1, "lastid": 2}}]
        payload2 = [{"error": True, "exception": "Database error"}]

        self.session.post.side_effect = [
            ResponseMock(status_code=200, json_data=payload1),
            ResponseMock(status_code=200, json_data=payload2),
        ]

        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)
        with self.assertRaises(ElearningApiError):
            self.client.fetch_action_events(start, end, page_size=2)

    def test_duplicate_event_ids_across_pages(self):
        self.client.sesskey = "sess123"
        page1_events = [{"id": "1", "timesort": 1000, "action": {"actionable": True}}, {"id": "2", "timesort": 1001, "action": {"actionable": True}}]
        page2_events = [{"id": "2", "timesort": 1002, "action": {"actionable": True}}]  # ID "2" duplicated!
        payload1 = [{"error": False, "data": {"events": page1_events, "firstid": 1, "lastid": 2}}]
        payload2 = [{"error": False, "data": {"events": page2_events, "firstid": 2, "lastid": 3}}]

        self.session.post.side_effect = [
            ResponseMock(status_code=200, json_data=payload1),
            ResponseMock(status_code=200, json_data=payload2),
        ]

        start = datetime.now(APP_TZ)
        end = start + timedelta(days=30)
        with self.assertRaises(ElearningPaginationError):
            self.client.fetch_action_events(start, end, page_size=2)

    def test_partial_success_never_escapes(self):
        self.client.sesskey = "sess123"
        page1_events = [{"id": "1", "timesort": 1000, "action": {"actionable": True}}, {"id": "2", "timesort": 1001, "action": {"actionable": True}}]
        payload1 = [{"error": False, "data": {"events": page1_events, "firstid": 1, "lastid": 2}}]

        self.session.post.side_effect = [
            ResponseMock(status_code=200, json_data=payload1),
            Exception("Connection reset"),
        ]

        with self.assertRaises(ElearningApiError):
            self.client.fetch_deadline_result(days_ahead=30, page_size=2)

    # -------------------------------------------------------------------
    # Mapping & Identity Tests
    # -------------------------------------------------------------------
    def test_moodle_event_id_to_source_signature(self):
        raw = {"id": "392689", "timesort": 1788739200, "action": {"actionable": True}}
        mapped = map_moodle_event(raw, APP_TZ)
        self.assertEqual(mapped["source_signature"], "moodle_event:392689")

    def test_same_activity_url_different_ids(self):
        raw1 = {"id": "392689", "name": "Event 1", "timesort": 1788739200, "url": "http://example.com/mod", "action": {"actionable": True}}
        raw2 = {"id": "392690", "name": "Event 2", "timesort": 1788739300, "url": "http://example.com/mod", "action": {"actionable": True}}

        m1 = map_moodle_event(raw1, APP_TZ)
        m2 = map_moodle_event(raw2, APP_TZ)

        self.assertNotEqual(m1["source_signature"], m2["source_signature"])
        self.assertEqual(m1["activity_url"], m2["activity_url"])

    def test_event_kind_normalization(self):
        raw = {"id": "100", "timesort": 1000, "eventtype": "close", "action": {"actionable": True}}
        m = map_moodle_event(raw, APP_TZ)
        self.assertEqual(m["event_kind"], "close")

    def test_timezone_aware_timestamps(self):
        ts = 1788739200  # 2026-09-07T00:00:00Z
        raw = {"id": "100", "timesort": ts, "action": {"actionable": True}}
        m = map_moodle_event(raw, APP_TZ)

        due_date_str = m["due_date"]
        parsed_dt = datetime.fromisoformat(due_date_str)
        self.assertIsNotNone(parsed_dt.tzinfo)
        self.assertEqual(parsed_dt.astimezone(timezone.utc).timestamp(), ts)


if __name__ == "__main__":
    unittest.main()
