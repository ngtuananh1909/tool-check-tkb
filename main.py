"""main.py - Daily morning Telegram notification sender.

Execution flow:
    1. Load environment variables from a local .env file (if present).
    2. Query today's classes and appointments from Supabase.
    3. Send a Telegram notification with the day's schedule.

Data collection (crawling, DB sync, calendar sync) is handled separately by run_hour.py.
This script is scheduled to run once daily (typically at midnight) to send the morning briefing.

On any failure, a Telegram error alert is sent so that issues are surfaced immediately.
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
    """Fetch today's schedule and send morning Telegram notification."""
    _load_dotenv()

    student_id = os.environ.get("STUDENT_ID")

    # ------------------------------------------------------------------
    # Debug: Log all relevant env vars
    # ------------------------------------------------------------------
    logger.info("=== ENV VAR CHECK ===")
    logger.info(f"GOOGLE_CALENDAR_ID: {'SET' if os.environ.get('GOOGLE_CALENDAR_ID') else 'NOT SET'}")
    logger.info(f"GOOGLE_SERVICE_ACCOUNT_JSON: {'SET' if os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON') else 'NOT SET'}")
    logger.info(f"GOOGLE_SERVICE_ACCOUNT_FILE: {'SET' if os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') else 'NOT SET'}")
    logger.info(f"APP_TIMEZONE: {os.environ.get('APP_TIMEZONE', 'NOT SET')}")
    logger.info(f"TELEGRAM_BOT_TOKEN: {'SET' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'NOT SET'}")
    logger.info(f"TELEGRAM_CHAT_ID: {'SET' if os.environ.get('TELEGRAM_CHAT_ID') else 'NOT SET'}")
    logger.info("========================")

    # ------------------------------------------------------------------
    # Fetch today's classes and appointments from Supabase
    # ------------------------------------------------------------------
    logger.info("=== Fetching today's schedule and appointments ===")
    try:
        from calendar_sync import fetch_events_from_calendar
        from time_utils import local_today
        
        today = local_today()
        today_classes, today_appointments, upcoming_exams = fetch_events_from_calendar(target_date=today, days_ahead=7)
        logger.info("Today has %d class(es).", len(today_classes))
        logger.info("Today has %d appointment(s).", len(today_appointments))
        logger.info("Upcoming exam set has %d row(s).", len(upcoming_exams))
    except Exception as exc:
        _handle_fatal("Failed to fetch today's data", exc)
        return

    # ------------------------------------------------------------------
    # Send Telegram notification
    # ------------------------------------------------------------------
    logger.info("=== Sending morning Telegram notification ===")
    try:
        from notifier import send_daily_summary
        send_daily_summary(
            today_classes,
            today_appointments,
            upcoming_exams=upcoming_exams,
        )
        logger.info("Telegram notification sent successfully.")
    except Exception as exc:
        _handle_fatal("Telegram notification failed", exc)
        return

    logger.info("=== Morning notification complete. ===")


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
