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
import time

import requests

from time_utils import local_today

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

# Period → official time mapping from the provided TDTU timetable image.
PERIOD_TIME: dict[int, str] = {
    1: "06:50",
    2: "07:40",
    3: "08:30",
    4: "09:20",
    5: "10:20",
    6: "11:10",
    7: "12:45",
    8: "13:35",
    9: "14:25",
    10: "15:25",
    11: "16:15",
    12: "17:05",
    13: "18:05",
    14: "18:55",
    15: "19:45",
}


# ---------------------------------------------------------------------------
# MarkdownV2 helpers
# ---------------------------------------------------------------------------

# Characters that must be escaped in MarkdownV2 outside of code spans
_MARKDOWN_V2_SPECIAL = re.compile(r"([_\*\[\]\(\)~`>#+\-=|{}.!\\])")


def _escape(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _MARKDOWN_V2_SPECIAL.sub(r"\\\1", str(text))


def _escape_code_span(text: str) -> str:
    """Escape text for use inside a MarkdownV2 inline code span (backticks).

    Inside a code span only the backtick itself and the backslash need escaping;
    all other characters are treated as literals by Telegram.
    """
    return str(text).replace("\\", "\\\\").replace("`", "\\`")


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


def send_daily_summary(
    classes: list[dict],
    appointments: list[dict],
    upcoming_exams: list[dict] | None = None,
    elearning_progress: list[dict] | None = None,
) -> None:
    """Send one combined message for today's classes and personal appointments."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    message = _build_combined_message(
        classes,
        appointments,
        upcoming_exams=upcoming_exams or [],
        elearning_progress=elearning_progress or [],
    )
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
        f"`{_escape_code_span(error)}`"
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
    today = local_today()
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
            status_label = _escape(_format_class_status(cls.get("status")))

            lines += [
                f"*{_escape(str(idx))}\\.* 📚 {subject}",
                f"   📍 Phòng: `{room}`",
                f"   ⏰ Tiết {_escape(str(start))} → {_escape(str(end))}  \\({start_time} \\- {end_time}\\)",
                f"   🔖 Trạng thái: {status_label}",
                "",
            ]

    lines.append("_Chúc bạn học tốt\\!_ 🚀")
    return "\n".join(lines)


def _build_combined_message(
    classes: list[dict],
    appointments: list[dict],
    upcoming_exams: list[dict],
    elearning_progress: list[dict],
) -> str:
    """Return a MarkdownV2 summary containing both classes and appointments."""
    today = local_today()
    weekday_name = today.strftime("%A")
    date_str = today.strftime("%d/%m/%Y")

    lines: list[str] = [
        f"📅 *Kế hoạch hôm nay \\- {_escape(weekday_name)}, {_escape(date_str)}*",
        "",
        "🎓 *Lịch học*",
    ]

    if not classes:
        lines += ["Không có lớp học\\.", ""]
    else:
        for idx, cls in enumerate(classes, start=1):
            subject = _escape(cls.get("subject_name", "N/A"))
            room = _escape(cls.get("room", "N/A"))
            start = cls.get("start_period", 0)
            end = cls.get("end_period", 0)
            start_time = _escape(PERIOD_TIME.get(start, str(start)))
            end_time = _escape(PERIOD_TIME.get(end, str(end)))
            status_label = _escape(_format_class_status(cls.get("status")))

            lines += [
                f"{_escape(str(idx))}\\. {subject}",
                f"   📍 `{room}`",
                f"   ⏰ Tiết {_escape(str(start))}→{_escape(str(end))} \\({start_time}\\-{end_time}\\)",
                f"   🔖 Trạng thái: {status_label}",
            ]
        lines.append("")

    lines.append("🗓️ *Lịch hẹn cá nhân*")
    if not appointments:
        lines += ["Không có lịch hẹn\\.", ""]
    else:
        for idx, appt in enumerate(appointments, start=1):
            title = _escape(appt.get("title", "N/A"))
            start = _escape(_display_time(appt.get("start_time")))
            end = _escape(_display_time(appt.get("end_time")))
            location = _escape(appt.get("location") or "")
            note = _escape(appt.get("note") or "")

            time_range = f"{start} \\- {end}" if start and end else (start or "all day")
            lines.append(f"{_escape(str(idx))}\\. {title}")
            lines.append(f"   ⏰ {time_range}")
            if location:
                lines.append(f"   📍 {location}")
            if note:
                lines.append(f"   📝 {note}")
        lines.append("")

    lines.append("📝 *Lịch thi sắp tới \\(7 ngày\\)*")
    if not upcoming_exams:
        lines += ["Không có lịch thi\\.", ""]
    else:
        for idx, exam in enumerate(upcoming_exams[:10], start=1):
            subject = _escape(exam.get("subject_name") or "N/A")
            exam_date = _escape(str(exam.get("exam_date") or "N/A"))
            room = _escape(exam.get("exam_room") or "")
            start = _escape(_display_time(exam.get("start_time")))
            end = _escape(_display_time(exam.get("end_time")))

            lines.append(f"{_escape(str(idx))}\\. {subject}")
            lines.append(f"   📅 {exam_date}")
            if start or end:
                time_range = f"{start} \\- {end}" if start and end else (start or end)
                lines.append(f"   ⏰ {time_range}")
            if room:
                lines.append(f"   📍 {room}")
        lines.append("")

    lines.append("📚 *Tiến độ eLearning theo môn*")
    if not elearning_progress:
        lines += ["Chưa có dữ liệu tiến độ\\.", ""]
    else:
        for row in elearning_progress[:12]:
            course_name = _escape(_compact_course_name(row.get("course_name") or "N/A"))
            percent = row.get("progress_percent")
            try:
                percent_text = f"{float(percent):.0f}%"
            except (TypeError, ValueError):
                percent_text = "0%"
            lessons_completed = row.get("lessons_completed")
            lessons_total = row.get("lessons_total")

            progress_label = _escape(percent_text)
            detail = ""
            if lessons_completed is not None and lessons_total is not None:
                detail = f" ┊ ✅ {_escape(str(lessons_completed))}/{_escape(str(lessons_total))}"

            lines.append(f"• 📘 {course_name} ┊ 📈 *{progress_label}*{detail}")
        lines.append("")

    lines.append("_Hom nay minh luon dong hanh cung ban, co gi can thi nhan minh nha\\!_")
    return "\n".join(lines)


def _display_time(value: str | None) -> str:
    """Format DB time strings like HH:MM:SS into HH:MM for display."""
    if not value:
        return ""
    text = str(value).strip()
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return text


def _format_class_status(status: object) -> str:
    """Convert internal class status to human-friendly Vietnamese label."""
    value = str(status or "").strip().lower()
    mapping = {
        "scheduled": "Học bình thường",
        "makeup": "Học bù",
        "absent": "Báo vắng",
        "cancelled": "Nghỉ học",
        "moved": "Dời lịch",
    }
    return mapping.get(value, "Học bình thường")


def _compact_course_name(name: object) -> str:
    """Shorten noisy eLearning course labels for compact Telegram output."""
    text = str(name or "").strip()
    if not text:
        return "N/A"

    text = re.sub(r"\s+", " ", text)
    text = text.replace("_", " ")
    text = re.sub(r"^HK\d+\s+\d{4}\s+\d{5,}\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^HK\d+\s+\d{4}\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^course\s*name\s*", "", text, flags=re.IGNORECASE)
    return text.strip(" -")


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
    last_exc: Exception | None = None

    for attempt in range(1, 4):
        try:
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
            return
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 3:
                break
            delay = 2 ** (attempt - 1)
            logger.warning(
                "Telegram send failed due to network issue (%s). Retrying in %ss (%d/3).",
                exc,
                delay,
                attempt,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise RuntimeError(f"Telegram message send failed after retries: {last_exc}")
