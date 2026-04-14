"""
notifier.py – Telegram notification sender for today's class schedule.

Required environment variables:
    TELEGRAM_BOT_TOKEN  – Bot token from @BotFather
    TELEGRAM_CHAT_ID    – Target chat / channel ID

The message is formatted using Telegram's MarkdownV2 specification.
"""

import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

# Period → approximate time mapping (TDTU timetable convention)
# Adjust if your university uses different time slots.
PERIOD_TIME: dict[int, str] = {
    1: "07:00",
    2: "07:50",
    3: "08:40",
    4: "09:30",
    5: "10:20",
    6: "11:10",
    7: "12:30",
    8: "13:20",
    9: "14:10",
    10: "15:00",
    11: "15:50",
    12: "16:40",
    13: "17:30",
    14: "18:00",
    15: "18:50",
    16: "19:40",
}


# ---------------------------------------------------------------------------
# MarkdownV2 helpers
# ---------------------------------------------------------------------------

# Characters that must be escaped in MarkdownV2 outside of code spans
_MARKDOWN_V2_SPECIAL = re.compile(r"([_\*\[\]\(\)~`>#+\-=|{}.!\\])")


def _escape(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _MARKDOWN_V2_SPECIAL.sub(r"\\\1", str(text))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_today_schedule(classes: list[dict]) -> None:
    """
    Build and send a Telegram MarkdownV2 message summarising *classes*.

    Parameters
    ----------
    classes : list[dict]
        List of schedule row dicts with keys:
            subject_name, room, day_of_week, start_period, end_period

    Raises
    ------
    RuntimeError
        If the Telegram API call fails.
    """
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    message = _build_message(classes)
    _send_message(token, chat_id, message)


def send_error_alert(error: str) -> None:
    """
    Send a brief error alert to Telegram so failures are visible immediately.

    Parameters
    ----------
    error : str
        Human-readable description of the failure.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.error("Telegram credentials missing; cannot send error alert.")
        return

    text = (
        "⚠️ *Schedule Bot Error*\n\n"
        f"`{_escape(error)}`"
    )
    try:
        _send_message(token, chat_id, text)
    except Exception as exc:
        # Do not raise here – we're already in an error path
        logger.error("Failed to send error alert to Telegram: %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_message(classes: list[dict]) -> str:
    """Return a MarkdownV2-formatted string for *classes*."""
    import datetime

    today = datetime.date.today()
    weekday_name = today.strftime("%A")
    date_str = today.strftime("%d/%m/%Y")

    lines: list[str] = [
        f"📅 *Lịch học hôm nay \\- {_escape(weekday_name)}, {_escape(date_str)}*",
        "",
    ]

    if not classes:
        lines.append("✅ Hôm nay không có lớp học\\. Nghỉ ngơi thôi\\! 🎉")
    else:
        for idx, cls in enumerate(classes, start=1):
            subject = _escape(cls.get("subject_name", "N/A"))
            room = _escape(cls.get("room", "N/A"))
            start = cls.get("start_period", 0)
            end = cls.get("end_period", 0)
            start_time = _escape(PERIOD_TIME.get(start, str(start)))
            end_time = _escape(PERIOD_TIME.get(end, str(end)))

            lines += [
                f"*{_escape(str(idx))}\\.* 📚 {subject}",
                f"   📍 Phòng: `{room}`",
                f"   ⏰ Tiết {_escape(str(start))} → {_escape(str(end))}  \\({start_time} \\- {end_time}\\)",
                "",
            ]

    lines.append("_Chúc bạn học tốt\\!_ 🚀")
    return "\n".join(lines)


def _send_message(token: str, chat_id: str, text: str) -> None:
    """
    POST a message to the Telegram Bot API.

    Raises
    ------
    RuntimeError
        On non-2xx HTTP status or Telegram API error.
    """
    url = TELEGRAM_API_BASE.format(token=token, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }

    logger.info("Sending Telegram message to chat_id=%s", chat_id)
    response = requests.post(url, json=payload, timeout=30)

    if not response.ok:
        raise RuntimeError(
            f"Telegram API error {response.status_code}: {response.text}"
        )

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned ok=false: {result}")

    message_id = (result.get("result") or {}).get("message_id", "unknown")
    logger.info("Telegram message sent successfully (message_id=%s).", message_id)
