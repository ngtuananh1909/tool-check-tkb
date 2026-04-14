"""
notifier.py
-----------
Telegram notification module for the TDTU schedule system.

Fetches today's classes from the database and sends a formatted
MarkdownV2 message via the Telegram Bot API.

Required environment variables:
    TELEGRAM_BOT_TOKEN  – bot token from @BotFather
    TELEGRAM_CHAT_ID    – chat/channel ID to send the message to
    STUDENT_ID          – used to query the correct student's schedule
"""

import logging
import os
import re
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

from database import get_todays_classes

load_dotenv()

logger = logging.getLogger(__name__)

# Vietnam time is UTC+7
_VN_TZ = timezone(timedelta(hours=7))

# Telegram Bot API base URL
_TG_API = "https://api.telegram.org/bot{token}/sendMessage"

# Mapping from Python's weekday() (0=Monday) to Vietnamese day strings
_WEEKDAY_MAP = {
    0: "Thứ 2",
    1: "Thứ 3",
    2: "Thứ 4",
    3: "Thứ 5",
    4: "Thứ 6",
    5: "Thứ 7",
    6: "Chủ nhật",
}


def _escape_md(text: str) -> str:
    """
    Escape special characters for Telegram MarkdownV2 formatting.
    Required characters: _ * [ ] ( ) ~ ` > # + - = | { } . !
    """
    special = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(r"([" + re.escape(special) + r"])", r"\\\1", text)


def _build_message(classes: list[dict], day_label: str, date_str: str) -> str:
    """
    Build a MarkdownV2-formatted Telegram message for today's classes.

    Args:
        classes:   List of class dicts from the database.
        day_label: Vietnamese day name, e.g. 'Thứ 2'.
        date_str:  Human-readable date, e.g. '14/04/2026'.

    Returns:
        Formatted MarkdownV2 string ready to send.
    """
    esc_day = _escape_md(day_label)
    esc_date = _escape_md(date_str)

    header = f"🗓 *Lịch học hôm nay \\- {esc_day} \\({esc_date}\\)*\n"

    if not classes:
        return header + "\n✅ Không có lịch học hôm nay\\. Nghỉ ngơi nhé\\! 🎉"

    lines = [header]
    for idx, cls in enumerate(classes, start=1):
        subject = _escape_md(cls.get("subject_name", "N/A"))
        room = _escape_md(cls.get("room", "N/A"))
        start = _escape_md(str(cls.get("start_period", "?")))
        end = _escape_md(str(cls.get("end_period", "?")))

        lines.append(
            f"\n*{idx}\\.*\n"
            f"📚 *Môn:* {subject}\n"
            f"📍 *Phòng:* {room}\n"
            f"⏰ *Tiết:* {start} \\- {end}"
        )

    return "\n".join(lines)


def _send_telegram(message: str) -> None:
    """
    Send a MarkdownV2 message via the Telegram Bot API.

    Args:
        message: The formatted MarkdownV2 text to send.
    """
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    url = _TG_API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "MarkdownV2",
    }

    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    logger.info("Telegram message sent successfully (status %s)", response.status_code)


def send_schedule_notification() -> None:
    """
    Main entry point for the notifier module.

    1. Determine today's date and Vietnamese day name (in VN timezone).
    2. Query the database for today's classes.
    3. Build and send the Telegram notification.
    """
    student_id = os.environ["STUDENT_ID"]

    now_vn = datetime.now(tz=_VN_TZ)
    day_of_week = _WEEKDAY_MAP[now_vn.weekday()]
    date_str = now_vn.strftime("%d/%m/%Y")

    logger.info("Fetching classes for %s (%s)", day_of_week, date_str)

    classes = get_todays_classes(student_id, day_of_week)
    message = _build_message(classes, day_of_week, date_str)
    _send_telegram(message)


def send_error_notification(error_message: str) -> None:
    """
    Send a failure alert to Telegram when an error occurs.

    Args:
        error_message: The error description to include in the alert.
    """
    esc_err = _escape_md(error_message)
    message = f"🚨 *Lỗi hệ thống TKB*\n\n`{esc_err}`"

    try:
        _send_telegram(message)
    except Exception as exc:
        # Log but do not re-raise – we don't want error-reporting to crash the run
        logger.error("Failed to send error notification: %s", exc)
