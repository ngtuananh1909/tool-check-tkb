"""
run_hour.py – Hourly data collection and sync orchestrator.

This script runs the data-collection pipeline:
    1. Crawl the TDTU portal for the latest timetable.
    2. Push raw crawler results directly to Google Calendar.
    3. Finish after Calendar reconciliation.

Can be scheduled to run hourly via cron, Railway Scheduled Jobs, or similar.
This does NOT send Telegram notifications; that's handled separately by main.py.
"""

import logging
import os
import sys
import time

# -----------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load .env file into os.environ when running locally."""
    try:
        from dotenv import load_dotenv  # type: ignore[import]
        loaded = load_dotenv()
        if loaded:
            logger.info(".env file loaded successfully.")
    except ImportError:
        pass


def _resolve_crawler_weeks_ahead() -> int:
    """Read multi-week crawl horizon from env with safe fallback."""
    raw = (os.environ.get("CRAWLER_WEEKS_AHEAD") or "2").strip()
    try:
        weeks = int(raw)
    except ValueError:
        logger.warning("Invalid CRAWLER_WEEKS_AHEAD=%r; using 2.", raw)
        return 2
    if weeks < 0:
        return 0
    return min(weeks, 12)


def _handle_error(context: str, exc: Exception) -> None:
    """Log error and send alert if possible."""
    error_msg = f"{context}: {exc}"
    logger.exception(error_msg)

    try:
        from notifier import send_error_alert
        send_error_alert(error_msg)
    except Exception as alert_exc:
        logger.error("Could not send error alert: %s", alert_exc)

    sys.exit(1)


def _log_step_elapsed(step_name: str, started_at: float) -> None:
    """Log how long a step took in seconds."""
    elapsed = time.perf_counter() - started_at
    logger.info("%s finished in %.2fs", step_name, elapsed)


def run_hourly_sync() -> None:
    """Execute the crawler and synchronize successful results to Calendar."""
    _load_dotenv()

    student_id = os.environ.get("STUDENT_ID")
    password = os.environ.get("PASSWORD")

    # -------- Pre-check & Step 1: Crawl using shared portal snapshot (1 login) --------
    step_started = time.perf_counter()
    logger.info("Step 1: Crawling schedule & exam data from TDTU portal")
    try:
        from crawler import fetch_elearning_deadlines
        from tdtu import fetch_portal_snapshot

        weeks_ahead = _resolve_crawler_weeks_ahead()
        logger.debug("Crawler will fetch current week + %d future week(s).", weeks_ahead)

        schedule = None
        exams = None

        # Attempt shared single-login HTTP snapshot
        snapshot = fetch_portal_snapshot(student_id, password, weeks_ahead=1 + weeks_ahead)

        # 1. Semester logging
        if snapshot.semester.success and snapshot.semester.data:
            logger.info("Active semester on portal: %s", snapshot.semester.data)
        else:
            try:
                from crawler import _get_current_semester_playwright
                sem = _get_current_semester_playwright(student_id, password)
                logger.info("Active semester on portal (Playwright fallback): %s", sem)
            except Exception as exc:
                logger.warning("Could not determine active semester: %s", exc)

        # 2. Schedule operation
        if snapshot.schedule.success:
            schedule = snapshot.schedule.data
            logger.info("Schedule HTTP crawler returned %d row(s).", len(schedule) if schedule is not None else 0)
        else:
            logger.warning("Schedule HTTP crawler failed (%s); triggering Playwright fallback...", snapshot.schedule.error)
            try:
                from crawler import _fetch_schedule_playwright
                schedule = _fetch_schedule_playwright(student_id, password, weeks_ahead=weeks_ahead)
                logger.info("Schedule Playwright fallback returned %d row(s).", len(schedule))
            except Exception:
                logger.exception("Schedule Playwright fallback failed; preserving existing schedule data.")
                schedule = None

        # 3. Exam operation
        if snapshot.exams.success:
            exams = snapshot.exams.data
            logger.info("Exam HTTP crawler returned %d row(s).", len(exams) if exams is not None else 0)
        else:
            logger.warning("Exam HTTP crawler failed (%s); triggering Playwright fallback...", snapshot.exams.error)
            try:
                from crawler import _fetch_exam_schedule_from_portal
                exams = _fetch_exam_schedule_from_portal(student_id, password, weeks_ahead=weeks_ahead)
                logger.info("Exam Playwright fallback returned %d row(s).", len(exams))
            except Exception:
                logger.exception("Exam Playwright fallback failed; preserving existing exam data.")
                exams = None

        elearning_deadlines = None
        try:
            elearning_deadlines = fetch_elearning_deadlines()
            logger.info("eLearning deadline crawler returned %d row(s).", len(elearning_deadlines))
        except Exception:
            logger.exception("eLearning deadline crawl failed; continuing without a deadline update.")
    except Exception as exc:
        logger.exception("Step 1 failed after %.2fs", time.perf_counter() - step_started)
        _handle_error("Crawler failed", exc)
        return
    _log_step_elapsed("Step 1", step_started)

    # -------- Step 2: Direct Google Calendar sync --------
    # Use raw crawler results so Calendar is updated immediately after crawling.
    step_started = time.perf_counter()
    logger.info("Step 2: Syncing raw crawler data directly to Google Calendar")
    try:
        from calendar_sync import sync_crawled_data_to_google_calendar

        _, did_sync = sync_crawled_data_to_google_calendar(
            schedule,
            exams,
            student_id=student_id,
            deadlines=elearning_deadlines,
        )
        if did_sync:
            logger.info("Google Calendar sync complete.")
        else:
            logger.info(
                "Google Calendar sync skipped (missing GOOGLE_CALENDAR_ID or Google service-account credentials)."
            )
    except Exception as exc:
        logger.exception("Step 2 failed after %.2fs", time.perf_counter() - step_started)
        _handle_error("Google Calendar sync failed", exc)
        return
    _log_step_elapsed("Step 2", step_started)

    logger.info("=== Hourly data collection and sync complete. ===")


if __name__ == "__main__":
    run_hourly_sync()
