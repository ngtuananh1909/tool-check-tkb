"""
crawler.py – Playwright-based scraper for TDTU student schedule portal.

Logs in to https://old-stdportal.tdtu.edu.vn/ using credentials from environment
variables, navigates to the schedule section, and parses the timetable HTML
table.

Required environment variables:
    STUDENT_ID  – TDTU student ID used as the login username
    PASSWORD    – Portal account password

Optional environment variables:
    TARGET_SEMESTER – Force specific semester label (e.g. HK2/2025-2026)
    CRAWLER_WEEKS_AHEAD – Number of future weeks to crawl beyond current week
"""

import logging
import os
import re
import datetime
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from time_utils import local_today

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PORTAL_URL = "https://old-stdportal.tdtu.edu.vn/Login/"
SCHEDULE_URL_BASE = "https://lichhoc-lichthi.tdtu.edu.vn/tkb2.aspx"
EXAM_URL_BASE = "https://lichhoc-lichthi.tdtu.edu.vn/xemlichthi.aspx"
ELEARNING_LOGIN_URL = "https://elearning.tdtu.edu.vn/login/index.php"
ELEARNING_MY_URL = "https://elearning.tdtu.edu.vn/my/"

# eLearning login pages may include hidden username fields (e.g. guest value).
# Restrict selectors to interactive inputs only.
ELEARNING_SELECTOR_USERNAME = (
    "form#login input[name='username']:not([type='hidden']), "
    "form#login input[id='username']:not([type='hidden']), "
    "input[id='username']:not([type='hidden'])"
)
ELEARNING_SELECTOR_PASSWORD = (
    "form#login input[name='password']:not([type='hidden']), "
    "form#login input[id='password']:not([type='hidden']), "
    "input[id='password']:not([type='hidden']), "
    "input[type='password']"
)
ELEARNING_SELECTOR_SUBMIT = (
    "form#login #loginbtn, "
    "form#login button[type='submit'], "
    "form#login input[type='submit'], "
    "#loginbtn"
)

# Timeout (ms) for locating the submit button before falling back to Enter
SUBMIT_BUTTON_TIMEOUT_MS = 5_000

# Selector hints – adjust if the portal markup changes
# Include both lowercase (HTML) and PascalCase (ASP.NET MVC model binding) variants.
SELECTOR_USERNAME = (
    "input[name='UserName'], input[id='UserName'], "
    "input[name='username'], input[id='username'], "
    "input[placeholder*='MSSV'], input[placeholder*='mssv'], "
    "input[type='text']"
)
SELECTOR_PASSWORD = (
    "input[name='Password'], input[id='Password'], "
    "input[name='password'], input[id='password'], "
    "input[type='password']"
)
# Ordered from most specific to a broad fallback (`button` with no type filter).
# Note: avoid combining CSS attribute selectors with Playwright :has-text() in the
# same compound rule – keep them as separate comma-separated alternatives instead.
SELECTOR_SUBMIT = (
    "button[type='submit'], input[type='submit'], "
    "button.btn-login, "
    "input[type='button'][value*='Login'], input[type='button'][value*='login'], "
    "input[type='button'][value*='Đăng'], "
    "button:has-text('Đăng nhập'), button:has-text('Đăng Nhập'), "
    "button:has-text('Login'), button:has-text('Sign in'), "
    "button"  # broad fallback – catches <button> without explicit type
)

# The schedule table typically lives inside an element with this text / URL
SCHEDULE_MENU_TEXT = re.compile(r"thời khóa biểu|TKB|lịch học", re.IGNORECASE)

# Filter keywords on the schedule page
SEMESTER_TEXT = re.compile(r"học kỳ|hoc\s*ky|semester|hk\s*\d", re.IGNORECASE)
WEEK_VIEW_TEXT = re.compile(r"theo\s*tuần|xem\s*lịch\s*theo\s*tuần|weekly|week", re.IGNORECASE)

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


def fetch_schedule(
    student_id: str | None = None,
    password: str | None = None,
    weeks_ahead: int | None = None,
) -> list[dict]:
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
    extra_weeks = _resolve_weeks_ahead(weeks_ahead)
    total_weeks = 1 + extra_weeks

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
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
            # Wait for the username input to be ready before filling.
            page.wait_for_selector(SELECTOR_USERNAME, state="visible", timeout=30_000)
            logger.info("Filling in login credentials for student %s", sid)
            page.fill(SELECTOR_USERNAME, sid)
            page.fill(SELECTOR_PASSWORD, pwd)

            # Record the login page URL *after* any redirect so we can
            # detect a failed login (i.e., we were returned to this URL).
            login_page_url = page.url

            # ── Submission strategy 1: click the submit button ──────────────
            submit_clicked = False
            try:
                page.locator(SELECTOR_SUBMIT).first.click(timeout=SUBMIT_BUTTON_TIMEOUT_MS)
                submit_clicked = True
                logger.info("Submit button clicked.")
            except Exception:
                logger.warning(
                    "Submit button not found via selector %r; trying fallback strategies.",
                    SELECTOR_SUBMIT,
                )

            if not submit_clicked:
                # ── Submission strategy 2: press Enter on the password field ─
                try:
                    page.locator(SELECTOR_PASSWORD).press("Enter")
                    logger.info("Pressed Enter on password field.")
                except Exception:
                    logger.warning("Enter key on password field failed; trying JS form submit.")
                    # ── Submission strategy 3: JavaScript form.submit() ───────
                    try:
                        page.evaluate(
                            "const f = document.querySelector('form'); if (f) f.submit();"
                        )
                        logger.info("Triggered JS form.submit().")
                    except Exception as js_exc:
                        logger.warning("JS form.submit() also failed: %s", js_exc)

            # Wait for the page to navigate away from the login URL.
            # Using wait_for_url is more reliable than wait_for_load_state
            # when some submission methods don't trigger a full page reload.
            try:
                page.wait_for_url(
                    lambda url: "login" not in str(url).lower(),
                    timeout=30_000,
                )
            except PlaywrightTimeoutError:
                # URL did not change within the timeout window.
                # Fall through to the explicit failure check below.
                logger.warning(
                    "wait_for_url timed out – login may have failed or navigation was delayed."
                )

            # Ensure the page is fully loaded after the URL change.
            page.wait_for_load_state("networkidle", timeout=30_000)

            # Basic check – if we're still on the login page, fail loudly.
            if page.url == login_page_url or "login" in page.url.lower():
                # Try to grab an error message from the page for better diagnostics
                error_text = page.text_content("body") or ""
                raise RuntimeError(
                    f"Login appears to have failed. Current URL: {page.url}. "
                    f"Page excerpt: {error_text[:500]}"
                )

            logger.info("Login successful. Current URL: %s", page.url)

            # ----------------------------------------------------------------
            # Step 3 – Navigate to the schedule section
            # ----------------------------------------------------------------
            schedule_url = _build_schedule_url(page.url)

            # Priority 1: anchor tags whose href already points at the schedule
            schedule_link = page.locator(
                "a[href*='tkb'], a[href*='schedule'], a[href*='lichhoc'], a[href*='lichhoc-lichthi']"
            )

            # Priority 2: visible anchor tags whose *text* matches schedule keywords.
            # Restrict to <a> so we never accidentally resolve to a hidden news/post
            # element (e.g. a <b> inside an announcement) that shares the same words.
            if schedule_link.count() == 0:
                schedule_link = page.locator("a").filter(has_text=SCHEDULE_MENU_TEXT)

            clicked_schedule_link = False
            if schedule_link.count() > 0:
                for i in range(schedule_link.count()):
                    candidate = schedule_link.nth(i)
                    try:
                        if not candidate.is_visible():
                            continue

                        logger.info("Clicking visible schedule navigation link (candidate %d)", i + 1)
                        candidate.click(timeout=10_000)
                        page.wait_for_load_state("networkidle", timeout=60_000)
                        clicked_schedule_link = True
                        break
                    except Exception as exc:
                        logger.debug("Skipping schedule link candidate %d: %s", i + 1, exc)

            if not clicked_schedule_link:
                # Fallback: navigate directly to the schedule URL
                logger.info("No visible schedule link found; navigating directly to %s", schedule_url)
                page.goto(schedule_url, wait_until="networkidle", timeout=60_000)

            # ----------------------------------------------------------------
            # Step 4 – Parse the schedule table
            # ----------------------------------------------------------------
            logger.info("Configuring schedule filters (semester + weekly view) when available")
            _configure_schedule_filters(page)

            logger.info(
                "Parsing schedule table on %s (current week + %d future week(s)).",
                page.url,
                extra_weeks,
            )
            all_rows: list[dict] = []
            for index in range(total_weeks):
                logger.info("Parsing week %d/%d.", index + 1, total_weeks)
                week_rows = _parse_schedule_table(page, sid)
                if week_rows:
                    all_rows.extend(week_rows)
                    logger.info("Week %d yielded %d row(s).", index + 1, len(week_rows))
                else:
                    logger.warning("Week %d yielded no rows.", index + 1)

                if index >= total_weeks - 1:
                    break

                if not _goto_next_week(page):
                    logger.warning(
                        "Could not navigate to next week after week %d. Keeping partial multi-week data.",
                        index + 1,
                    )
                    break

            schedule = _deduplicate_schedule_rows(all_rows)

            if not schedule:
                logger.warning("No schedule entries found in the crawled week range.")
            else:
                logger.info("Parsed %d unique schedule entries across crawled weeks.", len(schedule))
                logger.info("=== FINAL DEDUPLICATED SCHEDULE ===")
                for i, entry in enumerate(schedule):
                    logger.info(
                        "  [%d] student=%s subject=%r room=%r day=%r date=%r period=%s-%s status=%s",
                        i + 1,
                        entry.get("student_id"),
                        entry.get("subject_name"),
                        entry.get("room"),
                        entry.get("day_of_week"),
                        entry.get("session_date"),
                        entry.get("start_period"),
                        entry.get("end_period"),
                        entry.get("status"),
                    )
                logger.info("=== END FINAL SCHEDULE ===")

        except PlaywrightTimeoutError as exc:
            raise RuntimeError(f"Playwright timed out: {exc}") from exc
        finally:
            context.close()
            browser.close()

    return schedule


def _resolve_weeks_ahead(weeks_ahead: int | None) -> int:
    """Resolve number of extra weeks to crawl, using env var when not provided."""
    raw = weeks_ahead
    if raw is None:
        env_value = (os.environ.get("CRAWLER_WEEKS_AHEAD") or "0").strip()
        try:
            raw = int(env_value)
        except ValueError:
            logger.warning("Invalid CRAWLER_WEEKS_AHEAD=%r; using 0.", env_value)
            raw = 0

    if raw is None:
        return 0
    if raw < 0:
        return 0
    return min(raw, 12)


def _deduplicate_schedule_rows(rows: list[dict]) -> list[dict]:
    """Deduplicate rows across all crawled weeks while preserving first-seen order."""
    deduped: list[dict] = []
    seen: set[tuple[str, str, str, str, int, int]] = set()

    for row in rows:
        signature = (
            str(row.get("subject_name") or "").strip().lower(),
            str(row.get("room") or "").strip().lower(),
            str(row.get("day_of_week") or "").strip().lower(),
            str(row.get("session_date") or "").strip(),
            int(row.get("start_period", 0) or 0),
            int(row.get("end_period", 0) or 0),
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(row)

    return deduped


def fetch_exam_schedule(
    student_id: str | None = None,
    password: str | None = None,
    weeks_ahead: int | None = None,
) -> list[dict]:
    """Fetch exam schedule with portal-first strategy and optional eLearning fallback."""
    sid = student_id or os.environ.get("STUDENT_ID")
    pwd = password or os.environ.get("PASSWORD")
    if not sid or not pwd:
        raise ValueError("Credentials missing. Set STUDENT_ID and PASSWORD environment variables.")

    exams: list[dict] = []
    try:
        exams = _fetch_exam_schedule_from_portal(sid, pwd, weeks_ahead=weeks_ahead)
        if exams:
            logger.info("Fetched %d exam row(s) from TDTU portal.", len(exams))
            return exams
    except Exception as exc:
        logger.warning("Portal exam crawl failed: %s", exc)

    try:
        exams = _fetch_exam_schedule_from_stdportal_announcements(sid, pwd)
        if exams:
            logger.info("Fetched %d exam row(s) from stdportal announcements fallback.", len(exams))
            return exams
    except Exception as exc:
        logger.warning("Stdportal announcements exam fallback failed: %s", exc)

    enable_fallback = str(os.environ.get("EXAM_SOURCE_FALLBACK_ELEARNING", "true")).strip().lower()
    if enable_fallback not in {"1", "true", "yes", "on"}:
        return []

    try:
        exams = _fetch_exam_schedule_from_elearning(sid, pwd)
        if exams:
            logger.info("Fetched %d exam row(s) from eLearning fallback.", len(exams))
    except Exception as exc:
        logger.warning("eLearning exam fallback failed: %s", exc)
        exams = []

    return exams


def fetch_elearning_progress(
    username: str | None = None,
    password: str | None = None,
) -> list[dict]:
    """Login to eLearning and parse per-course completion percentages from /my page."""
    # eLearning credentials are unified with portal credentials.
    user = username or os.environ.get("STUDENT_ID")
    pwd = password or os.environ.get("PASSWORD")
    if not user or not pwd:
        raise ValueError(
            "eLearning credentials missing. Set STUDENT_ID and PASSWORD."
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            logger.info("Navigating to eLearning login page: %s", ELEARNING_LOGIN_URL)
            page.goto(ELEARNING_LOGIN_URL, wait_until="networkidle", timeout=60_000)
            page.wait_for_selector(ELEARNING_SELECTOR_USERNAME, state="visible", timeout=30_000)

            page.fill(ELEARNING_SELECTOR_USERNAME, user)
            page.fill(ELEARNING_SELECTOR_PASSWORD, pwd)
            page.locator(ELEARNING_SELECTOR_SUBMIT).first.click(timeout=10_000)
            page.wait_for_load_state("networkidle", timeout=30_000)

            if "login" in page.url.lower():
                raise RuntimeError(f"eLearning login failed. Current URL: {page.url}")

            page.goto(ELEARNING_MY_URL, wait_until="networkidle", timeout=60_000)
            # Moodle dashboards often render course cards asynchronously.
            try:
                page.wait_for_selector(
                    ".dashboard-card, .block_myoverview, [data-region='courses-view'], .progress, .progress-bar",
                    timeout=20_000,
                )
            except Exception:
                logger.warning("eLearning dashboard selectors did not appear before timeout; parsing anyway.")

            progress_rows = _parse_elearning_progress(page)
            deduped = _deduplicate_progress_rows(progress_rows)
            if not deduped:
                logger.warning(
                    "eLearning progress parser returned 0 rows. url=%s title=%s",
                    page.url,
                    page.title(),
                )
                body_excerpt = (page.locator("body").inner_text() or "").strip().replace("\n", " ")
                logger.debug("eLearning page excerpt: %s", body_excerpt[:500])
            return deduped
        finally:
            context.close()
            browser.close()


def _fetch_exam_schedule_from_portal(sid: str, pwd: str, weeks_ahead: int | None = None) -> list[dict]:
    """Fetch exam rows from the old portal / lichhoc-lichthi stack."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            page.goto(PORTAL_URL, wait_until="networkidle", timeout=60_000)
            page.wait_for_selector(SELECTOR_USERNAME, state="visible", timeout=30_000)
            page.fill(SELECTOR_USERNAME, sid)
            page.fill(SELECTOR_PASSWORD, pwd)

            login_page_url = page.url
            try:
                page.locator(SELECTOR_SUBMIT).first.click(timeout=SUBMIT_BUTTON_TIMEOUT_MS)
            except Exception:
                page.locator(SELECTOR_PASSWORD).press("Enter")

            try:
                page.wait_for_url(lambda url: "login" not in str(url).lower(), timeout=30_000)
            except PlaywrightTimeoutError:
                logger.warning("Portal exam login wait_for_url timed out.")

            page.wait_for_load_state("networkidle", timeout=30_000)
            if page.url == login_page_url or "login" in page.url.lower():
                raise RuntimeError("Portal exam login failed.")

            # Strategy 1: click a visible exam link from the authenticated portal page.
            exam_link = page.locator(
                "a[href*='lichthi'], a[href*='Lichthi'], a[href*='exam'], a[href*='Exam']"
            )
            if exam_link.count() > 0:
                for i in range(exam_link.count()):
                    candidate = exam_link.nth(i)
                    try:
                        if not candidate.is_visible():
                            continue
                        candidate.click(timeout=10_000)
                        page.wait_for_load_state("networkidle", timeout=60_000)
                        exams = _parse_exam_table_with_filters(page)
                        if exams:
                            return exams
                    except Exception:
                        continue

            # Strategy 2: move to timetable page (using visible links first), then click exam tab.
            schedule_link = page.locator(
                "a[href*='tkb'], a[href*='schedule'], a[href*='lichhoc'], a[href*='lichhoc-lichthi']"
            )
            if schedule_link.count() == 0:
                schedule_link = page.locator("a").filter(has_text=SCHEDULE_MENU_TEXT)

            clicked_schedule_link = False
            if schedule_link.count() > 0:
                for i in range(schedule_link.count()):
                    candidate = schedule_link.nth(i)
                    try:
                        if not candidate.is_visible():
                            continue
                        candidate.click(timeout=10_000)
                        page.wait_for_load_state("networkidle", timeout=60_000)
                        clicked_schedule_link = True
                        break
                    except Exception:
                        continue

            if not clicked_schedule_link:
                schedule_url = _build_schedule_url(page.url)
                page.goto(schedule_url, wait_until="networkidle", timeout=60_000)

            exam_tab = page.locator("a, button, input[type='button'], input[type='submit']").filter(
                has_text=re.compile(r"lịch\s*thi|lich\s*thi|exam", re.IGNORECASE)
            )
            if exam_tab.count() > 0:
                for i in range(exam_tab.count()):
                    candidate = exam_tab.nth(i)
                    try:
                        if not candidate.is_visible():
                            continue
                        candidate.click(timeout=10_000)
                        page.wait_for_load_state("networkidle", timeout=30_000)
                        exams = _parse_exam_table_with_filters(page)
                        if exams:
                            return exams
                    except Exception:
                        continue

            # Strategy 3: final fallback to direct exam URL built from any available token.
            exam_url = _build_exam_url(page.url)
            page.goto(exam_url, wait_until="networkidle", timeout=60_000)
            exams = _parse_exam_table_with_filters(page)
            return exams
        finally:
            context.close()
            browser.close()


def _fetch_exam_schedule_from_elearning(username: str, password: str) -> list[dict]:
    """Best-effort exam parsing from eLearning pages when portal source is unavailable."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            page.goto(ELEARNING_LOGIN_URL, wait_until="networkidle", timeout=60_000)
            page.wait_for_selector(ELEARNING_SELECTOR_USERNAME, state="visible", timeout=30_000)
            page.fill(ELEARNING_SELECTOR_USERNAME, username)
            page.fill(ELEARNING_SELECTOR_PASSWORD, password)
            page.locator(ELEARNING_SELECTOR_SUBMIT).first.click(timeout=10_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            if "login" in page.url.lower():
                raise RuntimeError("eLearning login failed while fetching exams.")

            page.goto(ELEARNING_MY_URL, wait_until="networkidle", timeout=60_000)
            exams = _parse_exam_table(page)
            return exams
        finally:
            context.close()
            browser.close()


def _fetch_exam_schedule_from_stdportal_announcements(username: str, password: str) -> list[dict]:
    """Fallback: collect exam-related announcements from stdportal homepage."""
    stdportal_home = "https://stdportal.tdtu.edu.vn/"
    stdportal_login_home = (
        "https://stdportal.tdtu.edu.vn/Login/Index?ReturnUrl=https%3A%2F%2Fstdportal.tdtu.edu.vn%2F"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            # Reuse old-portal login to establish SSO session before opening stdportal pages.
            page.goto(PORTAL_URL, wait_until="networkidle", timeout=60_000)
            page.wait_for_selector(SELECTOR_USERNAME, state="visible", timeout=30_000)
            page.fill(SELECTOR_USERNAME, username)
            page.fill(SELECTOR_PASSWORD, password)
            try:
                page.locator(SELECTOR_SUBMIT).first.click(timeout=SUBMIT_BUTTON_TIMEOUT_MS)
            except Exception:
                page.locator(SELECTOR_PASSWORD).press("Enter")
            try:
                page.wait_for_url(lambda url: "login" not in str(url).lower(), timeout=30_000)
            except Exception:
                pass

            # Two-step navigation consistently lands on authenticated stdportal home.
            page.goto(stdportal_home, wait_until="networkidle", timeout=60_000)
            page.goto(stdportal_login_home, wait_until="networkidle", timeout=60_000)

            links = page.evaluate(
                r"""
                () => {
                    const examPattern = /(lịch\s*thi|lich\s*thi|thi\s*cuối\s*kỳ|exam)/i;
                    return Array.from(document.querySelectorAll("a"))
                        .map((a) => ({
                            text: (a.innerText || "").trim(),
                            href: (a.href || "").trim(),
                        }))
                        .filter((item) => item.text && item.href)
                        .filter((item) => examPattern.test(item.text) || examPattern.test(item.href));
                }
                """
            ) or []

            rows: list[dict] = []
            seen_links: set[str] = set()
            for item in links:
                title = str(item.get("text") or "").strip()
                href = str(item.get("href") or "").strip()
                if not title or not href:
                    continue
                if href in seen_links:
                    continue
                seen_links.add(href)

                exam_date = _extract_exam_date_from_text(title)
                if not exam_date:
                    continue

                rows.append(
                    {
                        "subject_name": title,
                        "exam_date": exam_date,
                        "start_time": "",
                        "end_time": "",
                        "exam_room": "",
                        "exam_type": "Announcement",
                        "notes": f"Exam notice source: {href}",
                    }
                )

            return _deduplicate_exam_rows(rows)
        finally:
            context.close()
            browser.close()


def _extract_exam_date_from_text(text: str) -> str:
    """Extract the first DD/MM[/YYYY] token from announcement title as ISO date."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    match = re.search(r"(\d{1,2})[\/\.-](\d{1,2})(?:[\/\.-](\d{2,4}))?", cleaned)
    if not match:
        return ""

    day = int(match.group(1))
    month = int(match.group(2))
    year_text = match.group(3)
    year = int(year_text) if year_text else local_today().year
    if year < 100:
        year += 2000

    try:
        parsed = datetime.date(year, month, day)
    except ValueError:
        return ""
    return parsed.isoformat()


def _parse_exam_table(page) -> list[dict]:
    """Parse exam rows from any table with exam-like headers on current page/frames."""
    script = r"""
        () => {
            const contexts = [document, ...Array.from(document.querySelectorAll("iframe")).map((f) => {
                try { return f.contentDocument; } catch { return null; }
            }).filter(Boolean)];

            const rows = [];
            const parseDate = (text) => {
                const m = (text || "").match(/(\d{1,2})[\/\.-](\d{1,2})(?:[\/\.-](\d{2,4}))?/);
                if (!m) return "";
                const d = parseInt(m[1], 10);
                const mo = parseInt(m[2], 10);
                let y = m[3] ? parseInt(m[3], 10) : (new Date()).getFullYear();
                if (y < 100) y += 2000;
                if (!d || !mo || !y) return "";
                return `${String(y).padStart(4, "0")}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
            };
            const parseTime = (text) => {
                const m = (text || "").match(/(\d{1,2})[:h](\d{2})/i);
                if (!m) return "";
                return `${String(parseInt(m[1], 10)).padStart(2, "0")}:${m[2]}`;
            };

            for (const doc of contexts) {
                for (const table of Array.from(doc.querySelectorAll("table"))) {
                    const head = Array.from(table.querySelectorAll("tr:first-child th, tr:first-child td"))
                        .map((c) => (c.innerText || "").trim().toLowerCase());
                    const allHead = head.join(" ");
                    const hasSubject = /(môn|mon|subject)/.test(allHead);
                    const hasDate = /(ngày|ngay|date)/.test(allHead);
                    const hasTime = /(giờ|gio|time)/.test(allHead);
                    if (!hasSubject || (!hasDate && !hasTime)) continue;

                    const idxSubject = head.findIndex((h) => /(môn|mon|subject)/.test(h));
                    const idxDate = head.findIndex((h) => /(ngày|ngay|date)/.test(h));
                    const idxTime = head.findIndex((h) => /(giờ|gio|time)/.test(h));
                    const idxRoom = head.findIndex((h) => /(phòng|phong|room)/.test(h));
                    const idxType = head.findIndex((h) => /(hình thức|hinh thuc|type|loại|loai)/.test(h));

                    const trs = Array.from(table.querySelectorAll("tr")).slice(1);
                    for (const tr of trs) {
                        const tds = Array.from(tr.querySelectorAll("td")).map((c) => (c.innerText || "").trim());
                        if (!tds.length) continue;
                        const subject = idxSubject >= 0 ? (tds[idxSubject] || "") : "";
                        if (!subject) continue;
                        const dateText = idxDate >= 0 ? (tds[idxDate] || "") : tds.join(" ");
                        const dateIso = parseDate(dateText);
                        if (!dateIso) continue;
                        const timeText = idxTime >= 0 ? (tds[idxTime] || "") : tds.join(" ");
                        const start = parseTime(timeText);
                        let end = "";
                        const range = (timeText || "").match(
                            /(\d{1,2}[:h]\d{2})\s*(?:-|–|—|to|đến|den|->|~)\s*(\d{1,2}[:h]\d{2})/i
                        );
                        if (range) {
                            end = parseTime(range[2]);
                        }
                        rows.push({
                            subject_name: subject,
                            exam_date: dateIso,
                            start_time: start,
                            end_time: end,
                            exam_room: idxRoom >= 0 ? (tds[idxRoom] || "") : "",
                            exam_type: idxType >= 0 ? (tds[idxType] || "") : "",
                            notes: "Crawled from exam schedule",
                        });
                    }
                }
            }
            return rows;
        }
    """
    try:
        rows = page.evaluate(script) or []
    except Exception:
        rows = []

    rows.extend(_parse_exam_grid_cells(page))
    return _deduplicate_exam_rows(rows)


def _parse_exam_grid_cells(page) -> list[dict]:
    """Parse exam rows from grid-style cells containing Ngay thi/Gio thi text."""
    script = r"""
        () => {
            const contexts = [document, ...Array.from(document.querySelectorAll("iframe")).map((f) => {
                try { return f.contentDocument; } catch { return null; }
            }).filter(Boolean)];

            const rows = [];
            const parseDateIso = (text) => {
                const m = (text || "").match(/(\d{1,2})[\/\.-](\d{1,2})(?:[\/\.-](\d{2,4}))?/);
                if (!m) return "";
                const d = parseInt(m[1], 10);
                const mo = parseInt(m[2], 10);
                let y = m[3] ? parseInt(m[3], 10) : (new Date()).getFullYear();
                if (y < 100) y += 2000;
                if (!d || !mo || !y) return "";
                return `${String(y).padStart(4, "0")}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
            };
            const parseTime = (text) => {
                const m = (text || "").match(/(\d{1,2})[:h](\d{2})/i);
                if (!m) return "";
                return `${String(parseInt(m[1], 10)).padStart(2, "0")}:${m[2]}`;
            };

            for (const doc of contexts) {
                const cells = Array.from(doc.querySelectorAll("td, div"));
                for (const cell of cells) {
                    const text = (cell.innerText || "").trim();
                    if (!text) continue;

                    const lowered = text.toLowerCase();
                    if (!/(ngày\s*thi|ngay\s*thi|date\s*:)/i.test(lowered)) continue;
                    if (!/(giờ\s*thi|gio\s*thi|time\s*:)/i.test(lowered)) continue;

                    const lines = text
                        .split("\n")
                        .map((line) => (line || "").trim())
                        .filter((line) => line.length > 0);
                    if (!lines.length) continue;

                    const subject = (lines[0] || "").split("|")[0].trim();
                    if (!subject) continue;

                    const dateLine = lines.find((line) => /(ngày\s*thi|ngay\s*thi|date\s*:)/i.test(line)) || text;
                    const timeLine = lines.find((line) => /(giờ\s*thi|gio\s*thi|time\s*:)/i.test(line)) || text;
                    const roomLine = lines.find((line) => /(phòng\s*thi|phong\s*thi|room\s*:)/i.test(line)) || "";

                    const examDate = parseDateIso(dateLine);
                    if (!examDate) continue;

                    const start = parseTime(timeLine);
                    let end = "";
                    const range = (timeLine || "").match(
                        /(\d{1,2}[:h]\d{2})\s*(?:-|–|—|to|đến|den|->|~)\s*(\d{1,2}[:h]\d{2})/i
                    );
                    if (range) {
                        end = parseTime(range[2]);
                    }

                    let room = "";
                    const roomMatch = (roomLine || "").match(/(?:phòng\s*thi|phong\s*thi|room)\s*[:\-]?\s*(.+)$/i);
                    if (roomMatch) {
                        room = roomMatch[1].trim();
                    }

                    rows.push({
                        subject_name: subject,
                        exam_date: examDate,
                        start_time: start,
                        end_time: end,
                        exam_room: room,
                        exam_type: "",
                        notes: "Crawled from exam grid",
                    });
                }
            }

            return rows;
        }
    """

    try:
        return page.evaluate(script) or []
    except Exception:
        return []


def _parse_exam_table_with_filters(page) -> list[dict]:
    """Parse exam rows after selecting current semester and exam type filters."""
    try:
        semester_changed = _select_semester_if_available(page)
    except Exception as exc:
        logger.debug("Could not auto-select exam semester: %s", exc)
        semester_changed = False

    button_targets = _resolve_exam_type_button_targets(page)
    if button_targets:
        combined_rows: list[dict] = []
        for target in button_targets:
            changed = _click_exam_type_by_group(page, target["group"])
            if changed or semester_changed:
                page.wait_for_load_state("networkidle", timeout=30_000)

            _scroll_exam_page_to_bottom(page)
            rows = _parse_exam_table(page)
            exam_type_text = str(target.get("text") or "").strip()
            if exam_type_text:
                for row in rows:
                    if not str(row.get("exam_type") or "").strip():
                        row["exam_type"] = exam_type_text
            combined_rows.extend(rows)

        return _deduplicate_exam_rows(combined_rows)

    type_targets = _resolve_exam_type_targets(page)
    if not type_targets:
        if semester_changed:
            page.wait_for_load_state("networkidle", timeout=30_000)
        _scroll_exam_page_to_bottom(page)
        return _parse_exam_table(page)

    combined_rows: list[dict] = []
    for target in type_targets:
        changed = _select_exam_type_by_value(page, target["value"])
        if changed or semester_changed:
            page.wait_for_load_state("networkidle", timeout=30_000)

        _scroll_exam_page_to_bottom(page)
        rows = _parse_exam_table(page)
        exam_type_text = str(target.get("text") or "").strip()
        if exam_type_text:
            for row in rows:
                if not str(row.get("exam_type") or "").strip():
                    row["exam_type"] = exam_type_text
        combined_rows.extend(rows)

    return _deduplicate_exam_rows(combined_rows)


def _resolve_exam_type_targets(page) -> list[dict]:
    """Resolve target exam-type dropdown options (midterm/final) to crawl."""
    desired = _desired_exam_type_groups()

    for select in page.locator("select").all():
        try:
            if not select.is_visible():
                continue

            options = select.locator("option").all()
            parsed_options: list[dict] = []
            has_exam_type_option = False
            for option in options:
                value = (option.get_attribute("value") or "").strip()
                text = (option.inner_text() or "").strip()
                if not value:
                    continue

                group = _exam_type_group(text)
                if group is not None:
                    has_exam_type_option = True

                parsed_options.append(
                    {
                        "value": value,
                        "text": text,
                        "group": group,
                    }
                )

            if not has_exam_type_option:
                continue

            selected: list[dict] = []
            for group in desired:
                match = next((item for item in parsed_options if item["group"] == group), None)
                if match and all(existing["value"] != match["value"] for existing in selected):
                    selected.append(match)

            if selected:
                return selected

            return [item for item in parsed_options if item["group"] is not None]
        except Exception as exc:
            logger.debug("Skipping exam type select candidate due to error: %s", exc)

    return []


def _resolve_exam_type_button_targets(page) -> list[dict]:
    """Resolve exam type targets from tab/button controls (midterm/final)."""
    desired_groups = _desired_exam_type_groups()
    available = {
        "midterm": _exam_type_button_exists(page, "midterm"),
        "final": _exam_type_button_exists(page, "final"),
    }

    targets: list[dict] = []
    for group in desired_groups:
        if not available.get(group):
            continue
        label = "Giữa kỳ" if group == "midterm" else "Cuối kỳ"
        targets.append({"group": group, "text": label})

    if targets:
        return targets

    for group in ("midterm", "final"):
        if available.get(group):
            label = "Giữa kỳ" if group == "midterm" else "Cuối kỳ"
            targets.append({"group": group, "text": label})
    return targets


def _exam_type_button_exists(page, group: str) -> bool:
    for selector in _exam_type_button_selectors(group):
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
            for index in range(locator.count()):
                if locator.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


def _click_exam_type_by_group(page, group: str) -> bool:
    """Click exam type control by group if available."""
    for selector in _exam_type_button_selectors(group):
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if not candidate.is_visible():
                    continue
                candidate.click(timeout=10_000)
                page.wait_for_load_state("networkidle", timeout=30_000)
                logger.info("Selected exam type tab/button for group=%s using selector=%s", group, selector)
                return True
        except Exception as exc:
            logger.debug("Exam type button click failed (group=%s selector=%s): %s", group, selector, exc)
            continue

    return False


def _exam_type_button_selectors(group: str) -> list[str]:
    if group == "midterm":
        return [
            "input[type='button'][value*='giữa kỳ' i]",
            "input[type='button'][value*='giuaky' i]",
            "input[type='button'][value*='mid' i]",
            "input[type='submit'][value*='giữa kỳ' i]",
            "input[type='submit'][value*='giuaky' i]",
            "input[type='submit'][value*='mid' i]",
            "button:has-text('giữa kỳ')",
            "button:has-text('giua ky')",
            "button:has-text('mid')",
            "a:has-text('giữa kỳ')",
            "a:has-text('giua ky')",
            "a:has-text('mid')",
        ]

    return [
        "input[type='button'][value*='cuối kỳ' i]",
        "input[type='button'][value*='cuoiky' i]",
        "input[type='button'][value*='final' i]",
        "input[type='submit'][value*='cuối kỳ' i]",
        "input[type='submit'][value*='cuoiky' i]",
        "input[type='submit'][value*='final' i]",
        "button:has-text('cuối kỳ')",
        "button:has-text('cuoi ky')",
        "button:has-text('final')",
        "a:has-text('cuối kỳ')",
        "a:has-text('cuoi ky')",
        "a:has-text('final')",
    ]


def _scroll_exam_page_to_bottom(page) -> None:
    """Scroll down exam page to reveal full data grids before parsing."""
    try:
        page.evaluate(
            r"""
            () => {
                window.scrollTo(0, 0);
                const step = Math.max(500, Math.floor(window.innerHeight * 0.8));
                let y = 0;
                const maxY = Math.max(
                    document.body ? document.body.scrollHeight : 0,
                    document.documentElement ? document.documentElement.scrollHeight : 0,
                );
                while (y < maxY + step) {
                    window.scrollTo(0, y);
                    y += step;
                }
                window.scrollTo(0, maxY);
            }
            """
        )
        page.wait_for_timeout(1200)
    except Exception as exc:
        logger.debug("Exam page scroll helper failed: %s", exc)


def _select_exam_type_by_value(page, target_value: str) -> bool:
    """Select an exam type option by value if available."""
    value = str(target_value or "").strip()
    if not value:
        return False

    for select in page.locator("select").all():
        try:
            if not select.is_visible():
                continue

            options = select.locator("option").all()
            for option in options:
                option_value = (option.get_attribute("value") or "").strip()
                if option_value != value:
                    continue

                current_value = (select.input_value() or "").strip()
                if current_value == value:
                    return False

                select.select_option(value)
                page.wait_for_load_state("networkidle", timeout=30_000)
                logger.info("Selected exam type option value=%s", value)
                return True
        except Exception as exc:
            logger.debug("Exam type select attempt failed: %s", exc)

    return False


def _desired_exam_type_groups() -> list[str]:
    """Read desired exam groups from env. Defaults to both midterm and final."""
    raw = (os.environ.get("TARGET_EXAM_TYPES") or "midterm,final").strip()
    if not raw:
        return ["midterm", "final"]

    groups: list[str] = []
    for token in raw.split(","):
        normalized = token.strip().lower()
        if not normalized:
            continue
        if normalized in {"mid", "midterm", "giua", "giuaky", "giua_ky", "giua-ky", "gk", "giuakythi"}:
            if "midterm" not in groups:
                groups.append("midterm")
            continue
        if normalized in {"final", "cuoi", "cuoiky", "cuoi_ky", "cuoi-ky", "ck", "cuoikythi"}:
            if "final" not in groups:
                groups.append("final")
            continue

    return groups or ["midterm", "final"]


def _exam_type_group(text: str) -> str | None:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return None

    if re.search(r"giữa\s*kỳ|giua\s*ky|midterm", lowered, re.IGNORECASE):
        return "midterm"
    if re.search(r"cuối\s*kỳ|cuoi\s*ky|final", lowered, re.IGNORECASE):
        return "final"
    return None


def _parse_elearning_progress(page) -> list[dict]:
    """Extract per-course completion percentages from eLearning dashboard page."""
    script = r"""
        () => {
            const selectors = [
                ".dashboard-card",
                ".block_myoverview [data-course-id]",
                ".block_myoverview .dashboard-card",
                ".block_myoverview .card[data-course-id]",
                ".coursebox",
                "li.course",
                "[data-course-id]"
            ];
            const cards = Array.from(document.querySelectorAll(selectors.join(", ")));

            const courseNameById = {};
            const allCourseLinks = Array.from(document.querySelectorAll("a[href*='/course/view.php?id=']"));
            for (const link of allCourseLinks) {
                const href = link.getAttribute("href") || "";
                const idMatch = href.match(/[?&]id=(\d+)/);
                if (!idMatch) continue;
                const courseId = idMatch[1];

                let name = (link.textContent || "").trim().replace(/\s+/g, " ");
                name = name
                    .replace(/^course\s*is\s*starred\s*/i, "")
                    .replace(/^course\s*name\s*/i, "")
                    .trim();
                if (!name) continue;
                if (/^course\s*image$/i.test(name)) continue;
                if (/^course\s*category$/i.test(name)) continue;
                if (/^skip\s*course\s*overview$/i.test(name)) continue;

                const existing = courseNameById[courseId] || "";
                if (!existing || name.length > existing.length) {
                    courseNameById[courseId] = name;
                }
            }

            const clampPercent = (value) => {
                if (value === null || value === undefined) return null;
                const n = Number(value);
                if (!Number.isFinite(n)) return null;
                return Math.max(0, Math.min(100, Math.round(n)));
            };

            const parsePercentFromText = (text) => {
                const m = (text || "").match(/(\d{1,3})\s*%/);
                if (!m) return null;
                return clampPercent(parseInt(m[1], 10));
            };

            const parsePercentFromStyle = (styleValue) => {
                const m = (styleValue || "").match(/width\s*:\s*(\d{1,3}(?:\.\d+)?)\s*%/i);
                if (!m) return null;
                return clampPercent(parseFloat(m[1]));
            };

            const parseLessonRatio = (text) => {
                const m = (text || "").match(/(\d+)\s*[\/]\s*(\d+)/);
                if (!m) return [null, null];
                return [parseInt(m[1], 10), parseInt(m[2], 10)];
            };

            const pickCourseName = (card, text) => {
                const isNoise = (value) => {
                    const s = (value || "").trim();
                    if (!s) return true;
                    return /^(course\s*image|hình\s*ảnh\s*khóa\s*học|course\s*category|skip\s*course\s*overview|show\s*more|show\s*less)$/i.test(s);
                };

                const candidates = [];
                const pushText = (value) => {
                    const s = (value || "").trim();
                    if (!s) return;
                    candidates.push(s.replace(/\s+/g, " "));
                };

                const primaryNodes = card.querySelectorAll(
                    "a.aalink.coursename, .coursename a, [data-region='course-title'], a[href*='/course/view.php'] .multiline, h3, h4, .multiline"
                );
                for (const node of Array.from(primaryNodes)) {
                    pushText(node.textContent || "");
                }

                const fallbackLink = card.querySelector("a[href*='/course/view.php']");
                if (fallbackLink) {
                    pushText(fallbackLink.textContent || "");
                }

                const textLines = (text || "")
                    .split("\n")
                    .map((line) => line.trim())
                    .filter((line) => line.length > 0 && !isNoise(line));
                if (textLines.length > 0) {
                    pushText(textLines[0]);
                }

                for (const candidate of candidates) {
                    if (!isNoise(candidate)) return candidate;
                }
                return "";
            };

            const findPercent = (card, text) => {
                // 1) Direct text percentage inside card
                const fromText = parsePercentFromText(text);
                if (fromText !== null) return fromText;

                // 2) aria-valuenow commonly used by bootstrap progress bars
                const withAria = card.querySelector("[aria-valuenow]");
                if (withAria) {
                    const ariaValue = withAria.getAttribute("aria-valuenow");
                    const pct = clampPercent(ariaValue);
                    if (pct !== null) return pct;
                }

                // 3) data-progress style attrs used by some Moodle themes/plugins
                const withData = card.querySelector("[data-progress], [data-percentage], [data-percent]");
                if (withData) {
                    const raw = withData.getAttribute("data-progress")
                        || withData.getAttribute("data-percentage")
                        || withData.getAttribute("data-percent");
                    const pct = clampPercent(raw);
                    if (pct !== null) return pct;
                }

                // 4) width style of progress bar elements
                const bar = card.querySelector(".progress-bar, [role='progressbar']");
                if (bar) {
                    const stylePct = parsePercentFromStyle(bar.getAttribute("style") || "");
                    if (stylePct !== null) return stylePct;
                    const ariaNow = clampPercent(bar.getAttribute("aria-valuenow"));
                    if (ariaNow !== null) return ariaNow;
                    const titlePct = parsePercentFromText(bar.getAttribute("title") || "");
                    if (titlePct !== null) return titlePct;
                }

                return null;
            };

            const rows = [];
            for (const card of cards) {
                const text = (card.innerText || "").trim();
                const pct = findPercent(card, text);
                if (pct === null) continue;

                let courseName = pickCourseName(card, text);
                if (!courseName) continue;

                const courseLink = card.querySelector("a[href*='/course/view.php?id=']");
                let courseId = "";
                if (courseLink) {
                    const href = courseLink.getAttribute("href") || "";
                    const m = href.match(/[?&]id=(\d+)/);
                    if (m) courseId = m[1];
                }
                if (!courseId) {
                    const attrId = card.getAttribute("data-course-id") || card.getAttribute("data-courseid") || "";
                    if (attrId) courseId = attrId.trim();
                }
                // Ignore cards that cannot be mapped to a concrete Moodle course.
                if (!courseId) continue;

                const mappedName = courseNameById[courseId] || "";
                if (mappedName) {
                    courseName = mappedName;
                }

                const [done, total] = parseLessonRatio(text);
                rows.push({
                    course_id: courseId,
                    course_name: courseName,
                    progress_percent: pct,
                    lessons_completed: done,
                    lessons_total: total,
                });
            }

            // Fallback: some themes render a table/list without cards.
            if (!rows.length) {
                const links = Array.from(document.querySelectorAll("a[href*='/course/view.php?id=']"));
                for (const link of links) {
                    const href = link.getAttribute("href") || "";
                    const idMatch = href.match(/[?&]id=(\d+)/);
                    const courseId = idMatch ? idMatch[1] : "";
                    const courseName = (link.textContent || "").trim();
                    if (!courseName) continue;

                    const container = link.closest("li, tr, .card, .media, .coursebox") || link.parentElement;
                    const text = (container?.innerText || "").trim();
                    const pct = parsePercentFromText(text);
                    if (pct === null) continue;

                    const [done, total] = parseLessonRatio(text);
                    rows.push({
                        course_id: courseId,
                        course_name: courseName,
                        progress_percent: pct,
                        lessons_completed: done,
                        lessons_total: total,
                    });
                }
            }

            return rows;
        }
    """
    try:
        rows = page.evaluate(script) or []
    except Exception:
        rows = []
    return rows


def _deduplicate_exam_rows(rows: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for row in rows:
        subject = str(row.get("subject_name") or "").strip()
        exam_date = str(row.get("exam_date") or "").strip()
        start_time = str(row.get("start_time") or "").strip()
        end_time = str(row.get("end_time") or "").strip()
        room = str(row.get("exam_room") or row.get("room") or "").strip()
        exam_type = str(row.get("exam_type") or "").strip()
        if not subject or not exam_date:
            continue
        key = (subject.lower(), exam_date, start_time, end_time, room.lower(), exam_type.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(
            {
                "subject_name": subject,
                "exam_date": exam_date,
                "start_time": start_time,
                "end_time": end_time,
                "exam_room": room,
                "exam_type": exam_type or None,
                "notes": str(row.get("notes") or "").strip() or None,
            }
        )
    return deduped


def _deduplicate_progress_rows(rows: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        course_name = _clean_course_name(str(row.get("course_name") or "").strip())
        course_id = str(row.get("course_id") or "").strip()
        if not course_name:
            continue
        key = course_id.lower() or course_name.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(
            {
                "course_id": course_id,
                "course_name": course_name,
                "progress_percent": row.get("progress_percent") or 0,
                "lessons_completed": row.get("lessons_completed"),
                "lessons_total": row.get("lessons_total"),
            }
        )
    return deduped


def _clean_course_name(name: str) -> str:
    """Normalize noisy Moodle card labels to human-readable course names."""
    text = str(name or "").strip()
    if not text:
        return ""

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^course\s*is\s*starred\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^course\s*name\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^course\s*", "", text, flags=re.IGNORECASE)
    return text.strip(" -:\t")


def _build_exam_url(current_url: str) -> str:
    """Build exam URL with Token/RequestId from authenticated session URL."""
    parsed = urlparse(current_url)
    query = parse_qs(parsed.query)

    token = (query.get("Token") or [""])[0]
    request_id = (query.get("RequestId") or [""])[0]

    if token and request_id:
        return f"{EXAM_URL_BASE}?Token={token}&RequestId={request_id}"
    return EXAM_URL_BASE


def _capture_week_signature(page) -> str:
    """Get a lightweight signature for current week-view header to detect week changes."""
    current_week_controls = page.locator(
        "input[id*='btnTuanHienTai'], input[name*='btnTuanHienTai'], #ThoiKhoaBieu1_btnTuanHienTai"
    )
    try:
        if current_week_controls.count() > 0:
            value = (current_week_controls.first.input_value() or "").strip()
            if value:
                return re.sub(r"\s+", " ", value)
    except Exception:
        pass

    header_candidates = [
        "table tr:first-child",
        "table thead tr:first-child",
    ]
    for selector in header_candidates:
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
            text = (locator.first.inner_text() or "").strip()
            if text:
                return re.sub(r"\s+", " ", text)
        except Exception:
            continue

    return page.url


def _goto_next_week(page) -> bool:
    """Navigate to the next timetable week using common portal controls."""
    before = _capture_week_signature(page)
    selectors = [
        "#ThoiKhoaBieu1_btnTuanSau",
        "input[id*='btnTuanSau']",
        "input[name*='btnTuanSau']",
        "button:has-text('Tuần sau')",
        "a:has-text('Tuần sau')",
        "input[type='button'][value*='Tuần sau']",
        "input[type='submit'][value*='Tuần sau']",
        "input[value*='Following week']",
        "button:has-text('Next week')",
        "a:has-text('Next week')",
        "button:has-text('Next')",
        "a:has-text('Next')",
        "input[type='button'][value*='Next']",
        "input[type='submit'][value*='Next']",
        "button[title*='next' i]",
        "a[title*='next' i]",
        "button[aria-label*='next' i]",
        "a[aria-label*='next' i]",
    ]

    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
        except Exception:
            continue

        for idx in range(locator.count()):
            control = locator.nth(idx)
            try:
                if not control.is_visible():
                    continue
                control.click(timeout=8_000)
                try:
                    page.wait_for_function(
                        r"""
                        (previous) => {
                            const currentWeek = document.querySelector(
                                "input[id*='btnTuanHienTai'], input[name*='btnTuanHienTai'], #ThoiKhoaBieu1_btnTuanHienTai"
                            );
                            if (currentWeek) {
                                const value = (currentWeek.value || currentWeek.innerText || "").trim().replace(/\s+/g, " ");
                                if (value) {
                                    return value !== previous;
                                }
                            }

                            const header = document.querySelector("table tr:first-child, table thead tr:first-child");
                            if (!header) return false;
                            const text = (header.innerText || "").trim().replace(/\s+/g, " ");
                            return !!text && text !== previous;
                        }
                        """,
                        before,
                        timeout=15_000,
                    )
                except Exception:
                    pass
                page.wait_for_load_state("networkidle", timeout=30_000)
                page.wait_for_timeout(1_200)
                after = _capture_week_signature(page)
                if after != before:
                    logger.info("Moved to next week using selector: %s", selector)
                    return True
            except Exception:
                continue

    return False


def _configure_schedule_filters(page) -> None:
    """Try to select a semester and switch to week view before parsing."""
    semester_changed = _select_semester_if_available(page)
    week_view_changed = False

    try:
        week_view_changed = _switch_to_week_view_if_available(page)
    except Exception as exc:
        # Some ASP.NET controls trigger a full postback after semester change.
        # Retry once after waiting for navigation to stabilize.
        if semester_changed and "Execution context was destroyed" in str(exc):
            logger.info("Page reloaded after semester change; retrying week-view selection.")
            page.wait_for_load_state("networkidle", timeout=30_000)
            week_view_changed = _switch_to_week_view_if_available(page)
        else:
            raise

    if semester_changed or week_view_changed:
        try:
            _click_apply_filter_if_available(page)
        except Exception as exc:
            if "Execution context was destroyed" in str(exc):
                logger.info("Page reloaded while applying filters; continuing with latest state.")
                page.wait_for_load_state("networkidle", timeout=30_000)
            else:
                raise


def _select_semester_if_available(page) -> bool:
    """Select a likely semester option from any visible dropdown, if present."""
    preferred_semester = (os.environ.get("TARGET_SEMESTER") or "").strip().lower()

    for select in page.locator("select").all():
        try:
            if not select.is_visible():
                continue

            options = select.locator("option").all()
            if not options:
                continue

            option_texts = [opt.inner_text().strip() for opt in options]
            searchable = " | ".join(option_texts)
            if not SEMESTER_TEXT.search(searchable):
                continue

            current_value = (select.input_value() or "").strip()
            valid_options: list[tuple[str, str]] = []

            for opt in options:
                value = (opt.get_attribute("value") or "").strip()
                text = opt.inner_text().strip()
                text_lower = text.lower()

                if not value:
                    continue
                if any(token in text_lower for token in ["chọn", "select", "--"]):
                    continue

                valid_options.append((value, text))

            if not valid_options:
                continue

            target_value, target_text = _pick_target_semester(
                valid_options,
                preferred_semester=preferred_semester,
                current_value=current_value,
            )

            if not target_value:
                logger.info("No suitable semester target found; keeping current selection: %s", current_value)
                return False

            if current_value == target_value:
                logger.info("Semester is already selected: %s (%s)", current_value, target_text)
                return False

            select.select_option(target_value)
            page.wait_for_load_state("networkidle", timeout=30_000)
            logger.info("Selected semester option: %s", target_text)
            return True
        except Exception as exc:
            logger.debug("Skipping non-semester select due to error: %s", exc)

    logger.info("No semester dropdown detected on schedule page.")
    return False


def _pick_target_semester(
    valid_options: list[tuple[str, str]],
    preferred_semester: str,
    current_value: str,
) -> tuple[str | None, str | None]:
    """Choose semester option by env override, then by date-based default."""
    # 1) Explicit override from env, e.g. TARGET_SEMESTER=HK2/2025-2026
    if preferred_semester:
        for value, text in valid_options:
            if preferred_semester in text.lower():
                logger.info("Using TARGET_SEMESTER override: %s", text)
                return value, text
        logger.warning("TARGET_SEMESTER=%s not found in dropdown options.", preferred_semester)

    # 2) Date-based default: Jan-Jul -> HK2/(year-1)-year, Aug-Dec -> HK1/year-(year+1)
    today = local_today()
    if today.month <= 7:
        hk_num = 2
        start_year = today.year - 1
        end_year = today.year
    else:
        hk_num = 1
        start_year = today.year
        end_year = today.year + 1

    # Match flexibly: strip all separators/spaces from both the target and the option
    # so that "HK2/2025-2026", "HK2 2025-2026", "HK2-2025-2026" are all equivalent.
    def _sem_key(s: str) -> str:
        return re.sub(r'[\s/\-]+', '', s.lower())

    default_key = _sem_key(f"hk{hk_num}{start_year}{end_year}")
    for value, text in valid_options:
        if default_key in _sem_key(text):
            logger.info("Auto-selected semester by date rule: %s", text)
            return value, text

    # Also try matching on just the year range + semester number with common Vietnamese prefixes
    # Normalize away spaces around separators for the year-range check.
    year_re = re.compile(rf'{start_year}\s*[-/]\s*{end_year}')
    # k[yỳiì]: matches "ky" (unaccented), "kỳ" (grave), "ki", "kì" – Vietnamese romanisations of "kỳ"
    sem_re = re.compile(
        rf'(?:hk|ky|k[yỳiì]|học\s*kỳ|hoc\s*ky|semester)\s*[/\-]?\s*{hk_num}(?!\d)',
        re.IGNORECASE,
    )
    for value, text in valid_options:
        if year_re.search(text) and sem_re.search(text):
            logger.info("Auto-selected semester by date rule (flexible match): %s", text)
            return value, text

    # 3) Keep current if it still maps to a valid option
    for value, text in valid_options:
        if value == current_value:
            return value, text

    # 4) Final fallback to first valid item
    if valid_options:
        return valid_options[0]

    return None, None


def _switch_to_week_view_if_available(page) -> bool:
    """Switch to week-view mode via radio/button/link if that control exists."""
    # Strategy 1: radio controls commonly used by ASP.NET pages
    radio = page.locator(
        "input[type='radio'][id*='Tuan'], "
        "input[type='radio'][name*='Tuan'], "
        "input[type='radio'][value*='tuần'], "
        "input[type='radio'][value*='tuan'], "
        "input[type='radio'][value*='week'], "
        "input[type='radio'][id*='Week'], "
        "input[type='radio'][name*='Week']"
    )

    if radio.count() > 0:
        for i in range(radio.count()):
            try:
                item = radio.nth(i)
                if item.is_visible() and not item.is_checked():
                    item.check()
                    try:
                        page.wait_for_load_state("networkidle", timeout=30_000)
                    except Exception:
                        pass
                    logger.info("Switched schedule mode to week view via radio control.")
                    return True
            except Exception:
                continue

    # Strategy 2: clickable controls with text
    candidates = page.locator(
        "button:has-text('Xem lịch theo tuần'), a:has-text('Xem lịch theo tuần'), "
        "button:has-text('Theo tuần'), a:has-text('Theo tuần'), "
        "input[type='button'][value*='tuần'], input[type='submit'][value*='tuần'], "
        "button:has-text('Weekly'), a:has-text('Weekly')"
    )

    if candidates.count() > 0:
        for i in range(candidates.count()):
            try:
                item = candidates.nth(i)
                if item.is_visible():
                    text = item.inner_text().strip()
                    if text and not WEEK_VIEW_TEXT.search(text):
                        continue
                    item.click()
                    page.wait_for_load_state("networkidle", timeout=30_000)
                    logger.info("Switched schedule mode to week view via clickable control.")
                    return True
            except Exception:
                continue

    logger.info("No week-view control detected on schedule page.")
    return False


def _click_apply_filter_if_available(page) -> None:
    """Click a likely "apply/view" button if filters require explicit submission."""
    apply_controls = page.locator(
        "input[type='submit'][value*='Xem'], input[type='button'][value*='Xem'], "
        "button:has-text('Xem'), a:has-text('Xem'), "
        "input[type='submit'][value*='Apply'], input[type='button'][value*='Apply'], "
        "button:has-text('Apply'), a:has-text('Apply')"
    )

    try:
        if apply_controls.count() == 0:
            return
    except Exception as exc:
        if "Execution context was destroyed" in str(exc):
            page.wait_for_load_state("networkidle", timeout=30_000)
            return
        raise

    for i in range(apply_controls.count()):
        try:
            control = apply_controls.nth(i)
            if not control.is_visible():
                continue

            # Avoid clicking unrelated navigation controls.
            label = (control.inner_text() or "").strip()
            if label and not re.search(r"xem|apply", label, re.IGNORECASE):
                continue

            control.click()
            page.wait_for_load_state("networkidle", timeout=30_000)
            logger.info("Applied schedule filters.")
            return
        except Exception:
            continue


def _build_schedule_url(current_url: str) -> str:
    """Build the schedule URL with Token/RequestId from the authenticated session."""
    parsed = urlparse(current_url)
    query = parse_qs(parsed.query)

    token = (query.get("Token") or [""])[0]
    request_id = (query.get("RequestId") or [""])[0]

    if token and request_id:
        return f"{SCHEDULE_URL_BASE}?Token={token}&RequestId={request_id}"

    logger.warning(
        "Could not extract Token/RequestId from URL (%s); using base schedule URL.",
        current_url,
    )
    return SCHEDULE_URL_BASE


def _log_all_table_tds(page) -> None:
    """Log ALL <td> text content from every table on the page (and iframes) for inspection."""
    script = r"""
        () => {
            const contexts = [document, ...Array.from(document.querySelectorAll("iframe")).map((f) => {
                try { return f.contentDocument; } catch { return null; }
            }).filter(Boolean)];

            const results = [];
            contexts.forEach((doc, docIdx) => {
                const tables = Array.from(doc.querySelectorAll("table"));
                tables.forEach((tbl, tblIdx) => {
                    const tds = Array.from(tbl.querySelectorAll("td"));
                    tds.forEach((td, tdIdx) => {
                        const text = (td.innerText || "").trim().replace(/\s+/g, " ");
                        if (text) {
                            const tr = td.closest("tr");
                            const rowIdx = tr ? Array.from(tr.parentElement.children).indexOf(tr) : -1;
                            const colIdx = tr ? Array.from(tr.children).indexOf(td) : -1;
                            results.push({
                                doc: docIdx,
                                table: tblIdx,
                                row: rowIdx,
                                col: colIdx,
                                td: tdIdx,
                                text: text
                            });
                        }
                    });
                });
            });
            return results;
        }
    """
    try:
        tds = page.evaluate(script) or []
        logger.info("=== RAW TABLE TD DUMP: %d non-empty <td> cells found ===", len(tds))
        for item in tds:
            logger.info(
                "  doc[%d] table[%d] row[%d] col[%d]: %s",
                item.get("doc", 0),
                item.get("table", -1),
                item.get("row", -1),
                item.get("col", -1),
                item.get("text", ""),
            )
        logger.info("=== END RAW TABLE TD DUMP ===")
    except Exception as exc:
        logger.warning("Failed to dump raw table <td> cells: %s", exc)


def _parse_schedule_table(page, student_id: str) -> list[dict]:
    """
    Locate the first <table> that looks like a schedule table and extract rows.

    The portal typically uses a table with columns similar to:
        STT | Môn học | Nhóm | Phòng | Thứ | Tiết bắt đầu | Tiết kết thúc | …

    Because the exact column order may vary, we detect column positions by
    inspecting the header row.
    """
    # Dump all <td> cells for raw inspection before any filtering.
    _log_all_table_tds(page)

    weekly_entries = _parse_weekly_grid_table(page, student_id)
    if weekly_entries is not None:
        # Grid table structure was detected (weekly_entries may be [] for an empty week).
        if weekly_entries:
            logger.info("Parsed %d entries from weekly grid table.", len(weekly_entries))
            logger.info("=== WEEKLY GRID ENTRIES ===")
            for i, entry in enumerate(weekly_entries):
                logger.info(
                    "  [%d] subject=%r room=%r day=%r date=%r period=%s-%s status=%s",
                    i + 1,
                    entry.get("subject_name"),
                    entry.get("room"),
                    entry.get("day_of_week"),
                    entry.get("session_date"),
                    entry.get("start_period"),
                    entry.get("end_period"),
                    entry.get("status"),
                )
            logger.info("=== END WEEKLY GRID ENTRIES ===")
        else:
            logger.info("Weekly grid table found but contains no entries for this week.")
        return weekly_entries

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
        required = ["subject", "day", "start", "end"]
        if any(col.get(key) is None for key in required):
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
                start_match = re.search(r"\d+", start_raw)
                end_match = re.search(r"\d+", end_raw)
                start_period = int(start_match.group()) if start_match else 0
                end_period = int(end_match.group()) if end_match else 0
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
            logger.info("=== COLUMN-BASED TABLE ENTRIES ===")
            logger.info("  Headers: %s", headers_raw)
            logger.info("  Column mapping: %s", col)
            for i, entry in enumerate(entries):
                logger.info(
                    "  [%d] subject=%r room=%r day=%r period=%s-%s",
                    i + 1,
                    entry.get("subject_name"),
                    entry.get("room"),
                    entry.get("day_of_week"),
                    entry.get("start_period"),
                    entry.get("end_period"),
                )
            logger.info("=== END COLUMN-BASED TABLE ENTRIES ===")
            return entries

    raise RuntimeError(
        "Could not locate a parseable schedule table on the page. "
        "The portal markup may have changed."
    )


def _parse_weekly_grid_table(page, student_id: str) -> list[dict] | None:
    """Parse timetable from the week-view matrix layout (Period x Day)."""
    script = r"""
        () => {
            const englishDays = [
                ["monday", "Monday"],
                ["tuesday", "Tuesday"],
                ["wednesday", "Wednesday"],
                ["thursday", "Thursday"],
                ["friday", "Friday"],
                ["saturday", "Saturday"],
                ["sunday", "Sunday"]
            ];

            const extractWeekday = (headerText) => {
                const lower = (headerText || "").replace(/\s+/g, " ").toLowerCase();

                for (const [needle, day] of englishDays) {
                    if (lower.includes(needle)) return day;
                }

                const vnMap = [
                    ["thứ 2", "Monday"],
                    ["thu 2", "Monday"],
                    ["thứ hai", "Monday"],
                    ["thứ 3", "Tuesday"],
                    ["thu 3", "Tuesday"],
                    ["thứ ba", "Tuesday"],
                    ["thứ 4", "Wednesday"],
                    ["thu 4", "Wednesday"],
                    ["thứ tư", "Wednesday"],
                    ["thứ 5", "Thursday"],
                    ["thu 5", "Thursday"],
                    ["thứ năm", "Thursday"],
                    ["thứ 6", "Friday"],
                    ["thu 6", "Friday"],
                    ["thứ sáu", "Friday"],
                    ["thứ 7", "Saturday"],
                    ["thu 7", "Saturday"],
                    ["thứ bảy", "Saturday"],
                    ["chủ nhật", "Sunday"],
                    ["chu nhat", "Sunday"],
                    ["cn", "Sunday"]
                ];
                for (const [vn, en] of vnMap) {
                    if (lower.includes(vn)) return en;
                }
                return "";
            };

            const extractDate = (headerText) => {
                const text = (headerText || "").replace(/\s+/g, " ");
                const m = text.match(/(\d{1,2})[\/.\-](\d{1,2})(?:[\/.\-](\d{2,4}))?/);
                if (!m) return "";

                const day = parseInt(m[1], 10);
                const month = parseInt(m[2], 10);
                let year = m[3] ? parseInt(m[3], 10) : (new Date()).getFullYear();
                if (year < 100) year += 2000;

                if (!day || !month || !year) return "";
                if (month < 1 || month > 12 || day < 1 || day > 31) return "";
                return `${year.toString().padStart(4, "0")}-${month.toString().padStart(2, "0")}-${day.toString().padStart(2, "0")}`;
            };

            const cleanSubject = (text) => {
                const first = (text || "").split("\n")[0] || "";
                return first.split("|")[0].trim();
            };

            const extractRoom = (text) => {
                const roomMatch = (text || "").match(/Phòng\|Room:\s*([^\n]+)/i)
                    || (text || "").match(/Room:\s*([^\n]+)/i)
                    || (text || "").match(/Phòng:\s*([^\n]+)/i)
                    || (text || "").match(/[Pp]h[oò]ng\s*([A-Za-z0-9]+)/);
                return roomMatch ? roomMatch[1].trim() : "";
            };

            const detectStatus = (text) => {
                const lower = (text || "").toLowerCase()
                    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
                    .replace(/\s+/g, " ");
                // Absence notification keywords: "báo vắng", "GV vắng", "nghỉ học",
                // "nghỉ tiết", "vắng tiết", "GV báo vắng", "lớp nghỉ"
                const absentPattern = /bao\s*vang|gv\s*vang|nghi\s*hoc|nghi\s*tiet|vang\s*tiet|gv\s*bao\s*vang|lop\s*nghi/;
                if (absentPattern.test(lower)) {
                    return "absent";
                }
                // Makeup class keywords: "học bù", "lịch bù", "dạy bù", "bù học",
                // "bù tiết", "LHB" (lịch học bù abbreviation)
                const makeupPattern = /hoc\s*bu|lich\s*bu|day\s*bu|bu\s*hoc|bu\s*tiet|lhb/;
                if (makeupPattern.test(lower)) {
                    return "makeup";
                }
                return "scheduled";
            };

            // Split a cell that contains multiple schedule sub-entries into an
            // array of plain-text segments, one per sub-entry.  Each sub-entry
            // starts with a non-status <b> node (the subject name).
            // Returns null when the cell appears to contain a single entry.
            const splitCellEntries = (cell) => {
                const isStatusBold = (bText) => {
                    const s = (bText || "")
                        .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
                        .replace(/\s+/g, " ").trim().toLowerCase();
                    return /bao\s*vang|gv\s*vang|nghi\s*hoc|nghi\s*tiet|vang\s*tiet|lop\s*nghi|hoc\s*bu|lich\s*bu|day\s*bu|bu\s*hoc|bu\s*tiet|lhb/.test(s);
                };
                const groups = [];
                let current = [];
                for (const node of Array.from(cell.childNodes)) {
                    const nodeText = (node.textContent || "").trim();
                    const isSubjectBold = node.nodeName === "B" && !!nodeText && !isStatusBold(nodeText);
                    if (isSubjectBold && current.length > 0) {
                        groups.push(current);
                        current = [node];
                    } else {
                        current.push(node);
                    }
                }
                if (current.length > 0) groups.push(current);
                if (groups.length <= 1) return null;
                return groups.map((nodes) => {
                    const wrap = document.createElement("span");
                    nodes.forEach((n) => wrap.appendChild(n.cloneNode(true)));
                    return (wrap.innerText || "").trim();
                }).filter((t) => t.length > 0);
            };

            let target = null;
            let targetRows = [];

            for (const tbl of Array.from(document.querySelectorAll("table"))) {
                const rows = Array.from(tbl.querySelectorAll(":scope > tbody > tr, :scope > tr"));
                if (rows.length < 2) continue;

                const headerCells = Array.from(rows[0].querySelectorAll(":scope > th, :scope > td"));
                if (headerCells.length < 3) continue;

                const firstHeader = (headerCells[0]?.innerText || "").toLowerCase();
                const allHeaders = headerCells.map((c) => (c.innerText || "").toLowerCase()).join(" ");

                const hasPeriodHeader = /period|tiết|tiet|buổi|buoi|\bca\b|slot/.test(firstHeader) || /period|tiết|tiet|buổi|buoi|\bca\b|slot/.test(allHeaders);
                const hasDayHeader = /day|thứ|thu|monday|tuesday|wednesday|thursday|friday|saturday|sunday|cn|chủ nhật|chu nhat/.test(allHeaders);

                if (hasPeriodHeader && hasDayHeader) {
                    target = tbl;
                    targetRows = rows;
                    break;
                }
            }

            if (!target) return null;

            const rows = targetRows;
            const headerCells = Array.from(rows[0].querySelectorAll(":scope > th, :scope > td"));
            if (headerCells.length < 3) return null;

            const dayByColumn = {};
            const dateByColumn = {};
            const maxDayCol = headerCells.length - 1;
            // Index 0 = Sunday … 6 = Saturday, matching JavaScript's Date.getUTCDay()
            const WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
            for (let col = 1; col <= maxDayCol; col += 1) {
                const headerText = headerCells[col]?.innerText || "";
                dayByColumn[col] = extractWeekday(headerText);
                // extractDate always returns "" or "YYYY-MM-DD" (ISO format)
                dateByColumn[col] = extractDate(headerText);
                // Derive day of week from the ISO date when the header has no weekday name
                if (!dayByColumn[col] && dateByColumn[col]) {
                    const d = new Date(dateByColumn[col] + "T00:00:00Z");
                    if (!isNaN(d.getTime())) {
                        dayByColumn[col] = WEEKDAY_NAMES[d.getUTCDay()];
                    }
                }
            }

            const carry = {};
            const entries = [];

            for (let r = 1; r < rows.length; r += 1) {
                for (const key of Object.keys(carry)) {
                    if (carry[key] > 0) carry[key] -= 1;
                }

                const cells = Array.from(rows[r].querySelectorAll(":scope > td, :scope > th"));
                if (!cells.length) continue;

                let logicalCol = 0;
                let rowPeriod = 0;

                for (const cell of cells) {
                    while (carry[logicalCol] > 0) {
                        logicalCol += 1;
                    }

                    const rowSpan = Math.max(parseInt(cell.getAttribute("rowspan") || "1", 10) || 1, 1);
                    const colSpan = Math.max(parseInt(cell.getAttribute("colspan") || "1", 10) || 1, 1);
                    const text = (cell.innerText || "").trim();

                    if (logicalCol === 0) {
                        // Match bare numbers ("1") or prefixed formats ("Tiết 1", "Ca 1", "Period 2")
                        const periodMatch = text.match(/^(?:tiết|tiet|ca\s*học|ca|period|slot)[.\s]*(\d+)$|^(\d+)$/i);
                        if (periodMatch) {
                            rowPeriod = parseInt(periodMatch[1] || periodMatch[2], 10);
                        }
                    } else if (rowPeriod > 0) {
                        for (let c = logicalCol; c < logicalCol + colSpan; c += 1) {
                            const dayOfWeek = dayByColumn[c] || "";
                            const sessionDate = dateByColumn[c] || "";
                            if (!dayOfWeek || !text) continue;
                            if (/^(-|x|trống|rong)$/i.test(text)) continue;

                            const entryTexts = splitCellEntries(cell) || [text];
                            for (const entryText of entryTexts) {
                                const subject = cleanSubject(entryText);
                                if (!subject) continue;
                                entries.push({
                                    subject_name: subject,
                                    room: extractRoom(entryText),
                                    day_of_week: dayOfWeek,
                                    session_date: sessionDate,
                                    start_period: rowPeriod,
                                    end_period: rowPeriod + rowSpan - 1,
                                    status: detectStatus(entryText),
                                });
                            }
                        }
                    }

                    if (rowSpan > 1) {
                        for (let c = logicalCol; c < logicalCol + colSpan; c += 1) {
                            carry[c] = Math.max(carry[c] || 0, rowSpan - 1);
                        }
                    }

                    logicalCol += colSpan;
                }
            }

            const seen = new Set();
            const deduped = [];
            for (const e of entries) {
                const key = [e.subject_name, e.room, e.day_of_week, e.session_date, e.start_period, e.end_period].join("|");
                if (seen.has(key)) continue;
                seen.add(key);
                deduped.push(e);
            }

            return deduped;
        }
        """

    contexts = [("main-page", page)]
    for index, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        contexts.append((f"frame-{index}", frame))

    raw_entries: list[dict] | None = None
    for context_name, context in contexts:
        try:
            candidate = context.evaluate(script)
        except Exception as exc:
            logger.debug("Skipping weekly-grid parse for %s: %s", context_name, exc)
            continue

        if candidate is None:
            # JS returned null: this context had no matching table structure.
            continue

        # JS returned a list (possibly empty): table structure was detected.
        raw_entries = candidate
        logger.info(
            "Detected weekly grid schedule in %s with %d raw entries.",
            context_name,
            len(raw_entries),
        )
        break

    if raw_entries is None:
        # No table structure was found in any frame.
        return None

    entries: list[dict] = []
    for row in raw_entries:
        entries.append(
            {
                "student_id": student_id,
                "subject_name": row.get("subject_name", ""),
                "room": row.get("room", ""),
                "day_of_week": row.get("day_of_week", ""),
                "session_date": row.get("session_date", ""),
                "start_period": int(row.get("start_period", 0) or 0),
                "end_period": int(row.get("end_period", 0) or 0),
                "status": row.get("status", "scheduled"),
            }
        )

    return entries


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
