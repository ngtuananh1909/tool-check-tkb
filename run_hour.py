"""
run_hour.py – Hourly data collection and sync orchestrator.

This script runs steps 1-4 of the schedule-notification pipeline:
    1. Crawl the TDTU portal for the latest timetable.
    2. Upsert the timetable into Supabase.
    3. Fetch full dataset from Supabase for sync preparation.
    4. Export CSV and sync to Google Calendar.

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
    """Execute hourly crawl, DB sync, and calendar sync pipeline."""
    _load_dotenv()

    student_id = os.environ.get("STUDENT_ID")

    # -------- Step 1: Crawl --------
    step_started = time.perf_counter()
    logger.info("Step 1: Crawling schedule from TDTU portal")
    try:
        from crawler import (
            fetch_elearning_deadlines,
            fetch_elearning_progress,
            fetch_exam_schedule,
            fetch_schedule,
        )
        weeks_ahead = _resolve_crawler_weeks_ahead()
        logger.debug("Crawler will fetch current week + %d future week(s).", weeks_ahead)
        schedule = fetch_schedule(weeks_ahead=weeks_ahead)
        logger.debug("Crawler returned %d schedule entries.", len(schedule))

        exams = fetch_exam_schedule(weeks_ahead=weeks_ahead)
        logger.debug("Exam crawler returned %d exam row(s).", len(exams))

        elearning_progress = fetch_elearning_progress()
        logger.debug("eLearning crawler returned %d progress row(s).", len(elearning_progress))

        elearning_deadlines = fetch_elearning_deadlines()
        logger.debug("eLearning deadline crawler returned %d row(s).", len(elearning_deadlines))
    except Exception as exc:
        logger.exception("Step 1 failed after %.2fs", time.perf_counter() - step_started)
        _handle_error("Crawler failed", exc)
        return
    _log_step_elapsed("Step 1", step_started)

    # -------- Step 2: DB Sync (eLearning only) --------
    step_started = time.perf_counter()
    logger.info("Step 2: Updating eLearning data in Supabase")
    try:
        from database import (
            upsert_elearning_deadlines,
            upsert_elearning_progress,
        )
        progress_rows = upsert_elearning_progress(elearning_progress, student_id=student_id)
        deadline_rows = upsert_elearning_deadlines(elearning_deadlines, student_id=student_id)
        logger.debug("eLearning progress upserted: %d row(s).", progress_rows)
        logger.debug("eLearning deadlines upserted: %d row(s).", deadline_rows)
    except Exception as exc:
        logger.exception("Step 2 failed after %.2fs", time.perf_counter() - step_started)
        _handle_error("Database update failed", exc)
        return
    _log_step_elapsed("Step 2", step_started)

    # -------- Step 3: Direct Google Calendar sync --------
    step_started = time.perf_counter()
    logger.info("Step 3: Syncing raw data directly to Google Calendar")
    try:
        from calendar_sync import sync_crawled_data_to_google_calendar

        _, did_sync = sync_crawled_data_to_google_calendar(
            schedule,
            exams,
            student_id=student_id,
        )
        if did_sync:
            logger.info("Google Calendar sync complete.")
        else:
            logger.info(
                "Google Calendar sync skipped (missing GOOGLE_CALENDAR_ID or Google service-account credentials)."
            )
    except Exception as exc:
        logger.exception("Step 3 failed after %.2fs", time.perf_counter() - step_started)
        _handle_error("Google Calendar sync failed", exc)
        return
    _log_step_elapsed("Step 3", step_started)

    logger.info("=== Hourly data collection and sync complete. ===")


if __name__ == "__main__":
    run_hourly_sync()
