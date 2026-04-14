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
"""

import logging
import os
import re
import datetime
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

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
    today = datetime.date.today()
    if today.month <= 7:
        hk_num = 2
        start_year = today.year - 1
        end_year = today.year
    else:
        hk_num = 1
        start_year = today.year
        end_year = today.year + 1

    default_target = f"hk{hk_num}/{start_year}-{end_year}".lower()
    for value, text in valid_options:
        normalized = text.lower().replace(" ", "")
        if default_target in normalized:
            logger.info("Auto-selected semester by date rule: %s", text)
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
    if weekly_entries:
        logger.info("Parsed %d entries from weekly grid table.", len(weekly_entries))
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


def _parse_weekly_grid_table(page, student_id: str) -> list[dict]:
        """Parse timetable from the week-view matrix layout (Period x Day)."""
        raw_entries: list[dict] = page.evaluate(
            r"""
                () => {
                    const dayNames = [
                        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
                    ];

                    const extractWeekday = (headerText) => {
                        const text = (headerText || "").replace(/\s+/g, " ");
                        for (const day of dayNames) {
                            if (text.includes(day)) return day;
                        }

                        // Fallback to Vietnamese labels when English is not present.
                        const vnMap = [
                            ["thứ 2", "Monday"],
                            ["thứ 3", "Tuesday"],
                            ["thứ 4", "Wednesday"],
                            ["thứ 5", "Thursday"],
                            ["thứ 6", "Friday"],
                            ["thứ 7", "Saturday"],
                            ["chủ nhật", "Sunday"]
                        ];
                        const lower = text.toLowerCase();
                        for (const [vn, en] of vnMap) {
                            if (lower.includes(vn)) return en;
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
                            || (text || "").match(/Phòng:\s*([^\n]+)/i);
                        return roomMatch ? roomMatch[1].trim() : "";
                    };

                    // Pick the main weekly table: first row should contain "Period | Day".
                    let target = null;
                    for (const tbl of Array.from(document.querySelectorAll("table"))) {
                        const firstRow = tbl.querySelector(":scope > tbody > tr, :scope > tr");
                        if (!firstRow) continue;
                        const firstText = (firstRow.innerText || "").toLowerCase();
                        if (firstText.includes("period") && firstText.includes("day")) {
                            target = tbl;
                            break;
                        }
                    }
                    if (!target) return [];

                    const rows = Array.from(target.querySelectorAll(":scope > tbody > tr, :scope > tr"));
                    if (rows.length < 2) return [];

                    const headerCells = Array.from(rows[0].querySelectorAll(":scope > th, :scope > td"));
                    if (headerCells.length < 8) return [];

                    // Logical day columns are expected at indices 1..7.
                    const dayByColumn = {};
                    for (let col = 1; col <= 7; col += 1) {
                        dayByColumn[col] = extractWeekday(headerCells[col]?.innerText || "");
                    }

                    const carry = {}; // col -> remaining rowspan from previous rows
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
                                const periodMatch = text.match(/^\d+$/);
                                if (periodMatch) {
                                    rowPeriod = parseInt(periodMatch[0], 10);
                                }
                            } else if (rowPeriod > 0) {
                                for (let c = logicalCol; c < logicalCol + colSpan; c += 1) {
                                    const dayOfWeek = dayByColumn[c] || "";
                                    if (!dayOfWeek || !text || !/room|phòng/i.test(text)) {
                                        continue;
                                    }

                                    const subject = cleanSubject(text);
                                    if (!subject) continue;

                                    entries.push({
                                        subject_name: subject,
                                        room: extractRoom(text),
                                        day_of_week: dayOfWeek,
                                        start_period: rowPeriod,
                                        end_period: rowPeriod + rowSpan - 1,
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

                    // De-duplicate by (subject, room, day, start, end)
                    const seen = new Set();
                    const deduped = [];
                    for (const e of entries) {
                        const key = [e.subject_name, e.room, e.day_of_week, e.start_period, e.end_period].join("|");
                        if (seen.has(key)) continue;
                        seen.add(key);
                        deduped.push(e);
                    }

                    return deduped;
                }
                """
        )

        entries: list[dict] = []
        for row in raw_entries or []:
                entries.append(
                        {
                                "student_id": student_id,
                                "subject_name": row.get("subject_name", ""),
                                "room": row.get("room", ""),
                                "day_of_week": row.get("day_of_week", ""),
                                "start_period": int(row.get("start_period", 0) or 0),
                                "end_period": int(row.get("end_period", 0) or 0),
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
