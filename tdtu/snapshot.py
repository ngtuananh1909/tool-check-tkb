"""
Portal Snapshot Service for shared single-login portal sync cycles.
Ensures one authentication session per hourly sync.
"""

from dataclasses import dataclass, field
import logging
from typing import Any

from tdtu.client import TDTUClient
from tdtu.exceptions import TDTUError
from tdtu.exams.service import fetch_exam_schedule_http
from tdtu.schedule.service import fetch_schedule_http, get_current_semester_http

logger = logging.getLogger(__name__)


@dataclass
class PortalSnapshot:
    """Represents a complete authenticated crawl snapshot from TDTU portal."""

    semester: str = ""
    schedule: list[dict[str, Any]] = field(default_factory=list)
    exams: list[dict[str, Any]] = field(default_factory=list)
    source: str = "http"
    success: bool = True
    error: str | None = None


def fetch_portal_snapshot(
    student_id: str,
    password: str,
    weeks_ahead: int | None = None,
    selected_semester: str | None = None,
) -> PortalSnapshot:
    """
    Perform a complete hourly portal sync using ONE login session.
    1. Authenticate once.
    2. Resolve active semester.
    3. Fetch schedule entries.
    4. Fetch exam entries.
    Returns a unified PortalSnapshot object.
    """
    logger.info("[tdtu.snapshot] Starting single-login portal snapshot for ID: %s", student_id)

    try:
        with TDTUClient(student_id=student_id, password=password) as client:
            semester = get_current_semester_http(client)
            schedule = fetch_schedule_http(
                client,
                selected_semester=selected_semester,
                max_weeks=weeks_ahead,
            )
            exams = fetch_exam_schedule_http(
                client,
                selected_semester=selected_semester,
            )

            return PortalSnapshot(
                semester=semester,
                schedule=schedule,
                exams=exams,
                source="http",
                success=True,
                error=None,
            )
    except TDTUError as exc:
        logger.warning("[tdtu.snapshot] HTTP portal snapshot failed: %s", exc)
        return PortalSnapshot(
            source="http",
            success=False,
            error=str(exc),
        )
    except Exception as exc:
        logger.error("[tdtu.snapshot] Unexpected error during HTTP portal snapshot: %s", exc)
        return PortalSnapshot(
            source="http",
            success=False,
            error=f"Unexpected error: {exc}",
        )
