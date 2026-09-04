"""
Pure HTML parsers for TDTU student timetable / schedule pages.
Does NOT access network, environment variables, or external services.
"""

import logging
import re
import unicodedata
from typing import Any
from bs4 import BeautifulSoup

from tdtu.exceptions import TDTUParsingError, TDTUProtocolError

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


def _normalize_text(text: str) -> str:
    """Normalize text by stripping diacritics and extra spaces for pattern matching."""
    s = (text or "").strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).lower()


def detect_status(text: str) -> str:
    """
    Detect internal status code matching existing Playwright crawler contract.
    Returns: 'absent', 'makeup', 'cancelled', 'moved', or 'scheduled'.
    """
    norm = _normalize_text(text)

    # Absence keywords: "báo vắng", "GV vắng", "nghỉ học", "nghỉ tiết", "vắng tiết", "GV báo vắng", "lớp nghỉ"
    if re.search(r"\b(bao\s*vang|gv\s*vang|nghi\s*hoc|nghi\s*tiet|vang\s*tiet|gv\s*bao\s*vang|lop\s*nghi)\b", norm):
        return "absent"

    # Makeup class keywords: "học bù", "lịch bù", "dạy bù", "bù học", "bù tiết", "LHB"
    if re.search(r"\b(hoc\s*bu|lich\s*bu|day\s*bu|bu\s*hoc|bu\s*tiet|lhb)\b", norm):
        return "makeup"

    # Cancelled keywords: "hủy lớp", "hủy môn"
    if re.search(r"\b(huy\s*lop|huy\s*mon|cancel)\b", norm):
        return "cancelled"

    # Moved keywords: "dời lịch", "đổi phòng"
    if re.search(r"\b(doi\s*lich|doi\s*phong|rescheduled|moved)\b", norm):
        return "moved"

    return "scheduled"


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
    Explicitly skips Headerrow to avoid header text being parsed as course titles.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=re.compile(r".*Table1.*", re.IGNORECASE))
    if not table:
        return _parse_column_based_schedule(soup, student_id=student_id)

    header_tr = table.find("tr", class_="Headerrow") or table.find("tr")
    if not header_tr:
        return []

    headers = [th.get_text().strip() for th in header_tr.find_all(["td", "th"])]
    col_days = [_normalize_day(h) for h in headers]

    entries: list[dict[str, Any]] = []

    for row in table.find_all("tr"):
        if "Headerrow" in row.get("class", []):
            continue

        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue

        for col_idx, cell in enumerate(cells[1:], start=1):
            if col_idx >= len(col_days):
                break
            day_of_week = col_days[col_idx]
            if not day_of_week:
                continue

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
    Parse weekly grid timetable when weekly view is active.
    Extracts dates from #ThoiKhoaBieu1_btnTuanHienTai or column headers.
    Raises TDTUProtocolError if weekly dates cannot be resolved.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Check for week button or weekly view indicator
    week_btn = soup.find("input", id=re.compile(r".*btnTuanHienTai.*"))
    if not week_btn:
        # Check if weekly view radio button is selected or page has weekly indicators
        weekly_radio = soup.find("input", id=re.compile(r".*radXemTKBTheoTuan.*"))
        if not (weekly_radio and weekly_radio.has_attr("checked")):
            return None

    table = soup.find("table", id=re.compile(r".*tbTKBTheoTuan.*|.*Table1.*|.*Grid.*", re.IGNORECASE))
    if not table:
        return None

    header_tr = table.find("tr", class_="Headerrow") or table.find("tr")
    if not header_tr:
        return None

    headers = [th.get_text().strip() for th in header_tr.find_all(["td", "th"])]
    if len(headers) < 8:
        return None

    # Derive session dates from #ThoiKhoaBieu1_btnTuanHienTai or column headers
    dates_map: dict[str, str] = {}
    col_days = [_normalize_day(h) for h in headers]

    if week_btn:
        btn_val = week_btn.get("value", "")
        matches = re.findall(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", btn_val)
        if len(matches) >= 2:
            try:
                import datetime
                d1, m1, y1 = int(matches[0][0]), int(matches[0][1]), int(matches[0][2])
                d2, m2, y2 = int(matches[1][0]), int(matches[1][1]), int(matches[1][2])
                if y1 < 100: y1 += 2000
                if y2 < 100: y2 += 2000
                start_dt = datetime.date(y1, m1, d1)
                end_dt = datetime.date(y2, m2, d2)
                if end_dt < start_dt or (end_dt - start_dt).days > 7:
                    raise TDTUProtocolError(f"Invalid week date range bounds in control: {btn_val}")
                for i in range(7):
                    day_dt = start_dt + datetime.timedelta(days=i)
                    day_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][i]
                    dates_map[day_name] = day_dt.strftime("%Y-%m-%d")
            except ValueError as exc:
                raise TDTUProtocolError(f"Invalid date format in week range control '{btn_val}': {exc}") from exc

    # Fallback: check header text for explicit dates if week_btn map is empty
    if not dates_map:
        for idx, h_text in enumerate(headers):
            dm = re.search(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", h_text)
            if dm and idx < len(col_days):
                d_val, m_val = int(dm.group(1)), int(dm.group(2))
                y_val = int(dm.group(3)) if dm.group(3) else 2026
                if y_val < 100: y_val += 2000
                try:
                    import datetime
                    dt_obj = datetime.date(y_val, m_val, d_val)
                    dates_map[col_days[idx]] = dt_obj.strftime("%Y-%m-%d")
                except ValueError:
                    pass

    col_dates = [dates_map.get(d, "") for d in col_days]

    entries: list[dict[str, Any]] = []

    for row in table.find_all("tr"):
        if "Headerrow" in row.get("class", []):
            continue

        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue

        p_match = re.search(r"\d+", cells[0].get_text().strip())
        row_period = int(p_match.group(0)) if p_match else 0

        for col_idx, cell in enumerate(cells[1:], start=1):
            if col_idx >= len(col_days):
                break
            day_of_week = col_days[col_idx]
            session_date = col_dates[col_idx] if col_idx < len(col_dates) else ""
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
                    if not session_date:
                        raise TDTUProtocolError(
                            f"Weekly schedule entry '{entry['subject_name']}' is missing concrete session_date"
                        )
                    try:
                        import datetime
                        datetime.date.fromisoformat(session_date)
                    except ValueError as exc:
                        raise TDTUProtocolError(f"Invalid session_date '{session_date}': {exc}") from exc

                    entry["session_date"] = session_date
                    if entry["start_period"] == 0 and row_period > 0:
                        entry["start_period"] = row_period
                        entry["end_period"] = row_period + 2
                    entries.append(entry)

    return _deduplicate_schedule(entries)


def parse_period_range(text: str) -> tuple[int, int]:
    """
    Parse start and end period from cell text containing 'Tiết' or 'Period'.
    Supports single digits (123->1-3), ranges (10-12, 1 to 3), and multi-digit sequences (101112, 131415).
    """
    period_match = re.search(r"(?:Tiết|Period)[:\s]*([0-9\s\-to]+)", text, re.IGNORECASE)
    if not period_match:
        return 0, 0

    p_raw = period_match.group(1).strip()
    if not p_raw:
        return 0, 0

    m_range = re.search(r"(\d+)\s*[-to]+\s*(\d+)", p_raw)
    if m_range:
        return int(m_range.group(1)), int(m_range.group(2))

    clean_p = re.sub(r"\s+", "", p_raw)
    if len(clean_p) == 6 and clean_p.isdigit():
        p1, p2, p3 = int(clean_p[0:2]), int(clean_p[2:4]), int(clean_p[4:6])
        if 10 <= p1 <= 16 and 10 <= p2 <= 16 and 10 <= p3 <= 16:
            return min(p1, p2, p3), max(p1, p2, p3)

    digits = [int(d) for d in clean_p if d.isdigit()]
    if digits:
        return min(digits), max(digits)

    return 0, 0


def _parse_schedule_cell_text(text: str, day_of_week: str, student_id: str) -> dict[str, Any] | None:
    """Parse text block inside a schedule table cell."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None

    # Skip header-like texts accidentally passed
    if any(h in lines[0] for h in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]):
        return None

    subject_name = re.sub(r"\s*\|\s*.*$", "", lines[0]).strip()
    if not subject_name:
        return None

    full_text = " ".join(lines)
    room = ""
    status = detect_status(full_text)

    room_match = re.search(
        r"(?:Phòng|Room)\b(?:[\s\n|]*(?:Room|Phòng)\b)*[\s\n|:]*([A-Z0-9._-]+(?:\s+[A-Z0-9._-]+)*)(?=\s*(?:\n|\(|Tuần|Week|Tiết|Period|GV|báo|vắng|nghỉ|học|bù|lhb|hủy|dời|$))",
        full_text,
        re.IGNORECASE,
    )
    if room_match:
        room = room_match.group(1).strip()

    start_period, end_period = parse_period_range(full_text)

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

            full_row_text = " ".join(cells)
            status = detect_status(full_row_text)

            entries.append({
                "student_id": student_id,
                "subject_name": subj,
                "room": room,
                "day_of_week": day_en,
                "session_date": "",
                "start_period": start_p,
                "end_period": end_p,
                "status": status,
            })

    return _deduplicate_schedule(entries)


def _deduplicate_schedule(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Deduplicate schedule items by key fields.
    MUST include `status` in deduplication signature so paired rows (e.g. absent and makeup)
    are preserved! (Addresses Blocker 2).
    """
    seen = set()
    deduped = []
    for e in entries:
        key = (
            str(e.get("subject_name") or "").strip().lower(),
            str(e.get("room") or "").strip().lower(),
            str(e.get("day_of_week") or "").strip().lower(),
            str(e.get("session_date") or "").strip(),
            int(e.get("start_period", 0) or 0),
            int(e.get("end_period", 0) or 0),
            str(e.get("status") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped
