"""
Pure HTML parsers for TDTU student timetable / schedule pages.
Does NOT access network, environment variables, or external services.
"""

import logging
import re
from typing import Any
from bs4 import BeautifulSoup, Tag

from tdtu.exceptions import TDTUParsingError

logger = logging.getLogger(__name__)

ENGLISH_DAYS = [
    ("monday", "Monday"),
    ("tuesday", "Tuesday"),
    ("wednesday", "Wednesday"),
    ("thursday", "Thursday"),
    ("friday", "Friday"),
    ("saturday", "Saturday"),
    ("sunday", "Sunday"),
]

VN_DAY_MAP = [
    ("thứ 2", "Monday"),
    ("thu 2", "Monday"),
    ("thứ hai", "Monday"),
    ("thứ 3", "Tuesday"),
    ("thu 3", "Tuesday"),
    ("thứ ba", "Tuesday"),
    ("thứ 4", "Wednesday"),
    ("thu 4", "Wednesday"),
    ("thứ tư", "Wednesday"),
    ("thứ 5", "Thursday"),
    ("thu 5", "Thursday"),
    ("thứ năm", "Thursday"),
    ("thứ 6", "Friday"),
    ("thu 6", "Friday"),
    ("thứ sáu", "Friday"),
    ("thứ 7", "Saturday"),
    ("thu 7", "Saturday"),
    ("thứ bảy", "Saturday"),
    ("chủ nhật", "Sunday"),
    ("chu nhat", "Sunday"),
    ("cn", "Sunday"),
]


def _normalize_day(raw: str) -> str:
    """Normalize day text (Vietnamese or English) to standard English weekday string."""
    lower = (raw or "").strip().lower()
    for needle, en in ENGLISH_DAYS:
        if needle in lower:
            return en
    for vn, en in VN_DAY_MAP:
        if vn in lower:
            return en
    return raw.strip()


def parse_semester_options(html: str) -> list[dict[str, str]]:
    """Parse semester dropdown (<select name="ThoiKhoaBieu1$cboHocKy">) options."""
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", id=re.compile(r".*cboHocKy.*", re.IGNORECASE))
    if not select:
        return []

    options = []
    for opt in select.find_all("option"):
        val = opt.get("value", "").strip()
        txt = opt.get_text().strip()
        selected = opt.has_attr("selected")
        options.append({
            "value": val,
            "text": txt,
            "selected": selected,
        })
    return options


def parse_active_semester(html: str) -> str:
    """Extract currently selected semester text from dropdown or page header."""
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", id=re.compile(r".*cboHocKy.*", re.IGNORECASE))
    if select:
        selected_opt = select.find("option", selected=True)
        if selected_opt:
            return selected_opt.get_text().strip()

    # Fallback to text label matching HK...
    match = re.search(r"HK\s*\d*(?:\s*hè)?/\d{4}-\d{4}", html, re.IGNORECASE)
    if match:
        return match.group(0).strip()
    return ""


def parse_schedule_html(html: str, student_id: str = "") -> list[dict[str, Any]]:
    """
    Parse schedule HTML and return list of schedule record dictionaries.
    Tries weekly grid table first; if not present, falls back to general schedule table or column table.
    """
    grid_entries = parse_weekly_grid_table(html, student_id=student_id)
    if grid_entries is not None and len(grid_entries) > 0:
        return grid_entries

    general_entries = parse_general_schedule_table(html, student_id=student_id)
    if general_entries:
        return general_entries

    if grid_entries is not None:
        return grid_entries

    return []


def parse_general_schedule_table(html: str, student_id: str = "") -> list[dict[str, Any]]:
    """
    Parse general schedule table (#ThoiKhoaBieu1_Table1).
    Matrix layout: Rows (Morning, Afternoon, Evening), Columns (Monday - Sunday).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=re.compile(r".*Table1.*", re.IGNORECASE))
    if not table:
        # Fallback to column-based table matching headers
        return _parse_column_based_schedule(soup, student_id=student_id)

    # Days mapping from header row
    header_tr = table.find("tr", class_="Headerrow") or table.find("tr")
    if not header_tr:
        return []

    headers = [th.get_text().strip() for th in header_tr.find_all(["td", "th"])]
    col_days = []
    for h in headers:
        col_days.append(_normalize_day(h))

    entries: list[dict[str, Any]] = []

    # Iterate content rows (skip Headerrow)
    for row in table.find_all("tr"):
        if "Headerrow" in row.get("class", []):
            continue
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue

        # Process each day column (cells[1] onwards)
        for col_idx, cell in enumerate(cells[1:], start=1):
            day_of_week = col_days[col_idx] if col_idx < len(col_days) else ""
            if not day_of_week:
                continue

            # Check spans inside cell (each subject block is usually in a <span> or <p>)
            spans = cell.find_all("span") or [cell]
            for span in spans:
                text = span.get_text("\n").strip()
                if not text:
                    continue

                entry = _parse_schedule_cell_text(text, day_of_week, student_id)
                if entry:
                    entries.append(entry)

    return _deduplicate_schedule(entries)


def parse_weekly_grid_table(html: str, student_id: str = "") -> list[dict[str, Any]] | None:
    """
    Parse weekly grid timetable (when weekly view is selected).
    Matrix layout: Rows (Periods 1..15), Columns (Monday..Sunday).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=re.compile(r".*Table1.*|.*Grid.*", re.IGNORECASE))
    if not table:
        return None

    # Check if table headers contain week dates or weekday names
    header_tr = table.find("tr", class_="Headerrow") or table.find("tr")
    if not header_tr:
        return None

    headers = [th.get_text().strip() for th in header_tr.find_all(["td", "th"])]
    if len(headers) < 8:
        return None  # Needs period column + 7 day columns

    # Extract week date range if available in #ThoiKhoaBieu1_btnTuanHienTai
    week_btn = soup.find("input", id=re.compile(r".*btnTuanHienTai.*"))
    dates_map = {}
    if week_btn:
        btn_val = week_btn.get("value", "")
        matches = re.findall(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", btn_val)
        if len(matches) >= 2:
            try:
                import datetime
                d1, m1, y1 = int(matches[0][0]), int(matches[0][1]), int(matches[0][2])
                if y1 < 100: y1 += 2000
                start_dt = datetime.date(y1, m1, d1)
                for i in range(7):
                    day_dt = start_dt + datetime.timedelta(days=i)
                    day_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][i]
                    dates_map[day_name] = day_dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

    col_days = []
    col_dates = []
    for h in headers:
        day_en = _normalize_day(h)
        col_days.append(day_en)
        col_dates.append(dates_map.get(day_en, ""))

    entries: list[dict[str, Any]] = []

    for row in table.find_all("tr", class_=re.compile(r"rowContent|.*")):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue

        # Check period number from first cell
        p_match = re.search(r"\d+", cells[0].get_text().strip())
        row_period = int(p_match.group(0)) if p_match else 0

        for col_idx, cell in enumerate(cells[1:], start=1):
            if col_idx >= len(col_days):
                break
            day_of_week = col_days[col_idx]
            session_date = col_dates[col_idx]
            if not day_of_week:
                continue

            text = cell.get_text("\n").strip()
            if not text or text in ("-", "x", "trống", "rong"):
                continue

            spans = cell.find_all("span") or [cell]
            for span in spans:
                cell_text = span.get_text("\n").strip()
                if not cell_text:
                    continue

                entry = _parse_schedule_cell_text(cell_text, day_of_week, student_id)
                if entry:
                    if session_date:
                        entry["session_date"] = session_date
                    if entry["start_period"] == 0 and row_period > 0:
                        entry["start_period"] = row_period
                        entry["end_period"] = row_period
                    entries.append(entry)

    return _deduplicate_schedule(entries)


def _parse_schedule_cell_text(text: str, day_of_week: str, student_id: str) -> dict[str, Any] | None:
    """Parse text block inside a schedule table cell."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None

    # First line is usually subject name
    subject_name = lines[0]
    # Clean language labels like "| Political Economics"
    subject_name = re.sub(r"\s*\|\s*.*$", "", subject_name).strip()
    if not subject_name:
        return None

    room = ""
    start_period = 0
    end_period = 0
    status = "Học"

    full_text = " ".join(lines)

    # Extract Room: (Phòng: B204) or (Room: B204)
    room_match = re.search(r"(?:Phòng|Room)[:\s]*([A-Z0-9._-]+)", full_text, re.IGNORECASE)
    if room_match:
        room = room_match.group(1).strip()

    # Extract Period: Tiết 123 or Period 123 or Tiết: 1-3
    period_match = re.search(r"(?:Tiết|Period)[:\s]*(\d+)(?:\s*[-to]+\s*(\d+))?", full_text, re.IGNORECASE)
    if period_match:
        p_str = period_match.group(1)
        p_end_str = period_match.group(2)
        if p_end_str:
            start_period = int(p_str)
            end_period = int(p_end_str)
        elif len(p_str) == 1:
            start_period = int(p_str)
            end_period = int(p_str)
        elif len(p_str) > 1:
            start_period = int(p_str[0])
            end_period = int(p_str[-1])

    # Check status (e.g. Nghỉ / Cancelled)
    if re.search(r"\b(nghỉ|tạm nghỉ|cancel|cancelled)\b", full_text, re.IGNORECASE):
        status = "Nghỉ"

    return {
        "student_id": student_id,
        "subject_name": subject_name,
        "room": room,
        "day_of_week": day_of_week,
        "session_date": "",
        "start_period": start_period,
        "end_period": end_period,
        "status": status,
    }


def _parse_column_based_schedule(soup: BeautifulSoup, student_id: str) -> list[dict[str, Any]]:
    """Fallback parser for traditional column-based schedule table."""
    entries: list[dict[str, Any]] = []
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [td.get_text().strip().lower() for td in rows[0].find_all(["td", "th"])]
        if not any("môn" in h or "subject" in h for h in headers):
            continue

        subj_col = next((i for i, h in enumerate(headers) if "môn" in h or "subject" in h), None)
        room_col = next((i for i, h in enumerate(headers) if "phòng" in h or "room" in h), None)
        day_col = next((i for i, h in enumerate(headers) if "thứ" in h or "day" in h), None)
        start_col = next((i for i, h in enumerate(headers) if "bắt đầu" in h or "start" in h), None)
        end_col = next((i for i, h in enumerate(headers) if "kết thúc" in h or "end" in h), None)

        if subj_col is None or day_col is None:
            continue

        for row in rows[1:]:
            cells = [td.get_text().strip() for td in row.find_all("td")]
            if len(cells) <= max(subj_col, day_col):
                continue

            subj = cells[subj_col]
            if not subj:
                continue

            room = cells[room_col] if room_col is not None and room_col < len(cells) else ""
            day_raw = cells[day_col]
            day_en = _normalize_day(day_raw)

            start_p = 0
            end_p = 0
            if start_col is not None and start_col < len(cells):
                sm = re.search(r"\d+", cells[start_col])
                if sm: start_p = int(sm.group())
            if end_col is not None and end_col < len(cells):
                em = re.search(r"\d+", cells[end_col])
                if em: end_p = int(em.group())

            entries.append({
                "student_id": student_id,
                "subject_name": subj,
                "room": room,
                "day_of_week": day_en,
                "session_date": "",
                "start_period": start_p,
                "end_period": end_p,
                "status": "Học",
            })

    return _deduplicate_schedule(entries)


def _deduplicate_schedule(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate schedule items by key fields."""
    seen = set()
    deduped = []
    for e in entries:
        key = (
            e.get("subject_name"),
            e.get("room"),
            e.get("day_of_week"),
            e.get("session_date"),
            e.get("start_period"),
            e.get("end_period"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped
