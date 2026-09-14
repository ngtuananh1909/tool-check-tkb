""""
Pure HTML parser for TDTU student exam schedule pages.
Does NOT access network, environment variables, or external services.
"""

import datetime
import logging
import re
from typing import Any
from bs4 import BeautifulSoup

from time_utils import local_today
from tdtu.exceptions import TDTUParsingError

logger = logging.getLogger(__name__)

EXPECTED_EXAM_TABLE_IDS: dict[str, set[str]] = {
    "0": {"lichthi1_giuakytable", "giuakytable"},
    "1": {"lichthi1_cuoikytable", "cuoikytable"},
    "2": {"lichthi1_cuoiky2table", "cuoiky2table"},
}


def parse_date_iso(text: str, semester_hint: str = "") -> str:
    """
    Parse date from string like '15/09/2026' or '15-09-2026' into 'YYYY-MM-DD'.
    Derives year dynamically without hardcoding.
    """
    m = re.search(r"(\d{1,2})[/\.-](\d{1,2})(?:[/\.-](\d{2,4}))?", text or "")
    if not m:
        return ""
    d = int(m.group(1))
    mo = int(m.group(2))

    y = None
    if m.group(3):
        y = int(m.group(3))
        if y < 100:
            y += 2000

    if y is None and semester_hint:
        sem_match = re.search(r"HK\s*(\d)?.*?(\d{4})\s*-\s*(\d{4})", semester_hint, re.IGNORECASE)
        if sem_match:
            hk_num = sem_match.group(1)
            y1 = int(sem_match.group(2))
            y2 = int(sem_match.group(3))
            if hk_num == "1":
                y = y1 if mo >= 8 else y2
            elif hk_num == "2":
                y = y2
            else:
                y = y1
        else:
            m_year = re.search(r"20\d{2}", semester_hint)
            if m_year:
                y = int(m_year.group(0))

    if y is None:
        y = local_today().year

    try:
        dt = datetime.date(y, mo, d)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_time_str(text: str) -> str:
    """Parse time string like '07:30' or '7h30' into 'HH:MM'."""
    m = re.search(r"(\d{1,2})[:h](\d{2})", text or "", re.IGNORECASE)
    if not m:
        return ""
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_exam_cell(
    cell: Any,
    default_exam_type: str = "",
    semester_hint: str = "",
) -> dict[str, Any] | None:
    """
    Parse a single exam grid cell (td or div) containing exam schedule details.
    Extracts subject_name, exam_date, start_time, end_time (from range or duration),
    exam_room, exam_type, and notes (subject code, group, sub-group).
    Returns exam dictionary or None if cell does not contain an exam record.
    """
    text = cell.get_text("\n").strip() if hasattr(cell, "get_text") else str(cell).strip()
    if not text:
        return None

    lowered = text.lower()
    if not ("ngày thi" in lowered or "ngay thi" in lowered or "date" in lowered):
        return None
    if not ("giờ thi" in lowered or "gio thi" in lowered or "time" in lowered):
        return None

    # 1. Subject name: extract clean course title
    # Priority: <p> or <b> block containing title, stripping bilingual/secondary labels (.lbl-lang)
    subject = ""
    if hasattr(cell, "find"):
        p_tag = cell.find("p") or cell.find("b")
        if p_tag:
            p_copy = BeautifulSoup(str(p_tag), "html.parser")
            for lbl in p_copy.find_all("label", class_="lbl-lang"):
                lbl.decompose()
            subject = p_copy.get_text().strip()

    if not subject:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if lines:
            subject = lines[0].split("|")[0].strip()

    subject = subject.split("|")[0].strip()
    if not subject:
        return None

    # 2. Exam date: scan across entire cell text to handle multi-line/tag-separated date
    date_iso = ""
    m_date = re.search(r"(?:ngày\s*thi|date)[^\d]*(\d{1,2}[/\.-]\d{1,2}(?:[/\.-]\d{2,4})?)", text, re.IGNORECASE)
    if m_date:
        date_iso = parse_date_iso(m_date.group(1), semester_hint=semester_hint)
    if not date_iso:
        date_iso = parse_date_iso(text, semester_hint=semester_hint)
    if not date_iso:
        return None

    # 3. Start time
    start_t = ""
    m_time = re.search(r"(?:giờ\s*thi|time)[^\d]*(\d{1,2}[:h]\d{2})", text, re.IGNORECASE)
    if m_time:
        start_t = parse_time_str(m_time.group(1))
    if not start_t:
        start_t = parse_time_str(text)

    # 4. End time: range (07:30 - 09:00) or calculate from duration (30 phút / 45 phút)
    end_t = ""
    range_m = re.search(r"(\d{1,2}[:h]\d{2})\s*(?:-|–|—|to|đến|den|->|~)\s*(\d{1,2}[:h]\d{2})", text, re.IGNORECASE)
    if range_m:
        end_t = parse_time_str(range_m.group(2))
    elif start_t:
        m_dur = re.search(r"(?:thời\s*lượng|duration)[^\d]*(\d+)\s*(?:phút|minute|m|min)?", text, re.IGNORECASE)
        if m_dur:
            dur_mins = int(m_dur.group(1))
            sh, sm = map(int, start_t.split(":"))
            total_mins = sh * 60 + sm + dur_mins
            end_t = f"{(total_mins // 60) % 24:02d}:{total_mins % 60:02d}"

    # 5. Room
    room = ""
    m_room = re.search(r"(?:phòng|room)\s*(?:thi)?\s*(?:\|[^\n:]*)?[:\-]?\s*([A-Za-z0-9_\.-]+)", text, re.IGNORECASE)
    if m_room:
        room = m_room.group(1).strip()

    # 6. Exam type
    exam_type = default_exam_type

    # 7. Metadata in notes (course code, group, sub-group)
    m_code = re.search(r"\((\d{5,7})\)", text)
    code = m_code.group(1) if m_code else ""
    m_grp = re.search(r"(?:nhóm|group)\s*(?:\|[^\n:]*)?[:\-]?\s*(\w+)", text, re.IGNORECASE)
    grp = m_grp.group(1) if m_grp else ""
    m_subgrp = re.search(r"(?:tổ|sub-group)\s*(?:\|[^\n:]*)?[:\-]?\s*(\w+)", text, re.IGNORECASE)
    subgrp = m_subgrp.group(1) if m_subgrp else ""

    notes_parts = ["Crawled from exam schedule"]
    if code:
        notes_parts.append(f"Mã MH: {code}")
    if grp:
        notes_parts.append(f"Nhóm: {grp}")
    if subgrp:
        notes_parts.append(f"Tổ: {subgrp}")

    return {
        "subject_name": subject,
        "exam_date": date_iso,
        "start_time": start_t,
        "end_time": end_t,
        "exam_room": room,
        "exam_type": exam_type or default_exam_type,
        "notes": " - ".join(notes_parts),
    }


def parse_exam_html(
    html: str,
    default_exam_type: str = "",
    semester_hint: str = "",
    tab_arg: str | None = None,
) -> list[dict[str, Any]]:
    """
    Parse exam schedule HTML and extract exam records.
    Supports both standard column-based tables and weekly calendar grid tables.
    Optionally scoped to exact table ID corresponding to tab_arg ('0', '1', '2').
    Raises TDTUParsingError if target exam table contains non-header data rows but 0 records could be parsed.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []

    target_table_ids = EXPECTED_EXAM_TABLE_IDS.get(str(tab_arg), set()) if tab_arg is not None else set()

    # 1. Parse candidate tables
    tables = soup.find_all("table")
    non_header_data_rows_found = 0

    for table in tables:
        t_id = (table.get("id") or "").lower()
        t_name = (table.get("name") or "").lower()

        if target_table_ids and not (t_id in target_table_ids or t_name in target_table_ids):
            continue

        trs = table.find_all("tr")
        if not trs:
            continue

        # Check for non-header data rows in target exam tables before parsing
        table_data_rows = 0
        for tr in trs[1:]:
            if "Headerrow" in tr.get("class", []):
                continue
            tds = [td.get_text().strip() for td in tr.find_all("td")]
            row_text = " ".join(tds).lower()
            if not tds or not any(tds):
                continue
            if any(k in row_text for k in ("không có lịch", "chưa có lịch", "no data", "không tìm thấy")):
                continue
            table_data_rows += 1

        if table_data_rows > 0:
            non_header_data_rows_found += table_data_rows

        # Inspect headers ONLY from the first row of this table
        first_tr_cells = trs[0].find_all(["th", "td"])
        headers = [c.get_text().strip().lower() for c in first_tr_cells]

        # Standard column table layout: has subject column (excluding "monday"), date column, time column
        has_subject_col = any(
            re.search(r"\b(môn|mon|subject)\b", h) and not re.search(r"monday", h)
            for h in headers
        )
        has_date_col = any(re.search(r"\b(ngày|ngay|date)\b", h) for h in headers)
        has_time_col = any(re.search(r"\b(giờ|gio|time)\b", h) for h in headers)

        if has_subject_col and (has_date_col or has_time_col):
            # Layout A: Standard column table
            idx_subject = next(
                (i for i, h in enumerate(headers) if re.search(r"\b(môn|mon|subject)\b", h) and not re.search(r"monday", h)),
                -1,
            )
            idx_date = next((i for i, h in enumerate(headers) if re.search(r"\b(ngày|ngay|date)\b", h)), -1)
            idx_time = next((i for i, h in enumerate(headers) if re.search(r"\b(giờ|gio|time)\b", h)), -1)
            idx_room = next((i for i, h in enumerate(headers) if re.search(r"\b(phòng|phong|room)\b", h)), -1)
            idx_type = next((i for i, h in enumerate(headers) if re.search(r"\b(hình thức|hinh thuc|type|loại|loai)\b", h)), -1)

            for tr in trs[1:]:
                if "Headerrow" in tr.get("class", []):
                    continue
                tds = [td.get_text().strip() for td in tr.find_all("td")]
                subject = tds[idx_subject] if idx_subject >= 0 and idx_subject < len(tds) else ""
                if not subject:
                    continue

                date_text = tds[idx_date] if idx_date >= 0 and idx_date < len(tds) else " ".join(tds)
                date_iso = parse_date_iso(date_text, semester_hint=semester_hint)
                if not date_iso:
                    continue

                time_text = tds[idx_time] if idx_time >= 0 and idx_time < len(tds) else " ".join(tds)
                start_t = parse_time_str(time_text)
                end_t = ""
                range_m = re.search(r"(\d{1,2}[:h]\d{2})\s*(?:-|–|—|to|đến|den|->|~)\s*(\d{1,2}[:h]\d{2})", time_text, re.IGNORECASE)
                if range_m:
                    end_t = parse_time_str(range_m.group(2))
                elif start_t:
                    m_dur = re.search(r"(?:thời\s*lượng|duration)[^\d]*(\d+)\s*(?:phút|minute|m|min)?", time_text, re.IGNORECASE)
                    if m_dur:
                        dur_mins = int(m_dur.group(1))
                        sh, sm = map(int, start_t.split(":"))
                        total_mins = sh * 60 + sm + dur_mins
                        end_t = f"{(total_mins // 60) % 24:02d}:{total_mins % 60:02d}"

                exam_room = tds[idx_room] if idx_room >= 0 and idx_room < len(tds) else ""
                exam_type = tds[idx_type] if idx_type >= 0 and idx_type < len(tds) else default_exam_type

                rows.append({
                    "subject_name": subject,
                    "exam_date": date_iso,
                    "start_time": start_t,
                    "end_time": end_t,
                    "exam_room": exam_room,
                    "exam_type": exam_type or default_exam_type,
                    "notes": "Crawled from exam schedule",
                })
        else:
            # Layout B: Calendar Grid table (columns are weekdays, each cell is an exam card)
            for tr in trs[1:]:
                if "Headerrow" in tr.get("class", []):
                    continue
                for td in tr.find_all("td"):
                    exam_record = parse_exam_cell(td, default_exam_type=default_exam_type, semester_hint=semester_hint)
                    if exam_record:
                        rows.append(exam_record)

    # Distinguish valid empty table from parser failure on non-empty data rows
    if target_table_ids and non_header_data_rows_found > 0 and len(rows) == 0:
        raise TDTUParsingError(
            f"Exam table for tab '{default_exam_type}' (arg={tab_arg}) contained {non_header_data_rows_found} data rows but no records could be parsed"
        )

    # 2. Standalone grid cell blocks fallback (when no tab_arg is specified)
    if tab_arg is None and not rows:
        cells = soup.find_all(["td", "div"])
        for cell in cells:
            exam_record = parse_exam_cell(cell, default_exam_type=default_exam_type, semester_hint=semester_hint)
            if exam_record:
                rows.append(exam_record)

    return deduplicate_exam_rows(rows)


def deduplicate_exam_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate exam items by key fields."""
    seen = set()
    deduped = []
    for r in rows:
        key = (
            str(r.get("subject_name") or "").strip().lower(),
            str(r.get("exam_date") or "").strip(),
            str(r.get("start_time") or "").strip(),
            str(r.get("exam_room") or "").strip().lower(),
            str(r.get("exam_type") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def validate_exam_tab_structure(html: str, tab_arg: str) -> bool:
    """
    Verify exact expected tab container/table exists in the HTML page after postback.
    tab_arg "0" -> LichThi1_GiuaKyTable or GiuaKyTable
    tab_arg "1" -> LichThi1_CuoiKyTable or CuoiKyTable
    tab_arg "2" -> LichThi1_CuoiKy2Table or CuoiKy2Table
    Returns True if exact expected table/container exists, False if missing.
    """
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    expected = EXPECTED_EXAM_TABLE_IDS.get(str(tab_arg), set())

    for tag in soup.find_all(["table", "div", "span"]):
        tag_id = (tag.get("id") or "").lower()
        tag_name = (tag.get("name") or "").lower()
        if tag_id in expected or tag_name in expected:
            return True

    return False

