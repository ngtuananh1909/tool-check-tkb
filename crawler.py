"""
crawler.py
----------
Playwright-based scraper that logs into the TDTU student portal and
extracts the user's class schedule from the HTML timetable.

Required environment variables:
    STUDENT_ID  – student login username
    PASSWORD    – student login password
"""

import logging
import os

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

load_dotenv()

logger = logging.getLogger(__name__)

PORTAL_URL = "https://thongtin.tdtu.edu.vn/"


def _login(page, student_id: str, password: str) -> None:
    """Navigate to the portal and submit the login form."""
    page.goto(PORTAL_URL, wait_until="networkidle")

    # Fill in the login form fields
    page.fill("input[name='txtDangNhap']", student_id)
    page.fill("input[name='txtMatKhau']", password)

    # Click the login button and wait for navigation to complete
    with page.expect_navigation(wait_until="networkidle"):
        page.click("input[type='submit'], button[type='submit']")

    logger.info("Login submitted for student: %s", student_id)


def _navigate_to_schedule(page) -> None:
    """
    Navigate to the timetable / schedule section of the portal.
    The selector targets the side-menu link whose text contains 'thời khoá biểu'
    (Vietnamese for 'timetable').
    """
    try:
        # Try to find and click the schedule menu item
        page.click("text=Thời khoá biểu", timeout=10_000)
    except PlaywrightTimeout:
        # Fallback: try partial, case-insensitive selector
        page.click("a:has-text('khoá biểu')", timeout=10_000)

    page.wait_for_load_state("networkidle")
    logger.info("Navigated to schedule page")


def _parse_schedule_table(page, student_id: str) -> list[dict]:
    """
    Parse the schedule HTML table and return a list of schedule records.

    Expected table columns (by index):
        0 – STT (row number, ignored)
        1 – Tên môn học (Subject name)
        2 – Phòng (Room)
        3 – Thứ (Day of week, e.g. 'Thứ 2' = Monday)
        4 – Tiết bắt đầu (Start period)
        5 – Tiết kết thúc (End period)
    """
    # Wait for the schedule table to appear
    page.wait_for_selector("table", timeout=15_000)

    rows = page.query_selector_all("table tbody tr")
    schedule: list[dict] = []

    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) < 6:
            # Skip header rows or rows with insufficient columns
            continue

        texts = [c.inner_text().strip() for c in cells]

        subject_name = texts[1]
        room = texts[2]
        day_of_week = texts[3]
        start_period = texts[4]
        end_period = texts[5]

        # Skip empty or obviously invalid rows
        if not subject_name or not room:
            continue

        schedule.append(
            {
                "student_id": student_id,
                "subject_name": subject_name,
                "room": room,
                "day_of_week": day_of_week,
                "start_period": start_period,
                "end_period": end_period,
            }
        )

    logger.info("Parsed %d schedule entries", len(schedule))
    return schedule


def fetch_schedule() -> list[dict]:
    """
    Main entry point for the crawler module.

    Logs into the TDTU portal using credentials from environment variables,
    navigates to the schedule page, scrapes the timetable, and returns
    the schedule as a list of dictionaries.

    Returns:
        List of schedule dicts with keys:
            student_id, subject_name, room, day_of_week,
            start_period, end_period
    """
    student_id = os.environ["STUDENT_ID"]
    password = os.environ["PASSWORD"]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        try:
            _login(page, student_id, password)
            _navigate_to_schedule(page)
            schedule = _parse_schedule_table(page, student_id)
        finally:
            browser.close()

    return schedule
