"""
Schedule service for orchestrating HTTP timetable retrieval and semester selection.
"""

import logging
from typing import Any

from tdtu.client import TDTUClient
from tdtu.exceptions import TDTUProtocolError
from tdtu.schedule.parser import (
    _deduplicate_schedule,
    parse_active_semester,
    parse_general_schedule_table,
    parse_schedule_html,
    parse_semester_options,
    parse_weekly_grid_table,
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
    If max_weeks > 0 (or multi-week requested), switches to weekly view FIRST
    and iterates weekly postbacks without mixing general schedule rows.
    """
    page = client.open_schedule_page()
    initial_html = page.html
    sid = client.student_id

    # 1. Semester selection check
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
            initial_html = page.html

    # 2. Multi-week crawl check (Blocker 4)
    # If max_weeks > 1 is requested, attempt weekly view.
    if max_weeks and max_weeks > 1 and "btnTuanSau" in page.html:
        logger.info("[tdtu.schedule] Multi-week crawl requested (max_weeks=%d). Switching to weekly view...", max_weeks)
        if "radXemTKBTheoTuan" in page.html:
            page.postback(
                event_target="ThoiKhoaBieu1$radXemTKBTheoTuan",
                extra={"ThoiKhoaBieu1$radChonLua": "radXemTKBTheoTuan"},
            )

        entries: list[dict[str, Any]] = []

        # Parse initial week
        week_entries = parse_weekly_grid_table(page.html, student_id=sid)
        if week_entries is not None:
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
            w_entries = parse_weekly_grid_table(page.html, student_id=sid)
            if w_entries:
                entries.extend(w_entries)

        if entries:
            deduped = _deduplicate_schedule(entries)
            logger.info("[tdtu.schedule] Fetched %d weekly schedule entries via HTTP", len(deduped))
            return deduped

        logger.info("[tdtu.schedule] Weekly grid view empty or unnavigable; using general schedule parser...")

    # Single-week or general schedule crawl fallback
    entries = parse_schedule_html(initial_html, student_id=sid)
    deduped = _deduplicate_schedule(entries)
    logger.info("[tdtu.schedule] Fetched %d schedule entries via HTTP", len(deduped))
    return deduped
