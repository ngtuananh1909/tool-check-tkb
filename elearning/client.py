"""Pure HTTP Moodle API client for fetching TDTU eLearning calendar deadlines."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
import re
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from elearning.exceptions import (
    ElearningApiError,
    ElearningAuthError,
    ElearningError,
    ElearningPaginationError,
    ElearningResponseError,
)
from elearning.mapper import map_moodle_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeadlineCrawlResult:
    """Encapsulates mapped deadline items and the authoritative date window."""

    items: list[dict]
    window_start: datetime
    window_end: datetime


class ElearningClient:
    """Pure HTTP client for TDTU Moodle eLearning deadline collection."""

    BASE_URL = "https://elearning.tdtu.edu.vn"
    LOGIN_URL = "https://elearning.tdtu.edu.vn/login/index.php"
    CALENDAR_URL = "https://elearning.tdtu.edu.vn/calendar/view.php"
    SERVICE_URL = "https://elearning.tdtu.edu.vn/lib/ajax/service.php"

    def __init__(
        self,
        student_id: str,
        password: str,
        session: requests.Session | None = None,
    ):
        if not student_id or not password:
            raise ValueError("student_id and password are required")
        self.student_id = student_id
        self.password = password
        self.session = session or requests.Session()
        self._external_session = session is not None
        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        self.sesskey: str | None = None

    def __enter__(self) -> "ElearningClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close HTTP session if managed internally."""
        if not self._external_session and self.session:
            self.session.close()

    def login(self) -> None:
        """Authenticate with eLearning using pure HTTP requests and extract sesskey."""
        logger.info("Logging in to TDTU eLearning via pure HTTP...")

        # 1. GET login page to obtain hidden logintoken
        try:
            resp = self.session.get(self.LOGIN_URL, timeout=15)
            resp.raise_for_status()
        except Exception as exc:
            status = exc.response.status_code if getattr(exc, "response", None) is not None else None
            raise ElearningAuthError(
                "Failed to load login page" + (f" with HTTP status {status}" if status else "")
            ) from None

        logintoken = self._extract_logintoken(resp.text)
        if not logintoken:
            raise ElearningAuthError("Login page missing required 'logintoken'")

        # 2. POST login credentials
        post_data = {
            "username": self.student_id,
            "password": self.password,
            "logintoken": logintoken,
        }
        try:
            post_resp = self.session.post(
                self.LOGIN_URL, data=post_data, timeout=15, allow_redirects=True
            )
            post_resp.raise_for_status()
        except Exception as exc:
            status = exc.response.status_code if getattr(exc, "response", None) is not None else None
            raise ElearningAuthError(
                "Login POST request failed" + (f" with HTTP status {status}" if status else "")
            ) from None

        if "login" in post_resp.url.lower() or "MoodleSession" not in self.session.cookies:
            raise ElearningAuthError("eLearning HTTP login failed: invalid credentials or session rejected")

        logger.info("eLearning HTTP login successful.")

        # 3. GET calendar page to extract sesskey
        try:
            cal_resp = self.session.get(self.CALENDAR_URL, timeout=15)
            cal_resp.raise_for_status()
        except Exception as exc:
            status = exc.response.status_code if getattr(exc, "response", None) is not None else None
            raise ElearningAuthError(
                "Failed to load calendar page" + (f" with HTTP status {status}" if status else "")
            ) from None

        sesskey = self._extract_sesskey(cal_resp.text)
        if not sesskey:
            raise ElearningAuthError("Failed to extract 'sesskey' from calendar page")

        self.sesskey = sesskey
        logger.debug("Successfully extracted sesskey.")

    def fetch_action_events(
        self,
        window_start: datetime,
        window_end: datetime,
        page_size: int = 50,
    ) -> list[dict]:
        """Fetch raw action events from Moodle API with pagination safeguards."""
        if window_start.tzinfo is None or window_end.tzinfo is None:
            raise ValueError("window_start and window_end must be timezone-aware datetimes")
        if window_end <= window_start:
            raise ValueError("window_end must be strictly greater than window_start")
        if page_size <= 0:
            raise ValueError("page_size must be a positive integer")

        if not self.sesskey:
            raise ElearningAuthError("Client is not authenticated. Call login() first.")

        url = f"{self.SERVICE_URL}?sesskey={self.sesskey}"
        cursor: Any = None
        all_events: list[dict] = []
        seen_cursors: set[str] = set()
        seen_event_ids: set[str] = set()

        while True:
            args: dict[str, Any] = {
                "timesortfrom": int(window_start.timestamp()),
                "timesortto": int(window_end.timestamp()),
                "limitnum": page_size,
                "limittononsuspendedevents": True,
            }
            if cursor is not None:
                args["aftereventid"] = cursor

            payload = [
                {
                    "index": 0,
                    "methodname": "core_calendar_get_action_events_by_timesort",
                    "args": args,
                }
            ]

            try:
                resp = self.session.post(url, json=payload, timeout=20)
                resp.raise_for_status()
            except Exception as exc:
                status = exc.response.status_code if getattr(exc, "response", None) is not None else None
                raise ElearningApiError(
                    "Moodle AJAX request failed" + (f" with HTTP status {status}" if status else "")
                ) from None

            try:
                res_json = resp.json()
            except ValueError:
                raise ElearningResponseError("Moodle AJAX response was not valid JSON") from None

            if not isinstance(res_json, list) or not res_json:
                raise ElearningResponseError("Invalid API envelope: expected non-empty array")

            page_res = res_json[0]
            if not isinstance(page_res, dict):
                raise ElearningResponseError("Invalid API payload: array element is not a dict")

            if page_res.get("error"):
                exception_msg = str(page_res.get("exception") or "Unknown error").strip()
                raise ElearningApiError(f"Moodle API returned error: {exception_msg}")

            data = page_res.get("data")
            data_dict = data if isinstance(data, dict) else {}
            events = data_dict.get("events")
            if not isinstance(events, list):
                raise ElearningResponseError("Invalid API payload: 'data.events' is not a list")

            if not events:
                break

            # Invariant: Detect duplicate event IDs across pages
            for ev in events:
                if not isinstance(ev, dict):
                    raise ElearningResponseError("Event item is not a dictionary")
                ev_id = str(ev.get("id") or "").strip()
                if not ev_id:
                    raise ElearningResponseError("Moodle event missing required 'id'")
                if ev_id in seen_event_ids:
                    raise ElearningPaginationError(
                        f"Duplicate Moodle event ID detected across pages: {ev_id}"
                    )
                seen_event_ids.add(ev_id)

            all_events.extend(events)

            if len(events) < page_size:
                break

            next_cursor = data_dict.get("lastid")
            if next_cursor is None:
                raise ElearningPaginationError("Full Moodle event page did not provide 'lastid'")

            next_cursor_str = str(next_cursor).strip()
            if not next_cursor_str:
                raise ElearningPaginationError("Full Moodle event page provided empty 'lastid'")

            if next_cursor_str in seen_cursors or (cursor is not None and next_cursor_str == str(cursor)):
                raise ElearningPaginationError(
                    f"Pagination cursor did not advance: {next_cursor_str}"
                )

            seen_cursors.add(next_cursor_str)
            cursor = next_cursor

        return all_events

    def fetch_deadline_result(
        self,
        days_ahead: int = 120,
        page_size: int = 50,
    ) -> DeadlineCrawlResult:
        """Fetch and map all actionable deadlines within the authoritative window."""
        app_tz_name = os.environ.get("APP_TIMEZONE", "Asia/Ho_Chi_Minh").strip()
        try:
            app_tz = ZoneInfo(app_tz_name)
        except Exception:
            app_tz = ZoneInfo("Asia/Ho_Chi_Minh")

        window_start = datetime.now(app_tz)
        window_end = window_start + timedelta(days=max(1, days_ahead))

        raw_events = self.fetch_action_events(window_start, window_end, page_size=page_size)

        items: list[dict] = []
        for raw in raw_events:
            mapped = map_moodle_event(raw, app_tz)
            if mapped is not None:
                due_dt = datetime.fromisoformat(mapped["due_date"])
                if window_start <= due_dt < window_end:
                    items.append(mapped)

        logger.info(
            "eLearning API crawl completed: %d deadline(s) fetched (window: %s to %s).",
            len(items),
            window_start.isoformat(),
            window_end.isoformat(),
        )
        return DeadlineCrawlResult(
            items=items,
            window_start=window_start,
            window_end=window_end,
        )

    @staticmethod
    def _extract_logintoken(html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        inp = soup.find("input", {"name": "logintoken"})
        if inp and inp.get("value"):
            return str(inp["value"]).strip()
        match = re.search(r'name=["\']logintoken["\']\s+value=["\']([^"\']+)["\']', html)
        return match.group(1).strip() if match else None

    @staticmethod
    def _extract_sesskey(html: str) -> str | None:
        match = re.search(r'"sesskey":"([^"]+)"', html)
        return match.group(1).strip() if match else None
