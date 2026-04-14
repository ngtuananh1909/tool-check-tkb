"""
crawler.py – Playwright-based scraper for TDTU student schedule portal.

Logs in to https://thongtin.tdtu.edu.vn/ using credentials from environment
variables, navigates to the schedule section, and parses the timetable HTML
table.

Required environment variables:
    STUDENT_ID  – TDTU student ID used as the login username
    PASSWORD    – Portal account password
"""

import logging
import os
import re

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PORTAL_URL = "https://thongtin.tdtu.edu.vn/"

# Selector hints – adjust if the portal markup changes
SELECTOR_USERNAME = "input[name='username'], input[id='username'], input[placeholder*='MSSV'], input[type='text']"
SELECTOR_PASSWORD = "input[name='password'], input[id='password'], input[type='password']"
SELECTOR_SUBMIT = "button[type='submit'], input[type='submit']"

# The schedule table typically lives inside an element with this text / URL
SCHEDULE_MENU_TEXT = re.compile(r"thời khóa biểu|TKB|lịch học", re.IGNORECASE)

# Map Vietnamese day abbreviations / names to English weekday names
DAY_MAP: dict[str, str] = {
    "2": "Monday",
    "thứ 2": "Monday",
    "thứ hai": "Monday",
    "3": "Tuesday",
    "thứ 3": "Tuesday",
    "thứ ba": "Tuesday",
    "4": "Wednesday",
    "thứ 4": "Wednesday",
    "thứ tư": "Wednesday",
    "5": "Thursday",
    "thứ 5": "Thursday",
    "thứ năm": "Thursday",
    "6": "Friday",
    "thứ 6": "Friday",
    "thứ sáu": "Friday",
    "7": "Saturday",
    "thứ 7": "Saturday",
    "thứ bảy": "Saturday",
    "cn": "Sunday",
    "chủ nhật": "Sunday",
}


def _normalize_day(raw: str) -> str:
    """Return a normalized English weekday name from a Vietnamese raw string."""
    key = raw.strip().lower()
    return DAY_MAP.get(key, raw.strip())


def fetch_schedule(student_id: str | None = None, password: str | None = None) -> list[dict]:
    """
    Log in to the TDTU portal and return the student's timetable as a list of
    dictionaries.

    Each dictionary has the following keys:
        student_id   (str)
        subject_name (str)
        room         (str)
        day_of_week  (str)  – English weekday name, e.g. "Monday"
        start_period (int)
        end_period   (int)

    Parameters
    ----------
    student_id : str, optional
        Overrides the STUDENT_ID environment variable.
    password : str, optional
        Overrides the PASSWORD environment variable.

    Raises
    ------
    ValueError
        If credentials are not provided either as arguments or env vars.
    RuntimeError
        If the login fails or the schedule table cannot be located.
    """
    sid = student_id or os.environ.get("STUDENT_ID")
    pwd = password or os.environ.get("PASSWORD")

    if not sid or not pwd:
        raise ValueError(
            "Credentials missing. Set STUDENT_ID and PASSWORD environment variables."
        )

    schedule: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            # ----------------------------------------------------------------
            # Step 1 – Load the portal login page
            # ----------------------------------------------------------------
            logger.info("Navigating to %s", PORTAL_URL)
            page.goto(PORTAL_URL, wait_until="networkidle", timeout=60_000)

            # ----------------------------------------------------------------
            # Step 2 – Fill in credentials and submit
            # ----------------------------------------------------------------
            logger.info("Filling in login credentials for student %s", sid)
            page.fill(SELECTOR_USERNAME, sid)
            page.fill(SELECTOR_PASSWORD, pwd)
            page.click(SELECTOR_SUBMIT)

            # Wait until navigation is complete after login
            page.wait_for_load_state("networkidle", timeout=60_000)

            # Basic check – if we're still on the login page, fail loudly
            if page.url == PORTAL_URL or "login" in page.url.lower():
                # Try to grab an error message from the page for better diagnostics
                error_text = page.text_content("body") or ""
                raise RuntimeError(
                    f"Login appears to have failed. Current URL: {page.url}. "
                    f"Page excerpt: {error_text[:200]}"
                )

            logger.info("Login successful. Current URL: %s", page.url)

            # ----------------------------------------------------------------
            # Step 3 – Navigate to the schedule section
            # ----------------------------------------------------------------
            # Try to find and click a navigation link that matches common labels
            schedule_link = page.locator(f"a:has-text('{SCHEDULE_MENU_TEXT.pattern}')")
            if schedule_link.count() == 0:
                # Fallback: look for any link whose href contains 'tkb' or 'schedule'
                schedule_link = page.locator("a[href*='tkb'], a[href*='schedule'], a[href*='lichhoc']")

            if schedule_link.count() > 0:
                logger.info("Clicking schedule navigation link")
                schedule_link.first.click()
                page.wait_for_load_state("networkidle", timeout=60_000)
            else:
                logger.warning(
                    "Could not find a schedule navigation link; attempting to parse "
                    "current page."
                )

            # ----------------------------------------------------------------
            # Step 4 – Parse the schedule table
            # ----------------------------------------------------------------
            logger.info("Parsing schedule table on %s", page.url)
            schedule = _parse_schedule_table(page, sid)

            if not schedule:
                logger.warning("No schedule entries found in the table.")
            else:
                logger.info("Parsed %d schedule entries.", len(schedule))

        except PlaywrightTimeoutError as exc:
            raise RuntimeError(f"Playwright timed out: {exc}") from exc
        finally:
            context.close()
            browser.close()

    return schedule


def _parse_schedule_table(page, student_id: str) -> list[dict]:
    """
    Locate the first <table> that looks like a schedule table and extract rows.

    The portal typically uses a table with columns similar to:
        STT | Môn học | Nhóm | Phòng | Thứ | Tiết bắt đầu | Tiết kết thúc | …

    Because the exact column order may vary, we detect column positions by
    inspecting the header row.
    """
    # Grab all tables on the page
    tables = page.locator("table").all()
    if not tables:
        raise RuntimeError("No <table> elements found on the schedule page.")

    for table in tables:
        headers_raw = [
            th.inner_text().strip().lower()
            for th in table.locator("thead tr th, tr:first-child th, tr:first-child td").all()
        ]

        if not headers_raw:
            continue

        # Identify column indices by fuzzy header matching
        col = _detect_columns(headers_raw)
        if col.get("subject") is None:
            continue  # This table is probably not the schedule table

        logger.debug("Schedule table headers: %s", headers_raw)
        logger.debug("Detected column mapping: %s", col)

        rows = table.locator("tbody tr, tr:not(:first-child)").all()
        entries: list[dict] = []

        for row in rows:
            cells = [td.inner_text().strip() for td in row.locator("td").all()]
            if len(cells) <= max(v for v in col.values() if v is not None):
                continue  # Skip rows that don't have enough cells

            subject = cells[col["subject"]] if col.get("subject") is not None else ""
            room = cells[col["room"]] if col.get("room") is not None else ""
            day_raw = cells[col["day"]] if col.get("day") is not None else ""
            start_raw = cells[col["start"]] if col.get("start") is not None else "0"
            end_raw = cells[col["end"]] if col.get("end") is not None else "0"

            if not subject:
                continue

            try:
                start_period = int(re.search(r"\d+", start_raw).group()) if re.search(r"\d+", start_raw) else 0
                end_period = int(re.search(r"\d+", end_raw).group()) if re.search(r"\d+", end_raw) else 0
            except (AttributeError, ValueError):
                start_period = 0
                end_period = 0

            entries.append(
                {
                    "student_id": student_id,
                    "subject_name": subject,
                    "room": room,
                    "day_of_week": _normalize_day(day_raw),
                    "start_period": start_period,
                    "end_period": end_period,
                }
            )

        if entries:
            return entries

    raise RuntimeError(
        "Could not locate a parseable schedule table on the page. "
        "The portal markup may have changed."
    )


def _detect_columns(headers: list[str]) -> dict[str, int | None]:
    """
    Return a mapping of logical column name -> index based on header strings.

    Fuzzy keyword matching is used so that minor wording changes don't break
    the parser.
    """
    mapping: dict[str, int | None] = {
        "subject": None,
        "room": None,
        "day": None,
        "start": None,
        "end": None,
    }

    keywords: dict[str, list[str]] = {
        "subject": ["môn", "subject", "tên môn", "học phần"],
        "room": ["phòng", "room", "phòng học"],
        "day": ["thứ", "day", "ngày"],
        "start": ["bắt đầu", "tiết đầu", "start", "tiết bt"],
        "end": ["kết thúc", "tiết cuối", "end", "tiết kt"],
    }

    for idx, header in enumerate(headers):
        for col_name, kws in keywords.items():
            if mapping[col_name] is None and any(kw in header for kw in kws):
                mapping[col_name] = idx

    return mapping
