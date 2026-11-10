"""Vietnamese natural language appointment parser using regular expressions.

Zero-dependency, zero-latency extraction of dates, times, durations,
reminders, locations, and appointment titles from free-form Vietnamese text.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import NamedTuple

from time_utils import local_today

DEFAULT_REMINDER_MINUTES = 60
DEFAULT_DURATION_MINUTES = 60

# Weekday name mapping (Monday=0 ... Sunday=6)
WEEKDAY_MAP = {
    "2": 0, "hai": 0, "t2": 0, "thu 2": 0, "thứ 2": 0,
    "3": 1, "ba": 1, "t3": 1, "thu 3": 1, "thứ 3": 1,
    "4": 2, "bốn": 2, "bon": 2, "tu": 2, "tư": 2, "t4": 2, "thu 4": 2, "thứ 4": 2,
    "5": 3, "năm": 3, "nam": 3, "t5": 3, "thu 5": 3, "thứ 5": 3,
    "6": 4, "sáu": 4, "sau": 4, "t6": 4, "thu 6": 4, "thứ 6": 4,
    "7": 5, "bảy": 5, "bay": 5, "t7": 5, "thu 7": 5, "thứ 7": 5,
    "cn": 6, "chủ nhật": 6, "chu nhat": 6,
}


@dataclass(frozen=True)
class QuickAddResult:
    title: str | None
    appointment_date: dt.date | None
    start_time: str | None  # HH:MM
    end_time: str | None    # HH:MM
    duration_minutes: int = DEFAULT_DURATION_MINUTES
    location: str | None = None
    reminder_minutes: int = DEFAULT_REMINDER_MINUTES
    missing_fields: tuple[str, ...] = ()
    raw_input: str = ""

    @property
    def is_complete(self) -> bool:
        return bool(self.title and self.appointment_date and self.start_time)


def _parse_time_str_parts(time_str: str) -> tuple[int, int]:
    clean = str(time_str or "").strip()
    if ":" in clean:
        parts = clean.split(":")
        return int(parts[0]), int(parts[1])
    if "h" in clean.lower():
        parts = clean.lower().split("h")
        return int(parts[0]), int(parts[1]) if parts[1] else 0
    return int(clean), 0


def _add_minutes_to_time(time_str: str, minutes: int) -> str:
    """Add minutes to HH:MM or HHhMM time string, wrapping at 23:59."""
    hour, minute = _parse_time_str_parts(time_str)
    total_minutes = (hour * 60 + minute + minutes) % (24 * 60)
    new_hour = total_minutes // 60
    new_minute = total_minutes % 60
    return f"{new_hour:02d}:{new_minute:02d}"


def _time_diff_minutes(start_str: str, end_str: str) -> int:
    """Calculate positive minute difference from start to end HH:MM or HHhMM."""
    sh, sm = _parse_time_str_parts(start_str)
    eh, em = _parse_time_str_parts(end_str)
    start_total = sh * 60 + sm
    end_total = eh * 60 + em
    diff = end_total - start_total
    return diff if diff > 0 else diff + 24 * 60


def _normalize_clock_hour(hour: int, meridian: str | None) -> int:
    """Adjust hour based on Vietnamese period markers (sáng, chiều, tối, etc.)."""
    if not meridian:
        return hour
    mer = meridian.strip().lower()
    if mer in {"chiều", "chieu", "pm", "tối", "toi"}:
        if 1 <= hour <= 11:
            return hour + 12
        if hour == 12:
            return 12
    elif mer in {"sáng", "sang", "am"}:
        if hour == 12:
            return 0
    return hour


def _cut_span(text: str, start: int, end: int) -> str:
    """Remove a matched slice from text, leaving clean spaces."""
    return (text[:start] + " " + text[end:]).strip()


# -----------------------------------------------------------------------------
# 1. REMINDER EXTRACTION
# -----------------------------------------------------------------------------
_REMINDER_PATTERN = re.compile(
    r"\b(?:báo\s*trước|nhắc\s*trước|remind|báo|nhắc)\s*(\d+)\s*(h|tiếng|tieng|giờ|gio|p|phút|phut)\b",
    re.IGNORECASE,
)


def _extract_reminder(text: str) -> tuple[int, str]:
    match = _REMINDER_PATTERN.search(text)
    if not match:
        return DEFAULT_REMINDER_MINUTES, text
    val = int(match.group(1))
    unit = match.group(2).lower()
    if unit in {"h", "tiếng", "tieng", "giờ", "gio"}:
        reminder = val * 60
    else:
        reminder = val
    cleaned_text = _cut_span(text, match.start(), match.end())
    return reminder, cleaned_text


# -----------------------------------------------------------------------------
# 2. DATE EXTRACTION
# -----------------------------------------------------------------------------
_RELATIVE_DATE_PATTERNS = [
    (re.compile(r"\b(?:ngày\s*mốt|ngay\s*mot|ngày\s*kia|ngay\s*kia|mốt|mot)\b", re.IGNORECASE), 2),
    (re.compile(r"\b(?:ngày\s*mai|ngay\s*mai|mai)\b", re.IGNORECASE), 1),
    (re.compile(r"\b(?:hôm\s*nay|hom\s*nay|nay)\b", re.IGNORECASE), 0),
]

_WEEKDAY_PATTERN = re.compile(
    r"\b(?:thứ|thu|t)\s*([2-7]|hai|ba|bốn|bon|tư|tu|năm|nam|sáu|sau|bảy|bay)|(?:chủ\s*nhật|chu\s*nhat|cn)\b",
    re.IGNORECASE,
)
_WEEK_MODIFIER_PATTERN = re.compile(
    r"\b(tuần\s*sau|tuan\s*sau|tới|toi|tuần\s*này|tuan\s*nay)\b",
    re.IGNORECASE,
)

_NUMERIC_DATE_PATTERNS = [
    # YYYY-MM-DD or YYYY/MM/DD
    re.compile(r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b"),
    # DD-MM-YYYY or DD/MM/YYYY
    re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b"),
    # ngày DD tháng MM (năm YYYY)?
    re.compile(r"\b(?:ngày\s*)?(\d{1,2})\s*tháng\s*(\d{1,2})(?:\s*năm\s*(\d{4}))?\b", re.IGNORECASE),
    # DD/MM or DD-MM
    re.compile(r"\b(\d{1,2})[/-](\d{1,2})\b"),
]


def _extract_date(text: str, ref_date: dt.date) -> tuple[dt.date | None, str]:
    # 1. Check relative words (hôm nay, ngày mai, ngày mốt)
    for pattern, days in _RELATIVE_DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            found_date = ref_date + dt.timedelta(days=days)
            cleaned = _cut_span(text, match.start(), match.end())
            return found_date, cleaned

    # 2. Check numeric dates
    # First check YYYY-MM-DD
    m_iso = _NUMERIC_DATE_PATTERNS[0].search(text)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            found_date = dt.date(y, m, d)
            return found_date, _cut_span(text, m_iso.start(), m_iso.end())
        except ValueError:
            pass

    # Check DD/MM/YYYY
    m_dmy = _NUMERIC_DATE_PATTERNS[1].search(text)
    if m_dmy:
        d, m, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
        try:
            found_date = dt.date(y, m, d)
            return found_date, _cut_span(text, m_dmy.start(), m_dmy.end())
        except ValueError:
            pass

    # Check "ngày D tháng M (năm Y)?"
    m_textual = _NUMERIC_DATE_PATTERNS[2].search(text)
    if m_textual:
        d = int(m_textual.group(1))
        m = int(m_textual.group(2))
        y = int(m_textual.group(3)) if m_textual.group(3) else ref_date.year
        try:
            found_date = dt.date(y, m, d)
            return found_date, _cut_span(text, m_textual.start(), m_textual.end())
        except ValueError:
            pass

    # Check DD/MM
    m_dm = _NUMERIC_DATE_PATTERNS[3].search(text)
    if m_dm:
        d, m = int(m_dm.group(1)), int(m_dm.group(2))
        if 1 <= d <= 31 and 1 <= m <= 12:
            try:
                found_date = dt.date(ref_date.year, m, d)
                return found_date, _cut_span(text, m_dm.start(), m_dm.end())
            except ValueError:
                pass

    # 3. Check weekday (Thứ 2..Thứ 7, CN)
    m_wd = _WEEKDAY_PATTERN.search(text)
    if m_wd:
        matched_str = m_wd.group(0).lower().strip()
        matched_arg = (m_wd.group(1) or "").lower().strip()
        weekday_idx = WEEKDAY_MAP.get(matched_arg) if matched_arg else WEEKDAY_MAP.get(matched_str)
        if weekday_idx is not None:
            span_start, span_end = m_wd.start(), m_wd.end()
            # Check if there is an adjacent week modifier like "tuần sau" or "tới"
            mod_match = _WEEK_MODIFIER_PATTERN.search(text)
            is_next_week = False
            if mod_match:
                mod_str = mod_match.group(1).lower()
                if "sau" in mod_str or "tới" in mod_str or "toi" in mod_str:
                    is_next_week = True
                span_start = min(span_start, mod_match.start())
                span_end = max(span_end, mod_match.end())

            current_wd = ref_date.weekday()
            if weekday_idx == current_wd:
                days_ahead = 7 if is_next_week else 0
            elif weekday_idx > current_wd:
                days_ahead = (weekday_idx - current_wd) + (7 if is_next_week else 0)
            else:
                # weekday_idx < current_wd (already passed this week)
                # Next occurrence is in next week: (7 - current_wd + weekday_idx)
                days_ahead = 7 - current_wd + weekday_idx

            found_date = ref_date + dt.timedelta(days=days_ahead)
            return found_date, _cut_span(text, span_start, span_end)

    return None, text


# -----------------------------------------------------------------------------
# 3. TIME AND DURATION EXTRACTION
# -----------------------------------------------------------------------------
# Range: 9h - 11h, 9h đến 11h30, 09:00 - 11:00, từ 14h tới 16h30
_TIME_RANGE_PATTERN = re.compile(
    r"(?:từ\s+|tu\s+)?(\d{1,2})(?:[h:](\d{2})?|h)\s*(?:đến|den|tới|toi|-|->)\s*(\d{1,2})(?:[h:](\d{2})?|h)(?:\s*(sáng|sang|chiều|chieu|tối|toi|am|pm))?",
    re.IGNORECASE,
)

# Start time with duration: 9h trong 2 tiếng, 14h kéo dài 1h30p, 8h trong 45 phút
_TIME_DURATION_PATTERN = re.compile(
    r"(?:lúc\s+|luc\s+|vào\s+|vao\s+)?(\d{1,2})(?:[h:](\d{2})?|h)(?:\s*(sáng|sang|chiều|chieu|tối|toi|am|pm))?\s+(?:trong|kéo\s*dài|keo\s*dai|khoảng|khoang)\s+(\d+)\s*(h|tiếng|tieng|giờ|gio|p|phút|phut)(?:\s*(\d+)\s*(?:p|phút|phut))?",
    re.IGNORECASE,
)

# Single time: 9h, 9h30, 09:00, 14h30, 2h chiều, 8h tối, lúc 7h sáng
_SINGLE_TIME_PATTERN = re.compile(
    r"(?:lúc\s+|luc\s+|vào\s+|vao\s+)?(\d{1,2})(?:[h:](\d{2})?|h)(?:\s*(sáng|sang|chiều|chieu|tối|toi|am|pm))?",
    re.IGNORECASE,
)


def _extract_time_and_duration(text: str) -> tuple[str | None, str | None, int, str]:
    """Extract start_time, end_time, duration_minutes, and return remaining text."""
    # 1. Check time range (e.g. 9h - 11h)
    m_range = _TIME_RANGE_PATTERN.search(text)
    if m_range:
        h1 = int(m_range.group(1))
        min1 = int(m_range.group(2) or 0)
        h2 = int(m_range.group(3))
        min2 = int(m_range.group(4) or 0)
        meridian = m_range.group(5)

        h1 = _normalize_clock_hour(h1, meridian)
        h2 = _normalize_clock_hour(h2, meridian)
        if h2 < h1 and h2 <= 12:
            h2 += 12

        if 0 <= h1 <= 23 and 0 <= min1 <= 59 and 0 <= h2 <= 23 and 0 <= min2 <= 59:
            start_str = f"{h1:02d}:{min1:02d}"
            end_str = f"{h2:02d}:{min2:02d}"
            duration = _time_diff_minutes(start_str, end_str)
            cleaned = _cut_span(text, m_range.start(), m_range.end())
            return start_str, end_str, duration, cleaned

    # 2. Check single time with explicit duration (e.g. 9h trong 2 tiếng)
    m_dur = _TIME_DURATION_PATTERN.search(text)
    if m_dur:
        h = int(m_dur.group(1))
        minute = int(m_dur.group(2) or 0)
        meridian = m_dur.group(3)
        h = _normalize_clock_hour(h, meridian)

        dur_val = int(m_dur.group(4))
        dur_unit = m_dur.group(5).lower()
        extra_min = int(m_dur.group(6) or 0)

        if dur_unit in {"h", "tiếng", "tieng", "giờ", "gio"}:
            total_duration = dur_val * 60 + extra_min
        else:
            total_duration = dur_val + extra_min

        if total_duration <= 0:
            total_duration = DEFAULT_DURATION_MINUTES

        if 0 <= h <= 23 and 0 <= minute <= 59:
            start_str = f"{h:02d}:{minute:02d}"
            end_str = _add_minutes_to_time(start_str, total_duration)
            cleaned = _cut_span(text, m_dur.start(), m_dur.end())
            return start_str, end_str, total_duration, cleaned

    # 3. Check single time
    m_single = _SINGLE_TIME_PATTERN.search(text)
    if m_single:
        h = int(m_single.group(1))
        minute = int(m_single.group(2) or 0)
        meridian = m_single.group(3)
        h = _normalize_clock_hour(h, meridian)

        if 0 <= h <= 23 and 0 <= minute <= 59:
            start_str = f"{h:02d}:{minute:02d}"
            end_str = _add_minutes_to_time(start_str, DEFAULT_DURATION_MINUTES)
            cleaned = _cut_span(text, m_single.start(), m_single.end())
            return start_str, end_str, DEFAULT_DURATION_MINUTES, cleaned

    return None, None, DEFAULT_DURATION_MINUTES, text


# -----------------------------------------------------------------------------
# 4. LOCATION EXTRACTION
# -----------------------------------------------------------------------------
_LOCATION_PATTERN = re.compile(
    r"(?:\s+|^)(?:(?:ở|o|tại|tai|phòng|phong)\s+|@\s*)([^,;\n]+?)(?=(?:\s+(?:báo|nhắc|remind|\bngày\b|\bthứ\b|\bmai\b|\bmốt\b)|$))",
    re.IGNORECASE,
)


def _extract_location(text: str) -> tuple[str | None, str]:
    match = _LOCATION_PATTERN.search(text)
    if not match:
        return None, text
    loc = match.group(1).strip()
    matched_full = match.group(0).strip().lower()
    if matched_full.startswith("phòng") or matched_full.startswith("phong"):
        loc = f"Phòng {loc}"
    cleaned = _cut_span(text, match.start(), match.end())
    return loc, cleaned


# -----------------------------------------------------------------------------
# 5. CLEANUP REMAINING TEXT AS TITLE
# -----------------------------------------------------------------------------
_NOISE_WORDS_PATTERN = re.compile(
    r"^(?:lúc|luc|vào|vao|ngày|ngay|việc|viec|lịch|lich|hẹn|hen|đi|di|có|co|[-|:,])\s*",
    re.IGNORECASE,
)


def _clean_title(text: str) -> str | None:
    """Normalize remaining text into a clean appointment title."""
    cleaned = re.sub(r"[\s\t\n]+", " ", text).strip(" ,;:-|")
    for _ in range(3):
        m = _NOISE_WORDS_PATTERN.search(cleaned)
        if m and m.start() == 0 and len(cleaned) > m.end():
            first_word = m.group(0).strip().lower()
            if first_word in {"đi", "di"} and len(cleaned.split()) > 1:
                break
            cleaned = cleaned[m.end():].strip(" ,;:-|")
    return cleaned if cleaned else None


# -----------------------------------------------------------------------------
# MAIN PARSER ENTRYPOINT
# -----------------------------------------------------------------------------
def parse_quick_add(text: str, *, reference_date: dt.date | None = None) -> QuickAddResult:
    """Parse a free-form Vietnamese appointment command into structured fields."""
    raw = str(text or "").strip()
    if not raw:
        return QuickAddResult(
            title=None,
            appointment_date=None,
            start_time=None,
            end_time=None,
            missing_fields=("date", "start_time", "job"),
            raw_input=raw,
        )

    ref_date = reference_date or local_today()
    working_text = raw

    # Step 1: Strip optional /add prefix
    if working_text.lower().startswith("/add"):
        working_text = re.sub(r"^/add(?:@\w+)?\s*", "", working_text, flags=re.IGNORECASE).strip()

    # Step 2: Extract reminder
    reminder_minutes, working_text = _extract_reminder(working_text)

    # Step 3: Extract date
    appt_date, working_text = _extract_date(working_text, ref_date)

    # Step 4: Extract time and duration
    start_time, end_time, duration_minutes, working_text = _extract_time_and_duration(working_text)

    # Step 5: Extract location
    location, working_text = _extract_location(working_text)

    # Step 6: Remaining text is the title
    title = _clean_title(working_text)

    # Determine missing mandatory fields
    missing: list[str] = []
    if not appt_date:
        missing.append("date")
    if not start_time:
        missing.append("start_time")
    if not title:
        missing.append("job")

    return QuickAddResult(
        title=title,
        appointment_date=appt_date,
        start_time=start_time,
        end_time=end_time,
        duration_minutes=duration_minutes,
        location=location,
        reminder_minutes=reminder_minutes,
        missing_fields=tuple(missing),
        raw_input=raw,
    )
