"""
Pure HTML parser for TDTU student exam schedule pages.
Does NOT access network, environment variables, or external services.
"""

import datetime
import logging
import re
from typing import Any
from bs4 import BeautifulSoup

from time_utils import local_today

logger = logging.getLogger(__name__)


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


def parse_exam_html(html: str, default_exam_type: str = "", semester_hint: str = "") -> list[dict[str, Any]]:
    """
    Parse exam schedule HTML and extract exam records.
    Parses table structures (#LichThi1_GiuaKyTable, #LichThi1_CuoiKyTable, etc.) and grid cell blocks.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []

    # 1. Parse standard tables
    tables = soup.find_all("table")
    for table in tables:
        head_cells = table.find_all(["th", "td"])
        headers = [c.get_text().strip().lower() for c in head_cells[:15]]
        all_head = " ".join(headers)

        has_subject = bool(re.search(r"(môn|mon|subject)", all_head))
        has_date = bool(re.search(r"(ngày|ngay|date)", all_head))
        has_time = bool(re.search(r"(giờ|gio|time)", all_head))

        if not has_subject or not (has_date or has_time):
            continue

        idx_subject = next((i for i, h in enumerate(headers) if re.search(r"(môn|mon|subject)", h)), -1)
        idx_date = next((i for i, h in enumerate(headers) if re.search(r"(ngày|ngay|date)", h)), -1)
        idx_time = next((i for i, h in enumerate(headers) if re.search(r"(giờ|gio|time)", h)), -1)
        idx_room = next((i for i, h in enumerate(headers) if re.search(r"(phòng|phong|room)", h)), -1)
        idx_type = next((i for i, h in enumerate(headers) if re.search(r"(hình thức|hinh thuc|type|loại|loai)", h)), -1)

        trs = table.find_all("tr")[1:]
        for tr in trs:
            tds = [td.get_text().strip() for td in tr.find_all("td")]
            if not tds:
                continue

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

    # 2. Parse grid cell blocks containing "Ngày thi:" / "Giờ thi:"
    cells = soup.find_all(["td", "div"])
    for cell in cells:
        text = cell.get_text("\n").strip()
        if not text:
            continue
        lowered = text.lower()
        if not ("ngày thi" in lowered or "ngay thi" in lowered or "date:" in lowered):
            continue
        if not ("giờ thi" in lowered or "gio thi" in lowered or "time:" in lowered):
            continue

        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            continue

        subject = lines[0].split("|")[0].strip()
        if not subject:
            continue

        date_line = next((line for line in lines if re.search(r"(ngày|ngay|date)", line, re.IGNORECASE)), text)
        time_line = next((line for line in lines if re.search(r"(giờ|gio|time)", line, re.IGNORECASE)), text)
        room_line = next((line for line in lines if re.search(r"(phòng|phong|room)", line, re.IGNORECASE)), "")

        date_iso = parse_date_iso(date_line, semester_hint=semester_hint)
        if not date_iso:
            continue

        start_t = parse_time_str(time_line)
        end_t = ""
        range_m = re.search(r"(\d{1,2}[:h]\d{2})\s*(?:-|–|—|to|đến|den|->|~)\s*(\d{1,2}[:h]\d{2})", time_line, re.IGNORECASE)
        if range_m:
            end_t = parse_time_str(range_m.group(2))

        room = ""
        room_m = re.search(r"(?:phòng|phong|room)\s*[:\-]?\s*(.+)$", room_line, re.IGNORECASE)
        if room_m:
            room = room_m.group(1).strip()

        rows.append({
            "subject_name": subject,
            "exam_date": date_iso,
            "start_time": start_t,
            "end_time": end_t,
            "exam_room": room,
            "exam_type": default_exam_type,
            "notes": "Crawled from exam grid",
        })

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
    Verify expected tab container/table exists in the HTML page after postback.
    tab_arg "0" -> GiuaKyTable (or LichThi1_GiuaKyTable)
    tab_arg "1" -> CuoiKyTable (or LichThi1_CuoiKyTable)
    tab_arg "2" -> CuoiKy2Table (or LichThi1_CuoiKy2Table)
    Returns True if structurally valid expected table/container exists, False if missing.
    """
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")

    target_ids = {
        "0": ["lichthi1_giuakytable", "giuakytable", "giuaky"],
        "1": ["lichthi1_cuoikytable", "cuoikytable", "cuoiky"],
        "2": ["lichthi1_cuoiky2table", "cuoiky2table", "cuoiky2"],
    }
    kws = target_ids.get(str(tab_arg), ["table"])

    for tag in soup.find_all(["table", "div", "span"]):
        tag_id = (tag.get("id") or "").lower()
        tag_name = (tag.get("name") or "").lower()
        if any(kw in tag_id or kw in tag_name for kw in kws):
            return True

    return False

