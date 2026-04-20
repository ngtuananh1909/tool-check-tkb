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


def _parse_schedule_table(page, student_id: str) -> list[dict]:
    """
    Locate the first <table> that looks like a schedule table and extract rows.

    The portal typically uses a table with columns similar to:
        STT | Môn học | Nhóm | Phòng | Thứ | Tiết bắt đầu | Tiết kết thúc | …

    Because the exact column order may vary, we detect column positions by
    inspecting the header row.
    """
    weekly_entries = _parse_weekly_grid_table(page, student_id)
    if weekly_entries is not None:
        # Grid table structure was detected (weekly_entries may be [] for an empty week).
        if weekly_entries:
            logger.info("Parsed %d entries from weekly grid table.", len(weekly_entries))
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
                    || (text || "").match(/Phòng:\s*([^\n]+)/i);
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

                            const subject = cleanSubject(text);
                            if (!subject) continue;

                            entries.push({
                                subject_name: subject,
                                room: extractRoom(text),
                                day_of_week: dayOfWeek,
                                session_date: sessionDate,
                                start_period: rowPeriod,
                                end_period: rowPeriod + rowSpan - 1,
                                status: detectStatus(text),
                            });
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
