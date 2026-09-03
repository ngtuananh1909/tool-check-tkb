"""
Schedule service for orchestrating HTTP timetable retrieval and semester selection.
"""

import logging
from typing import Any

from tdtu.client import TDTUClient
from tdtu.schedule.parser import (
    parse_active_semester,
    parse_schedule_html,
    parse_semester_options,
)

logger = logging.getLogger(__name__)


def get_current_semester_http(client: TDTUClient) -> str:
    """Retrieve current active semester string from portal via HTTP."""
    page = client.open_schedule_page()
    semester = parse_active_semester(page.html)
    logger.info("[tdtu.schedule] Active semester resolved: %s", semester)
    return semester


def fetch_schedule_http(
    client: TDTUClient,
    selected_semester: str | None = None,
    max_weeks: int | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch schedule entries via authenticated HTTP.
    Optionally switches semester if selected_semester is specified.
    Optionally navigates future weeks if max_weeks > 1.
    """
    page = client.open_schedule_page()
    sid = client.student_id

    # Semester selection check
    if selected_semester:
        options = parse_semester_options(page.html)
        target_opt = None
        for opt in options:
            if selected_semester.lower() in opt["text"].lower() or opt["value"] == selected_semester:
                target_opt = opt
                break

        if target_opt and not target_opt.get("selected"):
            logger.info("[tdtu.schedule] Switching semester to: %s (value=%s)", target_opt["text"], target_opt["value"])
            page.postback(
                event_target="ThoiKhoaBieu1$cboHocKy",
                extra={"ThoiKhoaBieu1$cboHocKy": target_opt["value"]},
            )

    entries = parse_schedule_html(page.html, student_id=sid)

    # If max_weeks is specified and > 1, check if weekly view postback is available
    if max_weeks and max_weeks > 1:
        # Check if weekly view radio button is present
        if "radXemTKBTheoTuan" in page.html:
            logger.info("[tdtu.schedule] Switching to weekly timetable view...")
            page.postback(
                event_target="ThoiKhoaBieu1$radXemTKBTheoTuan",
                extra={"ThoiKhoaBieu1$radChonLua": "radXemTKBTheoTuan"},
            )
            week_entries = parse_schedule_html(page.html, student_id=sid)
            entries.extend(week_entries)

            # Navigate future weeks
            for week_idx in range(1, max_weeks):
                if "btnTuanSau" not in page.html:
                    break
                logger.debug("[tdtu.schedule] Navigating to week +%d", week_idx)
                page.postback(
                    event_target="",
                    extra={"ThoiKhoaBieu1$btnTuanSau": ">>"},
                )
                w_entries = parse_schedule_html(page.html, student_id=sid)
                entries.extend(w_entries)

    # Deduplicate combined entries
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
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    logger.info("[tdtu.schedule] Fetched %d schedule entries via HTTP", len(deduped))
    return deduped
