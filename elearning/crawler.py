"""Playwright DOM-based crawler for TDTU Moodle eLearning deadlines."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeadlineCrawlResult:
    """Packaged result of an authoritative eLearning crawl."""

    items: list[dict]
    window_start: dt.datetime
    window_end: dt.datetime


def compute_crawl_window(
    ref_dt: dt.datetime | None = None,
    tz: ZoneInfo | None = None,
) -> tuple[dt.datetime, dt.datetime, list[int]]:
    """Compute the half-open authority window [window_start, window_end) and month timestamps.

    Authority horizon: Current Month + Next Month.
    - window_start: whole-second current time in tz.
    - window_end: 00:00:00 on the 1st day of the month after the next month.
    - month_timestamps: list of Unix timestamps for the 1st day of Month 0 and Month 1.
    """
    app_tz = tz or ZoneInfo("Asia/Ho_Chi_Minh")
    now = (ref_dt or dt.datetime.now(app_tz)).astimezone(app_tz).replace(microsecond=0)
    window_start = now

    y0, m0 = now.year, now.month
    dt0 = dt.datetime(y0, m0, 1, 0, 0, 0, tzinfo=app_tz)

    m1 = m0 + 1
    y1 = y0
    if m1 > 12:
        m1 = 1
        y1 += 1
    dt1 = dt.datetime(y1, m1, 1, 0, 0, 0, tzinfo=app_tz)

    m2 = m1 + 1
    y2 = y1
    if m2 > 12:
        m2 = 1
        y2 += 1
    window_end = dt.datetime(y2, m2, 1, 0, 0, 0, tzinfo=app_tz)

    return window_start, window_end, [int(dt0.timestamp()), int(dt1.timestamp())]


class PlaywrightElearningCrawler:
    """Pure Playwright DOM crawler inspecting rendered Moodle Calendar and activity pages."""

    def __init__(
        self,
        base_url: str = "https://elearning.tdtu.edu.vn",
        timeout_ms: int = 30000,
        headless: bool = True,
        timezone: str = "Asia/Ho_Chi_Minh",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = timeout_ms
        self.headless = headless
        self.timezone = timezone
        self.tz = ZoneInfo(timezone)

    def crawl_deadlines(
        self,
        username: str,
        password: str,
        ref_dt: dt.datetime | None = None,
    ) -> DeadlineCrawlResult:
        """Authenticate and perform authoritative DOM crawl for incomplete assignments.

        Fails closed on any navigation timeout, session expiry, missing DOM, or
        unsupported candidate deadline within the authority window.
        """
        if not username or not password:
            raise ElearningAuthError("Missing username or password for eLearning crawl")

        window_start, window_end, month_timestamps = compute_crawl_window(ref_dt, self.tz)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context()
                page = context.new_page()
                try:
                    self._login(page, username, password)

                    raw_candidates: list[dict] = []
                    for month_ts in month_timestamps:
                        month_items = self._crawl_month_page(page, month_ts, window_start, window_end)
                        raw_candidates.extend(month_items)

                    # Deduplicate by moodle_event_id
                    unique_candidates: dict[str, dict] = {}
                    for item in raw_candidates:
                        eid = item["moodle_event_id"]
                        if eid not in unique_candidates:
                            unique_candidates[eid] = item

                    # Inspect actionable status for each candidate assignment
                    surviving_items: list[dict] = []
                    for candidate in unique_candidates.values():
                        is_actionable = self._check_assignment_actionable(page, candidate)
                        if is_actionable:
                            normalized = normalize_deadline_item(candidate, self.tz)
                            if window_start <= normalized["due_date_dt"] < window_end:
                                surviving_items.append(normalized)

                    surviving_items.sort(key=lambda x: x["due_date_dt"])
                    return DeadlineCrawlResult(
                        items=surviving_items,
                        window_start=window_start,
                        window_end=window_end,
                    )
                finally:
                    context.close()
                    browser.close()
        except ElearningError:
            raise
        except PlaywrightError as exc:
            raise ElearningCrawlError(f"Playwright automation failure: {exc}") from exc
        except Exception as exc:
            raise ElearningCrawlError(f"Unexpected error during eLearning crawl: {exc}") from exc

    def _login(self, page: Page, username: str, password: str) -> None:
        """Perform form login on Moodle login page."""
        login_url = f"{self.base_url}/login/index.php"
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            raise ElearningAuthError(f"Failed to load login page: {exc}") from exc

        # Find username input
        user_loc = page.locator('form#login input[name="username"]:not([type="hidden"])')
        if user_loc.count() == 0:
            user_loc = page.locator('input#username, input[name="username"]:not([type="hidden"])')
        if user_loc.count() == 0:
            raise ElearningAuthError("Login form username input not found")

        # Find password input
        pwd_loc = page.locator('form#login input[name="password"]:not([type="hidden"])')
        if pwd_loc.count() == 0:
            pwd_loc = page.locator('input#password:not([type="hidden"]), input[name="password"]:not([type="hidden"])')
        if pwd_loc.count() == 0:
            raise ElearningAuthError("Login form password input not found")

        # Find submit button
        btn_loc = page.locator('form#login #loginbtn, form#login button[type="submit"], #loginbtn')
        if btn_loc.count() == 0:
            raise ElearningAuthError("Login submit button not found")

        try:
            user_loc.first.fill(username)
            pwd_loc.first.fill(password)
            btn_loc.first.click()
            page.wait_for_url(lambda u: "login/index.php" not in u.lower(), timeout=self.timeout_ms)
        except Exception as exc:
            raise ElearningAuthError(f"Login submission or redirection failed: {exc}") from exc

        # Verify not rejected back to login
        if "login/index.php" in page.url.lower():
            raise ElearningAuthError("Authentication rejected: redirected back to login page")

    def _crawl_month_page(
        self,
        page: Page,
        month_ts: int,
        window_start: dt.datetime,
        window_end: dt.datetime,
    ) -> list[dict]:
        """Navigate to direct month view URL and extract candidate event cards."""
        month_url = f"{self.base_url}/calendar/view.php?view=month&time={month_ts}"
        try:
            page.goto(month_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            raise ElearningCrawlError(f"Failed to navigate to month page (time={month_ts}): {exc}") from exc

        if "login/index.php" in page.url.lower():
            raise ElearningAuthError("Session expired; redirected to login while loading month page")

        # Verify month table presence
        if page.locator("table.calendarmonth").count() == 0:
            raise ElearningCrawlError(f"Expected calendar month table (table.calendarmonth) not found for time={month_ts}")

        # Verify month heading presence
        if page.locator(".calendar-controls h2, h2.current, .current").count() == 0:
            raise ElearningCrawlError(f"Month heading not found for time={month_ts}")

        raw_cards = page.evaluate("""() => {
            const elements = Array.from(document.querySelectorAll('table.calendarmonth [data-event-id]'));
            return elements.map((el, idx) => {
                const dayCell = el.closest('td.day');
                return {
                    idx: idx,
                    eventId: el.getAttribute('data-event-id'),
                    courseId: el.getAttribute('data-course-id') || '',
                    title: el.getAttribute('title') || el.innerText.trim(),
                    href: el.getAttribute('href') || '',
                    dayTimestamp: dayCell ? dayCell.getAttribute('data-day-timestamp') : null
                };
            });
        }""")

        candidates: list[dict] = []
        for card in raw_cards:
            event_id = str(card.get("eventId") or "").strip()
            if not event_id or not event_id.isdigit():
                raise ElearningCrawlError(f"Calendar event card #{card.get('idx', 0)} missing required data-event-id")

            day_ts_raw = card.get("dayTimestamp")
            if not day_ts_raw or not str(day_ts_raw).isdigit():
                raise ElearningCrawlError(f"Calendar event #{event_id} missing valid day timestamp")

            due_ts = int(day_ts_raw)
            due_dt = dt.datetime.fromtimestamp(due_ts, tz=self.tz)

            title = card.get("title", "")
            href = card.get("href", "")
            kind = classify_event_kind(title, href)

            if kind == "open":
                # Known non-deadline opening event; exclude safely
                continue

            in_window = (window_start <= due_dt < window_end)

            if kind == "due":
                if in_window:
                    candidates.append({
                        "moodle_event_id": event_id,
                        "course_id": str(card.get("courseId") or ""),
                        "activity_name": clean_activity_name(title),
                        "activity_url": href,
                        "due_timestamp": due_ts,
                        "event_kind": "due",
                    })
            elif kind in ("quiz_close", "completion"):
                if in_window:
                    # Unsupported deadline kind inside authority window -> FAIL CLOSED
                    raise ElearningCrawlError(
                        f"Encountered unsupported deadline event kind '{kind}' for event #{event_id} in authority window"
                    )
            else:
                if in_window:
                    # Unrecognized event kind inside authority window -> FAIL CLOSED
                    raise ElearningCrawlError(
                        f"Encountered unrecognized event pattern for event #{event_id} in authority window"
                    )

        return candidates

    def _check_assignment_actionable(self, page: Page, candidate: dict) -> bool:
        """Inspect Assignment activity page to determine submission state (Policy B).

        Returns True if unsubmitted (actionable), False if already submitted.
        Fails closed if the submission table is missing or page times out.
        """
        activity_url = candidate["activity_url"]
        try:
            page.goto(activity_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            raise ElearningCrawlError(f"Failed to open assignment page {activity_url}: {exc}") from exc

        if "login/index.php" in page.url.lower():
            raise ElearningAuthError("Session expired; redirected to login while inspecting assignment")

        info = page.evaluate("""() => {
            const table = document.querySelector('table.submissionstatustable, table.generaltable');
            if (!table) {
                return { hasTable: false };
            }
            const hasSubmitted = !!document.querySelector('td.submissionstatussubmitted');
            const courseLink = document.querySelector('a[href*="/course/view.php?id="]');
            const heading = document.querySelector('h2, h1');
            return {
                hasTable: true,
                hasSubmitted: hasSubmitted,
                courseHref: courseLink ? courseLink.href : '',
                courseName: courseLink ? courseLink.innerText.trim() : '',
                activityTitle: heading ? heading.innerText.trim() : ''
            };
        }""")

        if not info.get("hasTable"):
            raise ElearningCrawlError(f"Submission status table not found on assignment page {activity_url}")

        if info.get("hasSubmitted"):
            return False

        # Enrich candidate metadata if present
        if info.get("courseName"):
            candidate["course_name"] = info["courseName"]
        if info.get("courseHref") and not candidate.get("course_id"):
            m = re.search(r"id=(\d+)", info["courseHref"])
            if m:
                candidate["course_id"] = m.group(1)
        if info.get("activityTitle"):
            candidate["activity_name"] = info["activityTitle"]

        return True
