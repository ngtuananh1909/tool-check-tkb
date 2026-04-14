"""
main.py
-------
Orchestration script for the TDTU automated schedule notification system.

Execution order:
    1. Crawler  – scrape the schedule from the TDTU portal.
    2. Database – clear old data and insert the fresh schedule.
    3. Notifier – query today's classes and send a Telegram message.

If any step fails, an error alert is sent to Telegram and the script
exits with a non-zero status code so GitHub Actions marks the run as
failed.
"""

import logging
import sys

from dotenv import load_dotenv

from crawler import fetch_schedule
from database import upsert_schedule
from notifier import send_error_notification, send_schedule_notification

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv()


def main() -> None:
    """Run all modules in sequence with error handling."""

    # ------------------------------------------------------------------
    # Step 1: Crawl the schedule from the TDTU portal
    # ------------------------------------------------------------------
    logger.info("=== Step 1: Fetching schedule from TDTU portal ===")
    try:
        schedule = fetch_schedule()
        logger.info("Crawler returned %d entries.", len(schedule))
    except Exception as exc:
        logger.exception("Crawler failed: %s", exc)
        send_error_notification(f"Crawler thất bại: {exc}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 2: Persist the schedule to Supabase
    # ------------------------------------------------------------------
    logger.info("=== Step 2: Updating schedule in the database ===")
    try:
        upsert_schedule(schedule)
        logger.info("Database updated successfully.")
    except Exception as exc:
        logger.exception("Database update failed: %s", exc)
        send_error_notification(f"Cập nhật CSDL thất bại: {exc}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 3: Send today's schedule via Telegram
    # ------------------------------------------------------------------
    logger.info("=== Step 3: Sending today's schedule notification ===")
    try:
        send_schedule_notification()
        logger.info("Telegram notification sent successfully.")
    except Exception as exc:
        logger.exception("Notification failed: %s", exc)
        send_error_notification(f"Gửi thông báo Telegram thất bại: {exc}")
        sys.exit(1)

    logger.info("=== All steps completed successfully ===")


if __name__ == "__main__":
    main()
