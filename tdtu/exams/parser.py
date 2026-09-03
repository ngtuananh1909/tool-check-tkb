"""
Pure HTML parser for TDTU student exam schedule pages.
Does NOT access network, environment variables, or external services.
"""

import logging
import re
from typing import Any
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def parse_date_iso(text: str) -> str:
    """Parse date from string like '15/09/2026' or '15-09-2026' into 'YYYY-MM-DD'."""
    m = re.search(r"(\d{1,2})[/\.-](\d{1,2})(?:[/\.-](\d{2,4}))?", text or "")
    if not m:
        return ""
    d = int(m.group(1))
    mo = int(m.group(2))
    y = int(m.group(3)) if m.group(3) else 2026
    if y < 100:
        y += 2000
    if not (1 <= d <= 31 and 1 <= mo <= 12 and y > 2000):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_time_str(text: str) -> str:
    """Parse time string like '07:30' or '7h30' into 'HH:MM'."""
    m = re.search(r"(\d{1,2})[:h](\d{2})", text or "", re.IGNORECASE)
    if not m:
        return ""
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_exam_html(html: str, default_exam_type: str = "") -> list[dict[str, Any]]:
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
            date_iso = parse_date_iso(date_text)
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

        date_iso = parse_date_iso(date_line)
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
    """Deduplicate exam items by subject_name, exam_date, start_time, and exam_room."""
    seen = set()
    deduped = []
    for r in rows:
        key = (
            r.get("subject_name"),
            r.get("exam_date"),
            r.get("start_time"),
            r.get("exam_room"),
            r.get("exam_type"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped
