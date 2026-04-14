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
        schedule = fetch_schedule()
        logger.info("Crawler returned %d schedule entries.", len(schedule))
    except Exception as exc:
        _handle_fatal("Crawler failed", exc)
        return

    # ------------------------------------------------------------------
    # Step 2 – Persist the fresh schedule to Supabase
    # ------------------------------------------------------------------
    logger.info("=== Step 2: Updating schedule in Supabase ===")
    try:
        from database import upsert_schedule
        upsert_schedule(schedule, student_id=student_id)
        logger.info("Supabase update complete.")
    except Exception as exc:
        _handle_fatal("Database update failed", exc)
        return

    # ------------------------------------------------------------------
    # Step 3 – Fetch today's classes
    # ------------------------------------------------------------------
    logger.info("=== Step 3: Fetching today's classes from Supabase ===")
    try:
        from database import get_today_schedule
        today_classes = get_today_schedule(student_id=student_id)
        logger.info("Today has %d class(es).", len(today_classes))
    except Exception as exc:
        _handle_fatal("Failed to fetch today's schedule", exc)
        return

    # ------------------------------------------------------------------
    # Step 4 – Send Telegram notification
    # ------------------------------------------------------------------
    logger.info("=== Step 4: Sending Telegram notification ===")
    try:
        from notifier import send_today_schedule
        send_today_schedule(today_classes)
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
