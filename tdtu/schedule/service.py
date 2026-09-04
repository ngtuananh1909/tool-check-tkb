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
    if not semester:
        raise TDTUProtocolError("Active semester string could not be resolved from portal page")
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
    Switches to weekly view FIRST before iterating future weeks without mixing general schedule rows.
    """
    page = client.open_schedule_page()
    sid = client.student_id

    # 1. Semester selection check
    if selected_semester:
        options = parse_semester_options(page.html)
        if not options:
            raise TDTUProtocolError(f"Requested schedule semester {selected_semester!r} but dropdown control is missing on page")

        target_opt = None
        for opt in options:
            if selected_semester.lower() in opt["text"].lower() or opt["value"] == selected_semester:
                target_opt = opt
                break

        if not target_opt:
            raise TDTUProtocolError(f"Requested schedule semester {selected_semester!r} not found on page")

        if not target_opt.get("selected"):
            logger.info("[tdtu.schedule] Switching semester to: %s (value=%s)", target_opt["text"], target_opt["value"])
            page.postback(
                event_target="ThoiKhoaBieu1$cboHocKy",
                extra={"ThoiKhoaBieu1$cboHocKy": target_opt["value"]},
            )
            # Re-verify selected semester (Bug 14)
            current_sem = parse_active_semester(page.html)
            if selected_semester.lower() not in current_sem.lower():
                raise TDTUProtocolError(
                    f"Schedule semester postback failed to set {selected_semester!r} (active: {current_sem!r})"
                )

    # 2. Weekly Schedule Crawl (Bug 2, 3, 4)
    # Switch to weekly view FIRST before checking btnTuanSau or week buttons
    if "radXemTKBTheoTuan" in page.html:
        logger.info("[tdtu.schedule] Switching to weekly view...")
        page.postback(
            event_target="ThoiKhoaBieu1$radXemTKBTheoTuan",
            extra={"ThoiKhoaBieu1$radChonLua": "radXemTKBTheoTuan"},
        )

        entries: list[dict[str, Any]] = []

        # Parse initial week grid
        week_entries = parse_weekly_grid_table(page.html, student_id=sid)
        if week_entries is not None:
            entries.extend(week_entries)

            # Navigate future weeks (BUG 3: check btnTuanSau AFTER weekly postback!)
            weeks_to_fetch = max_weeks if (max_weeks and max_weeks > 1) else 1
            for week_idx in range(1, weeks_to_fetch):
                if "btnTuanSau" not in page.html:
                    break
                logger.debug("[tdtu.schedule] Navigating to week +%d via btnTuanSau", week_idx)
                page.postback(
                    event_target="ThoiKhoaBieu1$btnTuanSau",
                    extra={"ThoiKhoaBieu1$btnTuanSau": ">>"},
                )
                w_entries = parse_weekly_grid_table(page.html, student_id=sid)
                if w_entries:
                    entries.extend(w_entries)

            deduped = _deduplicate_schedule(entries)
            # BUG 4: A valid weekly page returning [] is SUCCESS! Return [] without falling back to general schedule!
            logger.info("[tdtu.schedule] Fetched %d weekly schedule entries via HTTP", len(deduped))
            return deduped

    # Fallback for general schedule view when weekly view control is absent
    entries = parse_schedule_html(page.html, student_id=sid)
    deduped = _deduplicate_schedule(entries)
    logger.info("[tdtu.schedule] Fetched %d general schedule entries via HTTP", len(deduped))
    return deduped
