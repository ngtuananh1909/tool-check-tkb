"""
Portal Snapshot Service for shared single-login portal sync cycles.
Ensures one authentication session per hourly sync with per-operation FetchResult models.
"""

from dataclasses import dataclass, field
import logging
from typing import Any, Generic, TypeVar

from tdtu.client import TDTUClient
from tdtu.exceptions import TDTUError
from tdtu.exams.service import fetch_exam_schedule_http
from tdtu.schedule.service import fetch_schedule_http, get_current_semester_http

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class FetchResult(Generic[T]):
    """Represents the outcome of an individual crawler operation."""

    success: bool
    data: T | None
    error: str | None = None
    source: str = "http"


@dataclass
class PortalSnapshot:
    """Represents a complete authenticated crawl snapshot with isolated per-operation results."""

    semester: FetchResult[str] = field(default_factory=lambda: FetchResult(success=False, data=None))
    schedule: FetchResult[list[dict[str, Any]]] = field(default_factory=lambda: FetchResult(success=False, data=None))
    exams: FetchResult[list[dict[str, Any]]] = field(default_factory=lambda: FetchResult(success=False, data=None))


def fetch_portal_snapshot(
    student_id: str,
    password: str,
    weeks_ahead: int | None = None,
    selected_semester: str | None = None,
) -> PortalSnapshot:
    """
    Perform a complete hourly portal sync using ONE login session.
    Isolated per-operation error handling so failure in exams does NOT discard schedule.
    """
    logger.info("[tdtu.snapshot] Starting single-login portal snapshot for ID: %s", student_id)
    snapshot = PortalSnapshot()

    try:
        with TDTUClient(student_id=student_id, password=password) as client:
            # 1. Semester operation
            try:
                sem = get_current_semester_http(client)
                snapshot.semester = FetchResult(success=True, data=sem, source="http")
            except Exception as exc:
                logger.warning("[tdtu.snapshot] Semester HTTP fetch failed: %s", exc)
                snapshot.semester = FetchResult(success=False, data=None, error=str(exc), source="http")

            # 2. Schedule operation
            try:
                sched = fetch_schedule_http(
                    client,
                    selected_semester=selected_semester,
                    max_weeks=weeks_ahead,
                )
                snapshot.schedule = FetchResult(success=True, data=sched, source="http")
            except Exception as exc:
                logger.warning("[tdtu.snapshot] Schedule HTTP fetch failed: %s", exc)
                snapshot.schedule = FetchResult(success=False, data=None, error=str(exc), source="http")

            # 3. Exam operation
            try:
                exams = fetch_exam_schedule_http(
                    client,
                    selected_semester=selected_semester,
                )
                snapshot.exams = FetchResult(success=True, data=exams, source="http")
            except Exception as exc:
                logger.warning("[tdtu.snapshot] Exam HTTP fetch failed: %s", exc)
                snapshot.exams = FetchResult(success=False, data=None, error=str(exc), source="http")

    except TDTUError as exc:
        logger.warning("[tdtu.snapshot] Client authentication / connection failed: %s", exc)
        snapshot.semester = FetchResult(success=False, data=None, error=str(exc), source="http")
        snapshot.schedule = FetchResult(success=False, data=None, error=str(exc), source="http")
        snapshot.exams = FetchResult(success=False, data=None, error=str(exc), source="http")
    except Exception as exc:
        logger.error("[tdtu.snapshot] Unexpected error during portal snapshot: %s", exc)
        err_msg = f"Unexpected error: {exc}"
        snapshot.semester = FetchResult(success=False, data=None, error=err_msg, source="http")
        snapshot.schedule = FetchResult(success=False, data=None, error=err_msg, source="http")
        snapshot.exams = FetchResult(success=False, data=None, error=err_msg, source="http")

    return snapshot
