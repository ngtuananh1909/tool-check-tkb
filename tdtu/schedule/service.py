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
    Weekly view is authoritative: raises TDTUProtocolError if weekly control is missing
    or grid table is missing/malformed.
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
            if opt["value"] == selected_semester or selected_semester.lower() in opt["text"].lower():
                target_opt = opt
                break

        if not target_opt:
            raise TDTUProtocolError(f"Requested schedule semester {selected_semester!r} not found in dropdown options")

        if not target_opt.get("selected"):
            logger.info("[tdtu.schedule] Switching semester to: %s (value=%s)", target_opt["text"], target_opt["value"])
            page.postback(
                event_target="ThoiKhoaBieu1$cboHocKy",
                extra={"ThoiKhoaBieu1$cboHocKy": target_opt["value"]},
            )

        # Strict semester re-verification
        options_after = parse_semester_options(page.html)
        if not options_after:
            raise TDTUProtocolError("Schedule semester select control missing after postback")
        selected_opt = next((o for o in options_after if o.get("selected")), None)
        if not selected_opt:
            raise TDTUProtocolError("No schedule semester option is currently selected after postback")

        sem_val = selected_opt["value"]
        sem_txt = selected_opt["text"].strip()
        matched = (
            sem_val == selected_semester
            or selected_semester.lower() in sem_txt.lower()
            or sem_val == target_opt["value"]
        )
        if not matched:
            raise TDTUProtocolError(
                f"Schedule semester postback failed to select {selected_semester!r} (currently selected: {sem_txt!r}, value={sem_val!r})"
            )

    # 2. Weekly Schedule Crawl (Authoritative - no general schedule fallback)
    if "radXemTKBTheoTuan" not in page.html:
        raise TDTUProtocolError("Weekly schedule control 'radXemTKBTheoTuan' missing from schedule page")

    logger.info("[tdtu.schedule] Switching to weekly view...")
    page.postback(
        event_target="ThoiKhoaBieu1$radXemTKBTheoTuan",
        extra={"ThoiKhoaBieu1$radChonLua": "radXemTKBTheoTuan"},
    )

    # Parse initial week grid
    week_entries = parse_weekly_grid_table(page.html, student_id=sid)
    if week_entries is None:
        raise TDTUProtocolError("Weekly schedule grid table missing or malformed on initial weekly view page")

    entries: list[dict[str, Any]] = list(week_entries)

    # Navigate future weeks
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
        if w_entries is None:
            raise TDTUProtocolError(f"Weekly schedule grid table missing or malformed on week +{week_idx} postback page")
        entries.extend(w_entries)

    deduped = _deduplicate_schedule(entries)
    logger.info("[tdtu.schedule] Fetched %d weekly schedule entries via HTTP", len(deduped))
    return deduped
