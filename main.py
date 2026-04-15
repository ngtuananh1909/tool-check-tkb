"""
main.py – Orchestrator for the TDTU daily schedule notification system.

Execution flow:
    1. Load environment variables from a local .env file (if present).
    2. Run the Playwright crawler to fetch the latest timetable.
    3. Upsert the timetable into Supabase.
    4. Query today's classes from Supabase.
    5. Send a Telegram notification with the day's schedule.

On any failure, a Telegram error alert is sent so that issues are surfaced
immediately even when running headlessly in CI.
"""

import logging
import os
import sys

# ---------------------------------------------------------------------------
# Logging – set up first so every module inherits the same configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """
    Load .env file into os.environ when running locally.
    This is a no-op in CI because secrets are injected as real env vars.
    """
    try:
        from dotenv import load_dotenv  # type: ignore[import]
        loaded = load_dotenv()
        if loaded:
            logger.info(".env file loaded successfully.")
    except ImportError:
        # python-dotenv is an optional dev dependency; skip silently in CI
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


def main() -> None:
    """Run the full schedule-notification pipeline."""
    _load_dotenv()

    student_id = os.environ.get("STUDENT_ID")

    # ------------------------------------------------------------------
    # Step 1 – Crawl the portal for the current timetable
    # ------------------------------------------------------------------
    logger.info("=== Step 1: Crawling schedule from TDTU portal ===")
    try:
        from crawler import fetch_schedule
        weeks_ahead = _resolve_crawler_weeks_ahead()
        logger.info("Crawler will fetch current week + %d future week(s).", weeks_ahead)
        schedule = fetch_schedule(weeks_ahead=weeks_ahead)
        logger.info("Crawler returned %d schedule entries.", len(schedule))
    except Exception as exc:
        _handle_fatal("Crawler failed", exc)
        return

    # ------------------------------------------------------------------
    # Step 2 – Persist the fresh schedule to Supabase
    # ------------------------------------------------------------------
    logger.info("=== Step 2: Updating schedule in Supabase ===")
    try:
        from database import (
            materialize_class_sessions,
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
        logger.info("Supabase update complete.")
        logger.info("Class sessions materialized: %d row(s).", materialized)
    except Exception as exc:
        _handle_fatal("Database update failed", exc)
        return

    # ------------------------------------------------------------------
    # Step 3 – Fetch today's classes and appointments, plus full sync data
    # ------------------------------------------------------------------
    logger.info("=== Step 3: Fetching today's data and full sync data from Supabase ===")
    try:
        from database import (
            get_all_appointments,
            get_all_class_sessions,
            get_all_schedule,
            get_today_appointments,
            get_today_class_sessions,
            get_today_schedule,
        )
        today_classes = get_today_class_sessions(student_id=student_id)
        if not today_classes:
            logger.warning(
                "No class sessions found for today; falling back to weekly schedule rows for notifications."
            )
            today_classes = get_today_schedule(student_id=student_id)

        today_appointments = get_today_appointments(student_id=student_id)
        all_schedule_rows = get_all_class_sessions(student_id=student_id)
        if not all_schedule_rows:
            logger.warning(
                "No class sessions found for full sync; falling back to weekly schedule rows for calendar sync."
            )
            all_schedule_rows = get_all_schedule(student_id=student_id)
        all_appointments = get_all_appointments(student_id=student_id)
        logger.info("Today has %d class(es).", len(today_classes))
        logger.info("Today has %d appointment(s).", len(today_appointments))
        logger.info("Full class dataset has %d row(s).", len(all_schedule_rows))
        logger.info("Full appointment set has %d row(s).", len(all_appointments))
    except Exception as exc:
        _handle_fatal("Failed to fetch today's data", exc)
        return

    # ------------------------------------------------------------------
    # Step 4 – Export CSV and sync to Google Calendar
    # ------------------------------------------------------------------
    logger.info("=== Step 4: Exporting CSV and syncing Google Calendar ===")
    try:
        from calendar_sync import sync_database_to_csv_and_google_calendar

        csv_path, did_sync = sync_database_to_csv_and_google_calendar(
            all_schedule_rows,
            all_appointments,
            student_id=student_id,
        )
        logger.info("CSV export complete: %s", csv_path)
        if did_sync:
            logger.info("Google Calendar sync complete.")
        else:
            logger.info(
                "Google Calendar sync skipped (missing GOOGLE_CALENDAR_ID or GOOGLE_SERVICE_ACCOUNT_JSON)."
            )
    except Exception as exc:
        _handle_fatal("CSV export / Google Calendar sync failed", exc)
        return

    # ------------------------------------------------------------------
    # Step 5 – Send Telegram notification
    # ------------------------------------------------------------------
    logger.info("=== Step 5: Sending Telegram notification ===")
    try:
        from notifier import send_daily_summary
        send_daily_summary(today_classes, today_appointments)
        logger.info("Notification sent. Pipeline complete.")
    except Exception as exc:
        _handle_fatal("Telegram notification failed", exc)
        return

    logger.info("=== All steps completed successfully. ===")


def _handle_fatal(context: str, exc: Exception) -> None:
    """
    Log the error and send a Telegram alert, then allow the caller to return
    so the process exits cleanly (with sys.exit later if needed).
    """
    error_msg = f"{context}: {exc}"
    logger.exception(error_msg)

    try:
        from notifier import send_error_alert
        send_error_alert(error_msg)
    except Exception as alert_exc:
        logger.error("Could not send error alert: %s", alert_exc)

    # Exit with a non-zero code so GitHub Actions marks the run as failed
    sys.exit(1)


if __name__ == "__main__":
    main()
