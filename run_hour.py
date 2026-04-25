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


def run_hourly_sync() -> None:
    """Execute hourly crawl, DB sync, and calendar sync pipeline."""
    _load_dotenv()
    
    student_id = os.environ.get("STUDENT_ID")
    
    # -------- Step 1: Crawl --------
    logger.info("=== Step 1: Crawling schedule from TDTU portal ===")
    try:
        from crawler import (
            fetch_elearning_progress,
            fetch_exam_schedule,
            fetch_schedule,
        )
        weeks_ahead = _resolve_crawler_weeks_ahead()
        logger.info("Crawler will fetch current week + %d future week(s).", weeks_ahead)
        schedule = fetch_schedule(weeks_ahead=weeks_ahead)
        logger.info("Crawler returned %d schedule entries.", len(schedule))

        exams = fetch_exam_schedule(weeks_ahead=weeks_ahead)
        logger.info("Exam crawler returned %d exam row(s).", len(exams))

        elearning_progress = fetch_elearning_progress()
        logger.info("eLearning crawler returned %d progress row(s).", len(elearning_progress))
    except Exception as exc:
        _handle_error("Crawler failed", exc)
        return
    
    # -------- Step 2: DB Sync --------
    logger.info("=== Step 2: Updating schedule in Supabase ===")
    try:
        from database import (
            materialize_class_sessions,
            upsert_elearning_progress,
            upsert_exams,
            upsert_actual_class_sessions,
            upsert_schedule,
        )
        upsert_schedule(schedule, student_id=student_id)
        materialized = upsert_actual_class_sessions(schedule, student_id=student_id)
        if materialized == 0:
            logger.warning(
                "Crawler did not return concrete session_date rows; using generated fallback class sessions."
            )
            materialized = materialize_class_sessions(schedule, student_id=student_id)

        exam_rows = upsert_exams(exams, student_id=student_id)
        progress_rows = upsert_elearning_progress(elearning_progress, student_id=student_id)
        logger.info("Supabase update complete.")
        logger.info("Class sessions materialized: %d row(s).", materialized)
        logger.info("Exams upserted: %d row(s).", exam_rows)
        logger.info("eLearning progress upserted: %d row(s).", progress_rows)
    except Exception as exc:
        _handle_error("Database update failed", exc)
        return
    
    # -------- Step 3: Fetch full data --------
    logger.info("=== Step 3: Fetching full sync data from Supabase ===")
    try:
        from database import (
            get_all_appointments,
            get_all_class_sessions,
            get_upcoming_exams,
            get_all_schedule,
        )
        all_schedule_rows = get_all_class_sessions(student_id=student_id)
        if not all_schedule_rows:
            logger.warning(
                "No class sessions found for full sync; falling back to weekly schedule rows for calendar sync."
            )
            all_schedule_rows = get_all_schedule(student_id=student_id)
        all_appointments = get_all_appointments(student_id=student_id)
        all_exams = get_upcoming_exams(student_id=student_id, days_ahead=180)
        logger.info("Full class dataset has %d row(s).", len(all_schedule_rows))
        logger.info("Full appointment set has %d row(s).", len(all_appointments))
        logger.info("Full exam set has %d row(s).", len(all_exams))
    except Exception as exc:
        _handle_error("Failed to fetch full data", exc)
        return
    
    # -------- Step 4: Calendar sync & CSV export --------
    logger.info("=== Step 4: Exporting CSV and syncing Google Calendar ===")
    try:
        from calendar_sync import sync_database_to_csv_and_google_calendar
        
        csv_path, did_sync = sync_database_to_csv_and_google_calendar(
            all_schedule_rows,
            all_appointments,
            exams=all_exams,
            student_id=student_id,
        )
        logger.info("CSV export complete: %s", csv_path)
        if did_sync:
            logger.info("Google Calendar sync complete.")
        else:
            logger.info(
                "Google Calendar sync skipped (missing GOOGLE_CALENDAR_ID or Google service-account credentials)."
            )
    except Exception as exc:
        _handle_error("CSV export / Google Calendar sync failed", exc)
        return
    
    logger.info("=== Hourly data collection and sync complete. ===")


if __name__ == "__main__":
    run_hourly_sync()
