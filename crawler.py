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
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from time_utils import local_today
from tdtu import (
    TDTUClient,
    fetch_schedule_http,
    fetch_exam_schedule_http,
    get_current_semester_http,
    TDTUError,
)

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

# Portal controls can be slow during peak registration hours.  Do not use the
# browser's short default timeout here: an incomplete control interaction can
# otherwise make a perfectly usable timetable look empty.
PORTAL_CONTROL_TIMEOUT_MS = 30_000
PORTAL_CONTROL_MAX_ATTEMPTS = 2
# Timeout (ms) for locating the submit button before falling back to Enter.
SUBMIT_BUTTON_TIMEOUT_MS = PORTAL_CONTROL_TIMEOUT_MS
ELEARNING_NAVIGATION_TIMEOUT_MS = 60_000
ELEARNING_LOGIN_TIMEOUT_MS = 30_000
ELEARNING_DASHBOARD_TIMEOUT_MS = 20_000
ELEARNING_SELECTOR_DASHBOARD_READY = (
    ".dashboard-card, .block_myoverview, [data-region='courses-view'], "
    "[data-region='timeline-view'], #region-main"
)

PLAYWRIGHT_BROWSER_INSTALL_COMMAND = "python -m playwright install chromium"
SENSITIVE_URL_QUERY_PARAMS = re.compile(r"([?&](?:token|requestid)=)[^&]+", re.IGNORECASE)


def _launch_chromium(playwright):
    """Launch the bundled Chromium with an actionable missing-browser error."""
    executable_path = Path(playwright.chromium.executable_path)
    if not executable_path.exists():
        raise RuntimeError(
            "Playwright Chromium is not installed for this Python environment. "
            f"Run `{PLAYWRIGHT_BROWSER_INSTALL_COMMAND}` and retry. "
            f"Expected executable: {executable_path}"
        )
    return playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-http2",
        ],
    )


def _sanitize_url_for_log(url: object) -> str:
    """Redact portal session query parameters before writing a URL to logs."""
    return SENSITIVE_URL_QUERY_PARAMS.sub(r"\1[redacted]", str(url or ""))


def _click_portal_control(page, control, description: str) -> None:
    """Click a portal control, retrying one transient Playwright timeout.

    The TDTU portal uses ASP.NET postbacks, which periodically leave an
    otherwise visible control non-actionable for a few seconds.  Retrying only
    timeout errors preserves genuine selector/permission failures while making
    the hourly crawl resilient to those temporary delays.
    """
    for attempt in range(1, PORTAL_CONTROL_MAX_ATTEMPTS + 1):
        try:
            control.click(timeout=PORTAL_CONTROL_TIMEOUT_MS)
            return
        except PlaywrightTimeoutError as exc:
            if attempt == PORTAL_CONTROL_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"{description} timed out after {attempt} attempt(s) of "
                    f"{PORTAL_CONTROL_TIMEOUT_MS}ms"
                ) from exc
            logger.warning(
                "%s timed out after %dms (attempt %d/%d); waiting for the page and retrying.",
                description,
                PORTAL_CONTROL_TIMEOUT_MS,
                attempt,
                PORTAL_CONTROL_MAX_ATTEMPTS,
            )
            try:
                page.wait_for_load_state("domcontentloaded", timeout=PORTAL_CONTROL_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                logger.debug("Page was still loading before retrying %s.", description)


def _login_and_open_elearning_dashboard(page, username: str, password: str) -> None:
    """Authenticate with eLearning and open its dashboard without waiting for network idle.

    Moodle keeps background requests alive on some deployments, so ``networkidle``
    can time out even after the page and its login form are usable.
    """
    logger.info("Navigating to eLearning login page: %s", ELEARNING_LOGIN_URL)
    page.goto(
        ELEARNING_LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=ELEARNING_NAVIGATION_TIMEOUT_MS,
    )
    page.wait_for_selector(
        ELEARNING_SELECTOR_USERNAME,
        state="visible",
        timeout=ELEARNING_LOGIN_TIMEOUT_MS,
    )
    page.fill(ELEARNING_SELECTOR_USERNAME, username)
    page.fill(ELEARNING_SELECTOR_PASSWORD, password)
    page.locator(ELEARNING_SELECTOR_SUBMIT).first.click(timeout=10_000)

    try:
        page.wait_for_url(
            lambda url: "login" not in str(url).lower(),
            timeout=ELEARNING_LOGIN_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        if "login" in page.url.lower():
            raise RuntimeError(
                "eLearning login did not leave the login page. "
                f"Current URL: {_sanitize_url_for_log(page.url)}"
            ) from exc
        raise

    if "login" in page.url.lower():
        raise RuntimeError(
            f"eLearning login failed. Current URL: {_sanitize_url_for_log(page.url)}"
        )

    page.goto(
        ELEARNING_MY_URL,
        wait_until="domcontentloaded",
        timeout=ELEARNING_NAVIGATION_TIMEOUT_MS,
    )
    if "login" in page.url.lower():
        raise RuntimeError(
            "eLearning dashboard redirected to the login page. "
            f"Current URL: {_sanitize_url_for_log(page.url)}"
        )
    try:
        page.wait_for_selector(
            ELEARNING_SELECTOR_DASHBOARD_READY,
            timeout=ELEARNING_DASHBOARD_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        logger.warning("eLearning dashboard selectors did not appear before timeout; parsing anyway.")


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
WEEKLY_SCHEDULE_RADIO_SELECTOR = "#ThoiKhoaBieu1_radXemTKBTheoTuan"
WEEKLY_SCHEDULE_LABEL_SELECTOR = "label[for='ThoiKhoaBieu1_radXemTKBTheoTuan']"
WEEKLY_SCHEDULE_TABLE_SELECTOR = "#ThoiKhoaBieu1_tbTKBTheoTuan"

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

    extra_weeks = _resolve_weeks_ahead(weeks_ahead)
    total_weeks = 1 + extra_weeks

    http_required = os.environ.get("TDTU_HTTP_REQUIRED", "").lower() in ("true", "1", "yes")

    # --- PRIMARY: Authenticated HTTP Crawler ---
    try:
        with TDTUClient(sid, pwd) as client:
            http_schedule = fetch_schedule_http(client, max_weeks=total_weeks)
            logger.info("[crawler] HTTP fetch_schedule succeeded with %d rows", len(http_schedule))
            return http_schedule
    except Exception as exc:
        if http_required:
            logger.error("[crawler] HTTP fetch_schedule failed while TDTU_HTTP_REQUIRED=true: %s", _sanitize_url_for_log(exc))
            raise RuntimeError(f"TDTU HTTP schedule fetch failed while TDTU_HTTP_REQUIRED=true: {_sanitize_url_for_log(exc)}") from exc
        logger.warning("[crawler] HTTP fetch_schedule failed (%s), falling back to Playwright", _sanitize_url_for_log(exc))

    return _fetch_schedule_playwright(sid, pwd, weeks_ahead=weeks_ahead)


def _fetch_schedule_playwright(sid: str, pwd: str, weeks_ahead: int | None = None) -> list[dict]:
    """Execute Playwright schedule crawl strictly (no HTTP attempt)."""
    schedule: list[dict] = []
    extra_weeks = _resolve_weeks_ahead(weeks_ahead)
    total_weeks = 1 + extra_weeks

    with sync_playwright() as p:
        browser = _launch_chromium(p)
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
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)

            # ----------------------------------------------------------------
            # Step 2 – Fill in credentials and submit
            # ----------------------------------------------------------------
            # Wait for the username input to be ready before filling.
            page.wait_for_selector(SELECTOR_USERNAME, state="visible", timeout=30_000)
            logger.info("Filling in portal login credentials.")
            page.fill(SELECTOR_USERNAME, sid)
            page.fill(SELECTOR_PASSWORD, pwd)

            # Record the login page URL *after* any redirect so we can
            # detect a failed login (i.e., we were returned to this URL).
            login_page_url = page.url

            # ── Submission strategy 1: click the submit button ──────────────
            submit_clicked = False
            try:
                _click_portal_control(page, page.locator(SELECTOR_SUBMIT).first, "Portal login submit")
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
                    timeout=60_000,
                )
            except PlaywrightTimeoutError:
                # URL did not change within the timeout window.
                # Fall through to the explicit failure check below.
                logger.warning(
                    "wait_for_url timed out – login may have failed or navigation was delayed."
                )

            # Ensure the page is fully loaded after the URL change.
            # Use domcontentloaded – networkidle never settles on TDTU portal
            # due to persistent analytics/tracking connections.
            page.wait_for_load_state("domcontentloaded", timeout=60_000)

            # Basic check – if we're still on the login page, fail loudly.
            if page.url == login_page_url or "login" in page.url.lower():
                # Try to grab an error message from the page for better diagnostics
                error_text = page.text_content("body") or ""
                raise RuntimeError(
                    f"Login appears to have failed. Current URL: {_sanitize_url_for_log(page.url)}. "
                    f"Page excerpt: {error_text[:500]}"
                )

            logger.info("Login successful. Current URL: %s", _sanitize_url_for_log(page.url))

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
                        _click_portal_control(page, candidate, "Schedule navigation link")
                        page.wait_for_load_state("domcontentloaded", timeout=60_000)
                        clicked_schedule_link = True
                        break
                    except Exception as exc:
                        logger.debug("Skipping schedule link candidate %d: %s", i + 1, exc)

            if not clicked_schedule_link:
                # Fallback: navigate directly to the schedule URL
                logger.info(
                    "No visible schedule link found; navigating directly to %s",
                    _sanitize_url_for_log(schedule_url),
                )
                page.goto(schedule_url, wait_until="domcontentloaded", timeout=60_000)

            # ----------------------------------------------------------------
            # Step 4 – Parse the schedule table
            # ----------------------------------------------------------------
            logger.info("Configuring schedule filters (semester + weekly view) when available")
            _configure_schedule_filters(page)

            semester_label = _get_selected_semester_text(page)
            logger.info("Crawling schedule for semester: %s", semester_label)
            logger.info(
                "Parsing schedule table on %s (current week + %d future week(s)).",
                _sanitize_url_for_log(page.url),
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
                logger.debug("=== FINAL DEDUPLICATED SCHEDULE ===")
                for i, entry in enumerate(schedule):
                    logger.debug(
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
                logger.debug("=== END FINAL SCHEDULE ===")

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
    """Deduplicate rows across all crawled weeks while preserving first-seen order.

    Keep `status` in the signature so paired rows such as "absent" and
    "makeup" are not collapsed into a single record when they share the same
    subject, room, day, date, and period range.
    """
    deduped: list[dict] = []
    seen: set[tuple[str, str, str, str, int, int, str]] = set()

    for row in rows:
        signature = (
            str(row.get("subject_name") or "").strip().lower(),
            str(row.get("room") or "").strip().lower(),
            str(row.get("day_of_week") or "").strip().lower(),
            str(row.get("session_date") or "").strip(),
            int(row.get("start_period", 0) or 0),
            int(row.get("end_period", 0) or 0),
            str(row.get("status") or "").strip().lower(),
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
    http_required = os.environ.get("TDTU_HTTP_REQUIRED", "").lower() in ("true", "1", "yes")

    # --- PRIMARY: Authenticated HTTP Exam Crawler ---
    try:
        with TDTUClient(sid, pwd) as client:
            http_exams = fetch_exam_schedule_http(client)
            logger.info("[crawler] HTTP fetch_exam_schedule succeeded with %d rows", len(http_exams))
            return http_exams
    except Exception as exc:
        if http_required:
            logger.error("[crawler] HTTP fetch_exam_schedule failed while TDTU_HTTP_REQUIRED=true: %s", _sanitize_url_for_log(exc))
            raise RuntimeError(f"TDTU HTTP exam fetch failed while TDTU_HTTP_REQUIRED=true: {_sanitize_url_for_log(exc)}") from exc
        logger.warning("[crawler] HTTP fetch_exam_schedule failed (%s), falling back to Playwright", _sanitize_url_for_log(exc))

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
        browser = _launch_chromium(p)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            _login_and_open_elearning_dashboard(page, user, pwd)

            progress_rows = _parse_elearning_progress(page)
            deduped = _deduplicate_progress_rows(progress_rows)
            if not deduped:
                logger.warning(
                    "eLearning progress parser returned 0 rows. url=%s title=%s",
                    _sanitize_url_for_log(page.url),
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
        browser = _launch_chromium(p)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector(SELECTOR_USERNAME, state="visible", timeout=30_000)
            page.fill(SELECTOR_USERNAME, sid)
            page.fill(SELECTOR_PASSWORD, pwd)

            login_page_url = page.url
            try:
                _click_portal_control(page, page.locator(SELECTOR_SUBMIT).first, "Portal exam login submit")
            except Exception:
                page.locator(SELECTOR_PASSWORD).press("Enter")

            try:
                page.wait_for_url(lambda url: "login" not in str(url).lower(), timeout=30_000)
            except PlaywrightTimeoutError:
                logger.warning("Portal exam login wait_for_url timed out.")

            page.wait_for_load_state("domcontentloaded", timeout=30_000)
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
                        _click_portal_control(page, candidate, "Exam navigation link")
                        page.wait_for_load_state("domcontentloaded", timeout=60_000)
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
                        _click_portal_control(page, candidate, "Schedule navigation link")
                        page.wait_for_load_state("domcontentloaded", timeout=60_000)
                        clicked_schedule_link = True
                        break
                    except Exception:
                        continue

            if not clicked_schedule_link:
                schedule_url = _build_schedule_url(page.url)
                page.goto(schedule_url, wait_until="domcontentloaded", timeout=60_000)

            exam_tab = page.locator("a, button, input[type='button'], input[type='submit']").filter(
                has_text=re.compile(r"lịch\s*thi|lich\s*thi|exam", re.IGNORECASE)
            )
            if exam_tab.count() > 0:
                for i in range(exam_tab.count()):
                    candidate = exam_tab.nth(i)
                    try:
                        if not candidate.is_visible():
                            continue
                        _click_portal_control(page, candidate, "Exam tab")
                        page.wait_for_load_state("domcontentloaded", timeout=30_000)
                        exams = _parse_exam_table_with_filters(page)
                        if exams:
                            return exams
                    except Exception:
                        continue

            # Strategy 3: final fallback to direct exam URL built from any available token.
            exam_url = _build_exam_url(page.url)
            page.goto(exam_url, wait_until="domcontentloaded", timeout=60_000)
            exams = _parse_exam_table_with_filters(page)
            return exams
        finally:
            context.close()
            browser.close()


def _fetch_exam_schedule_from_elearning(username: str, password: str) -> list[dict]:
    """Best-effort exam parsing from eLearning pages when portal source is unavailable."""
    with sync_playwright() as p:
        browser = _launch_chromium(p)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            _login_and_open_elearning_dashboard(page, username, password)
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
        browser = _launch_chromium(p)
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
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector(SELECTOR_USERNAME, state="visible", timeout=30_000)
            page.fill(SELECTOR_USERNAME, username)
            page.fill(SELECTOR_PASSWORD, password)
            try:
                _click_portal_control(page, page.locator(SELECTOR_SUBMIT).first, "Portal announcement login submit")
            except Exception:
                page.locator(SELECTOR_PASSWORD).press("Enter")
            try:
                page.wait_for_url(lambda url: "login" not in str(url).lower(), timeout=30_000)
            except Exception:
                pass

            # Two-step navigation consistently lands on authenticated stdportal home.
            page.goto(stdportal_home, wait_until="domcontentloaded", timeout=60_000)
            page.goto(stdportal_login_home, wait_until="domcontentloaded", timeout=60_000)

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

    semester_label = _get_selected_semester_text(page)
    logger.info("Crawling exam schedule for semester: %s", semester_label)

    button_targets = _resolve_exam_type_button_targets(page)
    if button_targets:
        combined_rows: list[dict] = []
        for target in button_targets:
            changed = _click_exam_type_by_group(page, target["group"])
            if changed or semester_changed:
                page.wait_for_load_state("domcontentloaded", timeout=30_000)

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
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
        _scroll_exam_page_to_bottom(page)
        return _parse_exam_table(page)

    combined_rows: list[dict] = []
    for target in type_targets:
        changed = _select_exam_type_by_value(page, target["value"])
        if changed or semester_changed:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)

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
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
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
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
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


def fetch_elearning_deadlines(
    username: str | None = None,
    password: str | None = None,
) -> list[dict]:
    """Login to eLearning and return nearest incomplete deadline per course."""
    user = username or os.environ.get("STUDENT_ID")
    pwd = password or os.environ.get("PASSWORD")
    if not user or not pwd:
        raise ValueError("eLearning credentials missing. Set STUDENT_ID and PASSWORD.")

    with sync_playwright() as p:
        browser = _launch_chromium(p)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            _login_and_open_elearning_dashboard(page, user, pwd)
            dashboard_deadlines = _parse_elearning_dashboard_deadlines(page)
            courses = _parse_elearning_courses(page)
            return _deduplicate_elearning_deadlines(
                dashboard_deadlines + _collect_elearning_course_deadlines(page, courses)
            )
        finally:
            context.close()
            browser.close()


def _collect_elearning_course_deadlines(page, courses: list[dict], parse_course=None) -> list[dict]:
    if parse_course is None:
        parse_course = _parse_elearning_deadlines_on_course
    deadlines: list[dict] = []
    for course in courses:
        url = course.get("course_url")
        if not url:
            continue
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            logger.warning("Skipped eLearning course after navigation timeout: %s (%s)", course.get("course_name"), url)
            continue
        for row in parse_course(page, course):
            deadlines.append(row)
    return deadlines


def _deduplicate_elearning_deadlines(rows: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for row in rows:
        activity_url = str(row.get("activity_url") or "").strip()
        key = activity_url or "|".join(
            str(row.get(field) or "").strip()
            for field in ("course_id", "activity_name", "due_date")
        )
        if not key:
            continue
        current = deduped.get(key)
        if current is None:
            deduped[key] = row
            continue
        current_name = str(current.get("course_name") or "").strip().lower()
        candidate_name = str(row.get("course_name") or "").strip().lower()
        if current_name in {"", "image"} and candidate_name not in {"", "image"}:
            deduped[key] = row
    return sorted(deduped.values(), key=lambda item: str(item.get("due_date") or ""))


def _parse_elearning_dashboard_deadlines(page) -> list[dict]:
    raw_rows = page.evaluate(
        r'''
        () => {
            const rows = [];
            const containers = Array.from(document.querySelectorAll('[data-event-id], .event, .timeline-event, .block-timeline .list-group-item, [data-region="event-list-item"]'));
            for (const item of containers) {
                const text = (item.innerText || item.textContent || '').trim();
                const activityLink = item.querySelector('a[href*="/mod/"]');
                if (!activityLink) continue;

                const courseLink = item.querySelector('a[href*="/course/view.php"]');
                const timeNode = item.querySelector('time[datetime]');
                const dueTextMatch = text.match(/Due date\s*:?\s*([^\n]+)/i) || text.match(/Due\s*:?\s*([^\n]+)/i);
                rows.push({
                    course_id: courseLink ? new URL(courseLink.href, window.location.href).searchParams.get('id') || '' : '',
                    course_name: courseLink ? (courseLink.innerText || courseLink.textContent || '').trim() : '',
                    activity_name: (activityLink.innerText || activityLink.textContent || '').trim(),
                    activity_url: activityLink.href,
                    due_text: timeNode ? timeNode.getAttribute('datetime') || '' : (dueTextMatch ? dueTextMatch[1].trim() : '')
                });
            }
            return rows;
        }
        '''
    )
    deadlines: list[dict] = []
    seen_urls: set[str] = set()
    for raw in raw_rows:
        activity_url = str(raw.get("activity_url") or "").strip()
        activity_name = str(raw.get("activity_name") or "").strip()
        due_text = str(raw.get("due_text") or "").strip()
        if not activity_url or activity_url in seen_urls:
            continue
        due_date = _parse_elearning_due_date(due_text)
        if not due_date:
            continue
        seen_urls.add(activity_url)
        course_name = _clean_course_name(str(raw.get("course_name") or "").strip())
        if not course_name:
            course_name = "eLearning"
        course_id = str(raw.get("course_id") or "").strip()
        if not course_id:
            course_id = hashlib.sha256(course_name.lower().encode("utf-8")).hexdigest()[:16]
        deadlines.append(
            {
                "course_id": course_id,
                "course_name": course_name,
                "activity_name": activity_name,
                "due_date": due_date.isoformat(),
                "activity_url": activity_url,
                "completion_status": "incomplete",
            }
        )
    return deadlines


def _parse_elearning_courses(page) -> list[dict]:
    return page.evaluate(
        r'''
        () => Array.from(document.querySelectorAll('a[href*="/course/view.php"]'))
            .map((a) => ({
                course_url: a.href,
                course_name: (a.innerText || a.textContent || '').trim(),
                course_id: new URL(a.href, window.location.href).searchParams.get('id') || ''
            }))
            .filter((row) => row.course_url && row.course_name)
        '''
    )


def _parse_elearning_deadlines_on_course(page, course: dict) -> list[dict]:
    raw_rows = page.evaluate(
        r'''
        () => {
            const rows = [];
            const icons = Array.from(document.querySelectorAll('img[alt*="Not completed"], img[title*="Not completed"], img[src*="completion-manual-n"]'));
            for (const icon of icons) {
                const label = icon.getAttribute('alt') || icon.getAttribute('title') || '';
                const match = label.match(/Not completed:\s*(.+?)(?:\.\s*Select|$)/i);
                const activityName = match ? match[1].trim() : '';
                const activity = icon.closest('li.activity, div.activity, div.activity-item, tr, .modtype_assign, .modtype_quiz') || icon.parentElement;
                const link = activity ? activity.querySelector('a[href*="/mod/"]') : null;
                rows.push({ activity_name: activityName, activity_url: link ? link.href : '' });
            }
            return rows;
        }
        '''
    )
    deadlines: list[dict] = []
    seen_urls: set[str] = set()
    for raw in raw_rows:
        activity_url = str(raw.get("activity_url") or "").strip()
        activity_name = str(raw.get("activity_name") or "").strip()
        if not activity_url or activity_url in seen_urls:
            continue
        seen_urls.add(activity_url)
        try:
            page.goto(activity_url, wait_until="domcontentloaded", timeout=60_000)
            due_text = _extract_elearning_due_date_text(page)
        except Exception as exc:
            logger.debug("Could not inspect deadline activity %s: %s", activity_url, exc)
            continue
        due_date = _parse_elearning_due_date(due_text)
        if not due_date:
            continue
        deadlines.append(
            {
                "course_id": str(course.get("course_id") or "").strip(),
                "course_name": _clean_course_name(str(course.get("course_name") or "")),
                "activity_name": activity_name or page.title(),
                "due_date": due_date.isoformat(),
                "activity_url": activity_url,
                "completion_status": "incomplete",
            }
        )
    return deadlines


def _extract_elearning_due_date_text(page) -> str:
    return page.evaluate(
        r'''
        () => {
            const cells = Array.from(document.querySelectorAll('th, td, div, span'));
            for (const cell of cells) {
                const text = (cell.innerText || cell.textContent || '').trim();
                if (/^Due date$/i.test(text)) {
                    const next = cell.nextElementSibling;
                    if (next) return (next.innerText || next.textContent || '').trim();
                    const row = cell.closest('tr');
                    if (row) {
                        const rowCells = Array.from(row.querySelectorAll('th, td'));
                        const idx = rowCells.indexOf(cell);
                        if (idx >= 0 && rowCells[idx + 1]) return (rowCells[idx + 1].innerText || '').trim();
                    }
                }
            }
            const body = document.body ? document.body.innerText : '';
            const match = body.match(/Due date\s*\n\s*([^\n]+)/i);
            return match ? match[1].trim() : '';
        }
        '''
    )


def _parse_elearning_due_date(text: str) -> datetime.datetime | None:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    if not value:
        return None
    try:
        parsed_iso = datetime.datetime.fromisoformat(value)
        if parsed_iso.tzinfo is None:
            parsed_iso = parsed_iso.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=7)))
        return parsed_iso
    except ValueError:
        pass
    value = re.sub(r"^[A-Za-z]+,\s*", "", value)
    for fmt in ("%d %B %Y, %I:%M %p", "%d %b %Y, %I:%M %p", "%d/%m/%Y, %H:%M", "%d/%m/%Y %H:%M"):
        try:
            parsed = datetime.datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=7)))
        except ValueError:
            continue
    return None


def _nearest_deadline_per_course(rows: list[dict]) -> list[dict]:
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    nearest: dict[str, dict] = {}
    for row in rows:
        try:
            due_date = datetime.datetime.fromisoformat(str(row.get("due_date")))
        except ValueError:
            continue
        if due_date < now:
            continue
        key = str(row.get("course_id") or row.get("course_name") or "").strip()
        current = nearest.get(key)
        if current is None or due_date.isoformat() < str(current.get("due_date")):
            nearest[key] = row
    return sorted(nearest.values(), key=lambda item: str(item.get("due_date")))


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
                _click_portal_control(page, control, "Next-week control")
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
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1_200)
                after = _capture_week_signature(page)
                if after != before:
                    logger.info("Moved to next week using selector: %s", selector)
                    return True
            except Exception:
                continue

    return False


def _configure_schedule_filters(page) -> None:
    """Select semester, select weekly view, then wait for its grid."""
    _select_semester_if_available(page)
    _switch_to_week_view_if_available(page)
    try:
        page.wait_for_selector(
            WEEKLY_SCHEDULE_TABLE_SELECTOR,
            state="visible",
            timeout=30_000,
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"Weekly schedule table {WEEKLY_SCHEDULE_TABLE_SELECTOR} did not become visible."
        ) from exc


def get_current_semester(student_id: str | None = None, password: str | None = None) -> str:
    """Login to portal, navigate to schedule page, and return the selected semester text."""
    sid = student_id or os.environ.get("STUDENT_ID")
    pwd = password or os.environ.get("PASSWORD")
    if not sid or not pwd:
        return "unknown"

    http_required = os.environ.get("TDTU_HTTP_REQUIRED", "").lower() in ("true", "1", "yes")

    # --- PRIMARY: Authenticated HTTP ---
    try:
        with TDTUClient(sid, pwd) as client:
            sem = get_current_semester_http(client)
            if sem:
                logger.info("[crawler] HTTP get_current_semester resolved: %s", sem)
                return sem
    except Exception as exc:
        sanitized_exc = _sanitize_url_for_log(exc)
        if http_required:
            logger.error("[crawler] HTTP get_current_semester failed while TDTU_HTTP_REQUIRED=true: %s", sanitized_exc)
            raise RuntimeError(f"TDTU HTTP semester fetch failed while TDTU_HTTP_REQUIRED=true: {sanitized_exc}") from exc
        logger.warning("[crawler] HTTP get_current_semester failed (%s), falling back to Playwright", sanitized_exc)

    return _get_current_semester_playwright(sid, pwd)


def _get_current_semester_playwright(sid: str, pwd: str) -> str:
    """Execute Playwright semester pre-check strictly (no HTTP attempt)."""
    with sync_playwright() as p:
        browser = _launch_chromium(p)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector(SELECTOR_USERNAME, state="visible", timeout=30_000)
            page.fill(SELECTOR_USERNAME, sid)
            page.fill(SELECTOR_PASSWORD, pwd)

            submitted = False
            try:
                _click_portal_control(page, page.locator(SELECTOR_SUBMIT).first, "Semester pre-check login submit")
                submitted = True
            except Exception:
                try:
                    page.locator(SELECTOR_PASSWORD).press("Enter")
                    submitted = True
                except Exception:
                    page.evaluate(
                        "const f = document.querySelector('form'); if (f) f.submit();"
                    )
                    submitted = True

            if submitted:
                try:
                    page.wait_for_url(
                        lambda url: "login" not in str(url).lower(), timeout=30_000
                    )
                except PlaywrightTimeoutError:
                    pass
            page.wait_for_load_state("domcontentloaded", timeout=30_000)

            page.goto(SCHEDULE_URL_BASE, wait_until="domcontentloaded", timeout=60_000)
            _select_semester_if_available(page)
            return _get_selected_semester_text(page)
        except Exception:
            return "unknown"
        finally:
            context.close()
            browser.close()


def _get_selected_semester_text(page) -> str:
    """Return the text of the currently selected semester option, or 'unknown' if not found."""
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
            if not current_value:
                continue
            for opt in options:
                value = (opt.get_attribute("value") or "").strip()
                if value == current_value:
                    return opt.inner_text().strip()
            return current_value
        except Exception:
            continue
    return "unknown"


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
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
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

    # 2) Date-based default:
    #    Jan-May  -> HK2/(year-1)-year
    #    Jun-Jul  -> HK Hè/(year-1)-year  (summer semester: June-July)
    #    Aug-Dec  -> HK1/year-(year+1)
    today = local_today()
    if today.month <= 5:
        hk_num = 2
        start_year = today.year - 1
        end_year = today.year
        hk_label = f"hk{hk_num}"
    elif today.month <= 7:
        hk_num = 0  # Hè has no number; matched by keyword below
        start_year = today.year - 1
        end_year = today.year
        hk_label = "hè"
    else:
        hk_num = 1
        start_year = today.year
        end_year = today.year + 1
        hk_label = f"hk{hk_num}"

    # Match flexibly: strip all separators/spaces from both the target and the option
    # so that "HK2/2025-2026", "HK2 2025-2026", "HK2-2025-2026" are all equivalent.
    def _sem_key(s: str) -> str:
        return re.sub(r'[\s/\-]+', '', s.lower())

    if hk_label == "hè":
        # Summer semester (HK Hè): match keyword "hè" + correct year range
        year_re = re.compile(rf'{start_year}\s*[-/]\s*{end_year}', re.IGNORECASE)
        he_re = re.compile(r'h[eèê]\s*/?\s*$|h[eèê]\s*/?\s*{start_year}', re.IGNORECASE)
        for value, text in valid_options:
            if year_re.search(text) and he_re.search(text):
                logger.info("Auto-selected summer semester by date rule: %s", text)
                return value, text
    else:
        default_key = _sem_key(f"{hk_label}{start_year}{end_year}")
        for value, text in valid_options:
            if default_key in _sem_key(text):
                logger.info("Auto-selected semester by date rule: %s", text)
                return value, text

    # Also try matching on just the year range + semester number with common Vietnamese prefixes
    # Normalize away spaces around separators for the year-range check.
    year_re = re.compile(rf'{start_year}\s*[-/]\s*{end_year}')
    # k[yỳiì]: matches "ky" (unaccented), "kỳ" (grave), "ki", "kì" – Vietnamese romanisations of "kỳ"
    if hk_label == "hè":
        sem_re = re.compile(r'h[eèê]', re.IGNORECASE)
    else:
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
    """Click TDTU's exact weekly-schedule label when weekly grid is absent."""
    weekly_table = page.locator(WEEKLY_SCHEDULE_TABLE_SELECTOR)
    if weekly_table.count() > 0 and weekly_table.first.is_visible():
        return False

    radio = page.locator(WEEKLY_SCHEDULE_RADIO_SELECTOR)
    label = page.locator(WEEKLY_SCHEDULE_LABEL_SELECTOR)
    if radio.count() == 0 or label.count() == 0:
        raise RuntimeError("TDTU weekly-schedule radio or label was not found.")
    if not label.first.is_visible():
        raise RuntimeError("TDTU weekly-schedule label is not visible.")

    was_checked = radio.first.is_checked()
    _click_portal_control(page, label.first, "Weekly-schedule label")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        pass
    logger.info("Clicked TDTU weekly-schedule label (was_checked=%s).", was_checked)
    return not was_checked


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
        logger.debug("=== RAW TABLE TD DUMP: %d non-empty <td> cells found ===", len(tds))
        for item in tds:
            logger.debug(
                "  doc[%d] table[%d] row[%d] col[%d]: %s",
                item.get("doc", 0),
                item.get("table", -1),
                item.get("row", -1),
                item.get("col", -1),
                item.get("text", ""),
            )
        logger.debug("=== END RAW TABLE TD DUMP ===")
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
            logger.debug("=== WEEKLY GRID ENTRIES ===")
            for i, entry in enumerate(weekly_entries):
                logger.debug(
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
            logger.debug("=== END WEEKLY GRID ENTRIES ===")
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
            logger.debug("=== COLUMN-BASED TABLE ENTRIES ===")
            logger.debug("  Headers: %s", headers_raw)
            logger.debug("  Column mapping: %s", col)
            for i, entry in enumerate(entries):
                logger.debug(
                    "  [%d] subject=%r room=%r day=%r period=%s-%s",
                    i + 1,
                    entry.get("subject_name"),
                    entry.get("room"),
                    entry.get("day_of_week"),
                    entry.get("start_period"),
                    entry.get("end_period"),
                )
            logger.debug("=== END COLUMN-BASED TABLE ENTRIES ===")
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

            const toIsoDate = (day, month, year) => {
                if (!day || !month || !year) return "";
                const date = new Date(Date.UTC(year, month - 1, day));
                if (
                    date.getUTCFullYear() !== year
                    || date.getUTCMonth() !== month - 1
                    || date.getUTCDate() !== day
                ) return "";
                return `${year.toString().padStart(4, "0")}-${month.toString().padStart(2, "0")}-${day.toString().padStart(2, "0")}`;
            };

            const extractWeekRange = () => {
                const value = document.querySelector("#ThoiKhoaBieu1_btnTuanHienTai")?.value || "";
                const matches = Array.from(value.matchAll(/(\d{1,2})\/(\d{1,2})\/(\d{2,4})/g));
                if (matches.length < 2) return null;
                const dates = matches.slice(0, 2).map((match) => {
                    let year = parseInt(match[3], 10);
                    if (year < 100) year += 2000;
                    return toIsoDate(parseInt(match[1], 10), parseInt(match[2], 10), year);
                });
                return dates[0] && dates[1] ? { start: dates[0], end: dates[1] } : null;
            };

            const extractDate = (headerText, weekRange) => {
                const text = (headerText || "").replace(/\s+/g, " ");
                const m = text.match(/(\d{1,2})[\/.\-](\d{1,2})(?:[\/.\-](\d{2,4}))?/);
                if (!m) return "";

                const day = parseInt(m[1], 10);
                const month = parseInt(m[2], 10);
                if (m[3]) {
                    let year = parseInt(m[3], 10);
                    if (year < 100) year += 2000;
                    return toIsoDate(day, month, year);
                }
                if (!weekRange) return "";

                const years = new Set([
                    parseInt(weekRange.start.slice(0, 4), 10),
                    parseInt(weekRange.end.slice(0, 4), 10),
                ]);
                for (const year of years) {
                    const candidate = toIsoDate(day, month, year);
                    if (candidate && candidate >= weekRange.start && candidate <= weekRange.end) {
                        return candidate;
                    }
                }
                return "";
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
                return roomMatch ? roomMatch[1].trim().replace(/\s*\)$/, "").trim() : "";
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

            const extractPeriodRange = (text, fallbackStart, fallbackEnd) => {
                const fallback = {
                    start: Number.isFinite(fallbackStart) ? fallbackStart : 0,
                    end: Number.isFinite(fallbackEnd) ? fallbackEnd : 0,
                };
                const source = String(text || "");
                const markerMatch = source.match(/(?:tiết\|period|period|tiết)\s*:\s*([0-9,\-;\s]+)/i);
                if (!markerMatch) return fallback;
                const candidate = markerMatch[1];
                const digits = candidate.match(/\d+/g) || [];
                if (!digits.length) return fallback;

                const compact = candidate.replace(/\s+/g, "");
                if (/^\d{2,}$/.test(compact)) {
                    const chars = compact.split("").map((d) => parseInt(d, 10)).filter((n) => Number.isFinite(n));
                    if (!chars.length) return fallback;
                    return { start: Math.min(...chars), end: Math.max(...chars) };
                }

                const nums = digits.map((d) => parseInt(d, 10)).filter((n) => Number.isFinite(n));
                if (!nums.length) return fallback;
                return { start: Math.min(...nums), end: Math.max(...nums) };
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
                // If there's an inner table, prefer splitting by its inner <td>
                // cells (covers the nested-table layout in the portal).
                const innerTable = cell.querySelector("table");
                if (innerTable) {
                    const innerTds = Array.from(innerTable.querySelectorAll("td"));
                    if (innerTds.length > 1) {
                        return innerTds
                            .map((td) => (td.innerText || "").trim())
                            .filter((t) => t.length > 0);
                    }
                }

                // Fallback: split by bold subject markers among direct children.
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
                const hasSessionRows = rows.slice(1).some((row) => {
                    const firstCell = row.querySelector(":scope > td, :scope > th");
                    const value = (firstCell?.innerText || "").toLowerCase();
                    return /morning|afternoon|evening|sáng|chieu|chiều|tối|toi/.test(value);
                });

                if (hasDayHeader && (hasPeriodHeader || hasSessionRows)) {
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
            const weekRange = extractWeekRange();
            const maxDayCol = headerCells.length - 1;
            // Index 0 = Sunday … 6 = Saturday, matching JavaScript's Date.getUTCDay()
            const WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
            for (let col = 1; col <= maxDayCol; col += 1) {
                const headerText = headerCells[col]?.innerText || "";
                dayByColumn[col] = extractWeekday(headerText);
                // extractDate always returns "" or "YYYY-MM-DD" (ISO format)
                dateByColumn[col] = extractDate(headerText, weekRange);
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
                    } else {
                        for (let c = logicalCol; c < logicalCol + colSpan; c += 1) {
                            const dayOfWeek = dayByColumn[c] || "";
                            const sessionDate = dateByColumn[c] || "";
                            if (!dayOfWeek || !text) continue;
                            if (/^(-|x|trống|rong)$/i.test(text)) continue;

                            const entryTexts = splitCellEntries(cell) || [text];
                            for (const entryText of entryTexts) {
                                const subject = cleanSubject(entryText);
                                if (!subject) continue;
                                const parsedPeriod = extractPeriodRange(
                                    entryText,
                                    rowPeriod,
                                    rowPeriod > 0 ? rowPeriod + rowSpan - 1 : 0,
                                );
                                if (!(parsedPeriod.start > 0 && parsedPeriod.end >= parsedPeriod.start)) continue;
                                entries.push({
                                    subject_name: subject,
                                    room: extractRoom(entryText),
                                    day_of_week: dayOfWeek,
                                    session_date: sessionDate,
                                    start_period: parsedPeriod.start,
                                    end_period: parsedPeriod.end,
                                    status: detectStatus(entryText),
                                });
                            }
                        }
                    }

                    if (rowSpan > 1) {
                        for (let c = logicalCol; c < logicalCol + colSpan; c += 1) {
                            carry[c] = Math.max(carry[c] || 0, rowSpan);
                        }
                    }

                    logicalCol += colSpan;
                }

                for (const key of Object.keys(carry)) {
                    if (carry[key] > 0) carry[key] -= 1;
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
