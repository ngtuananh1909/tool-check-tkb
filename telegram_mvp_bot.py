"""
telegram_mvp_bot.py - MVP Telegram listener for creating appointments.

Message format (plain text):
    tieude-thoigian-diadiem(optional)

Examples:
    họp nhóm-15/04 14:00-B402
    đi khám-2026-04-16 09:30
    gym-18:00

Time parsing rules (MVP):
    - YYYY-MM-DD HH:MM
    - DD/MM/YYYY HH:MM
    - DD/MM HH:MM      (uses current year)
    - HH:MM            (uses today)

The script uses Telegram getUpdates long polling and only accepts messages
from TELEGRAM_CHAT_ID (if set).
"""

import datetime as dt
import logging
import os
import re
import time
from requests import RequestException

import requests

from database import (
    create_appointment,
    get_nearest_elearning_deadlines,
    get_today_appointments,
    get_today_class_sessions,
)
from gemini_parser import (
    generate_conversational_reply_with_gemini,
    parse_appointment_with_gemini,
)
from time_utils import local_now, local_today

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

HELP_TEXT = (
    "Hi ban, minh san sang ho tro ban ne.\n\n"
    "Lenh co san:\n"
    "/today - Xem lich hen hom nay\n"
    "/schedule - Xem lich hoc\n"
    "/deadline - Xem deadline eLearning\n"
    "/add - Them lich hen theo mau\n\n"
    "Nhap lich nhanh theo mau:\n"
    "tieude-thoigian-diadiem(optional)\n"
    "Vi du: hop nhom-15/04 14:00-B402"
)

CLARIFICATION_FALLBACK_TEXT = (
    "Tin nhan cua ban chua du ro de minh tao lich. "
    "Ban gui lai theo mau: tieude-thoigian-diadiem(optional) nha."
)

CONFIRM_PREFIX = "Xong roi ne, minh da ghi lich cho ban:"
CREATE_ERROR_PREFIX = "Minh chua tao duoc lich hen luc nay"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore[import]
        load_dotenv()
    except Exception:
        pass


def _telegram_api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _send_text(token: str, chat_id: str, text: str) -> None:
    _send_message_payload(token, {"chat_id": chat_id, "text": text})


def _send_text_with_keyboard(token: str, chat_id: str, text: str, keyboard: dict) -> None:
    _send_message_payload(token, {"chat_id": chat_id, "text": text, "reply_markup": keyboard})


def _send_message_payload(token: str, payload: dict) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                _telegram_api(token, "sendMessage"),
                json=payload,
                timeout=30,
            )
            if not resp.ok:
                logger.error("Failed to send Telegram message: %s", resp.text)
            return
        except RequestException as exc:
            last_exc = exc
            if attempt == 3:
                break
            delay = 2 ** (attempt - 1)
            logger.warning(
                "Telegram send failed (%s). Retrying in %ss (%d/3).",
                exc,
                delay,
                attempt,
            )
            time.sleep(delay)

    if last_exc is not None:
        logger.error("Telegram send failed after retries: %s", last_exc)


def _normalize_chat_id(chat_id: str | int | None) -> str:
    return str(chat_id or "").strip()


def _parse_schedule_day_arg(arg: str | None, today: dt.date | None = None) -> dt.date:
    base = today or local_today()
    value = re.sub(r"\s+", " ", str(arg or "").strip().lower())
    if not value or value in {"hôm nay", "hom nay", "today"}:
        return base
    if value in {"mai", "ngày mai", "ngay mai", "tomorrow"}:
        return base + dt.timedelta(days=1)

    weekdays = {
        "thứ 2": 0, "thu 2": 0, "thứ hai": 0, "thu hai": 0, "t2": 0,
        "thứ 3": 1, "thu 3": 1, "thứ ba": 1, "thu ba": 1, "t3": 1,
        "thứ 4": 2, "thu 4": 2, "thứ tư": 2, "thu tu": 2, "t4": 2,
        "thứ 5": 3, "thu 5": 3, "thứ năm": 3, "thu nam": 3, "t5": 3,
        "thứ 6": 4, "thu 6": 4, "thứ sáu": 4, "thu sau": 4, "t6": 4,
        "thứ 7": 5, "thu 7": 5, "thứ bảy": 5, "thu bay": 5, "t7": 5,
        "chủ nhật": 6, "chu nhat": 6, "cn": 6,
    }
    if value in weekdays:
        delta = (weekdays[value] - base.weekday()) % 7
        return base + dt.timedelta(days=delta or 7)

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", value)
    if m:
        day, month, year = m.groups()
        return dt.date(int(year or base.year), int(month), int(day))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return dt.date.fromisoformat(value)
    raise ValueError("Không đọc được ngày. Dùng: hôm nay, mai, thứ 2..CN, DD/MM hoặc YYYY-MM-DD.")


def _parse_add_fields(text: str) -> dict[str, str | None]:
    fields = {"time": None, "job": None, "where": None}
    labels = {
        "thời gian": "time", "thoi gian": "time", "time": "time",
        "job": "job", "việc": "job", "viec": "job",
        "where": "where", "địa điểm": "where", "dia diem": "where", "location": "where",
    }
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        label, raw_value = line.split(":", 1)
        key = labels.get(label.strip().lower())
        if key:
            value = raw_value.strip()
            fields[key] = value or None
    if not any(fields.values()):
        raise ValueError("Bạn cần nhập ít nhất một mục: thời gian, job hoặc where.")
    return fields


def _parse_input(text: str) -> tuple[str, dt.date, str, str | None]:
    """
    Parse 'title-time-location(optional)'.

    Returns:
        (title, appointment_date, start_time_hhmmss, location)
    """
    if "-" not in text:
        raise ValueError("Thiếu dữ liệu. Dùng format: tieude-thoigian-diadiem(optional)")

    title, rest = text.split("-", 1)
    title = title.strip()
    rest = rest.strip()

    if not title:
        raise ValueError("Tiêu đề không được rỗng.")
    if not rest:
        raise ValueError("Thiếu phần thời gian.")

    # Try parsing full remainder as time first.
    try:
        appt_date, hhmm = _parse_time_field(rest)
        return title, appt_date, f"{hhmm}:00", None
    except ValueError:
        pass

    # If failed, split from right to support optional location.
    dash_positions = [i for i, ch in enumerate(rest) if ch == "-"]
    for pos in reversed(dash_positions):
        time_candidate = rest[:pos].strip()
        location_candidate = rest[pos + 1 :].strip()
        if not time_candidate or not location_candidate:
            continue
        try:
            appt_date, hhmm = _parse_time_field(time_candidate)
            return title, appt_date, f"{hhmm}:00", location_candidate
        except ValueError:
            continue

    raise ValueError(
        "Không đọc được thời gian. Dùng format: "
        "tieude-thoigian-diadiem(optional)."
    )


def _looks_like_appointment_message(text: str) -> bool:
    lower = text.lower()
    has_time = re.search(r"\b\d{1,2}:\d{2}\b", text) is not None
    has_short_date = re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{4})?\b", text) is not None
    has_iso_date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text) is not None

    if "-" in text:
        parts = text.split("-", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            rest = parts[1]
            if re.search(r"\b\d{1,2}:\d{2}\b", rest) or re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{4})?\b", rest) or re.search(
                r"\b\d{4}-\d{2}-\d{2}\b", rest
            ):
                return True

    if has_time or has_short_date or has_iso_date:
        return True

    keywords = ("hẹn", "hen", "họp", "hop", "lịch", "lich", "meeting", "deadline")
    return any(word in lower for word in keywords)


def _normalize_gemini_payload(payload: dict) -> tuple[str, dt.date, str | None, str | None, str | None, str | None, float | None]:
    """Convert Gemini JSON payload into DB-ready fields."""
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("Gemini không trả về tiêu đề hợp lệ.")

    appointment_date_raw = str(payload.get("appointment_date") or "").strip()
    if not appointment_date_raw:
        raise ValueError("Gemini không trả về ngày hợp lệ.")
    try:
        appointment_date = dt.date.fromisoformat(appointment_date_raw)
    except ValueError as exc:
        raise ValueError(f"Ngày Gemini trả về không hợp lệ: {appointment_date_raw}") from exc

    start_time = _normalize_time_value(payload.get("start_time"))
    end_time = _normalize_time_value(payload.get("end_time"))
    location = _normalize_optional_text(payload.get("location"))
    note = _normalize_optional_text(payload.get("note"))
    confidence = payload.get("confidence")

    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = None

    return title, appointment_date, start_time, end_time, location, note, confidence


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return text


def _normalize_time_value(value: object) -> str | None:
    text = _normalize_optional_text(value)
    if not text:
        return None
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text):
        return text
    if re.fullmatch(r"\d{2}:\d{2}", text):
        return f"{text}:00"
    return None


def _parse_time_field(raw: str) -> tuple[dt.date, str]:
    """Return (date, HH:MM) from accepted time patterns."""
    value = raw.strip()
    now = local_now()

    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", value)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        return dt.date(y, mo, d), _validate_hhmm(h, mi)

    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})", value)
    if m:
        d, mo, y, h, mi = map(int, m.groups())
        return dt.date(y, mo, d), _validate_hhmm(h, mi)

    m = re.fullmatch(r"(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", value)
    if m:
        d, mo, h, mi = map(int, m.groups())
        return dt.date(now.year, mo, d), _validate_hhmm(h, mi)

    m = re.fullmatch(r"(\d{2}):(\d{2})", value)
    if m:
        h, mi = map(int, m.groups())
        return now.date(), _validate_hhmm(h, mi)

    raise ValueError(
        "Không đọc được thời gian. Dùng một trong các format: "
        "YYYY-MM-DD HH:MM | DD/MM/YYYY HH:MM | DD/MM HH:MM | HH:MM"
    )


def _validate_hhmm(hour: int, minute: int) -> str:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Giờ không hợp lệ.")
    return f"{hour:02d}:{minute:02d}"


def _build_schedule_text(rows: list[dict], target_date: dt.date) -> str:
    lines = [f"Lịch học ngày {target_date.strftime('%d/%m/%Y')}:"]
    if not rows:
        lines.append("- Không có lịch học.")
        return "\n".join(lines)
    for idx, row in enumerate(rows, start=1):
        subject = row.get("subject_name") or "Môn học"
        start = str(row.get("start_time") or "").strip()[:5]
        end = str(row.get("end_time") or "").strip()[:5]
        room = row.get("room") or ""
        time_text = f"{start}-{end}" if start and end else "chưa rõ giờ"
        line = f"{idx}. {time_text} - {subject}"
        if room:
            line += f" @ {room}"
        lines.append(line)
    return "\n".join(lines)


def _deadline_callback_key(row: dict) -> str:
    for field in ("id", "source_signature"):
        value = str(row.get(field) or "").strip()
        if value:
            return value
    activity_url = str(row.get("activity_url") or "").strip()
    if activity_url:
        return activity_url
    return "|".join(
        [
            str(row.get("course_id") or "").strip(),
            str(row.get("activity_name") or "").strip(),
            str(row.get("due_date") or "").strip(),
        ]
    ).strip("|")


def _build_deadline_keyboard(rows: list[dict]) -> dict:
    from course_aliases import shorten_course_name

    buttons = []
    for row in rows:
        key = _deadline_callback_key(row)
        if not key:
            continue
        buttons.append([{"text": shorten_course_name(str(row.get("course_name") or "")), "callback_data": f"deadline:{key}"}])
    return {"inline_keyboard": buttons}


def _format_progress(row: dict) -> str:
    percent = row.get("progress_percent")
    if percent is None:
        return ""
    try:
        percent_text = f"{float(percent):.0f}%"
    except (TypeError, ValueError):
        return ""
    completed = row.get("lessons_completed")
    total = row.get("lessons_total")
    if completed is not None and total is not None:
        return f"{percent_text} ({completed}/{total} bài)"
    return percent_text


def _build_deadline_list_text(rows: list[dict]) -> str:
    if not rows:
        return "Không tìm thấy deadline chưa hoàn thành sắp tới."
    lines = [f"Có {len(rows)} deadline sắp tới:"]
    from course_aliases import shorten_course_name

    for row in rows:
        course = shorten_course_name(str(row.get("course_name") or ""))
        activity = row.get("activity_name") or "Deadline"
        due = _format_deadline_due(row.get("due_date"))
        progress = _format_progress(row)
        suffix = f" - {progress}" if progress else ""
        lines.append(f"- {course}: {activity} ({due}){suffix}")
    return "\n".join(lines)


def _build_deadline_detail_text(row: dict | None) -> str:
    if not row:
        return "Không tìm thấy deadline cho môn này."
    from course_aliases import shorten_course_name

    lines = [
        f"Môn: {shorten_course_name(str(row.get('course_name') or 'Môn học'))}",
        f"Deadline: {row.get('activity_name') or 'Deadline'}",
        f"Hạn nộp: {_format_deadline_due(row.get('due_date'))}",
    ]
    progress = _format_progress(row)
    if progress:
        lines.append(f"Tiến độ: {progress}")
    if row.get("activity_url"):
        lines.append(str(row["activity_url"]))
    return "\n".join(lines)


def _format_deadline_due(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "chưa rõ"
    vietnam_tz = dt.timezone(dt.timedelta(hours=7))
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=vietnam_tz)
        else:
            parsed = parsed.astimezone(vietnam_tz)
        return parsed.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return text[:16]


def _parse_add_appointment_payload(text: str) -> tuple[str, dt.date, str | None, str | None]:
    fields = _parse_add_fields(text)
    job = fields["job"] or "Lịch cá nhân"
    where = fields["where"]
    if fields["time"]:
        appt_date, hhmm = _parse_time_field(fields["time"])
        return job, appt_date, f"{hhmm}:00", where
    return job, local_today(), None, where


def _build_appointment_confirmation(title: str, appt_date: dt.date, start_time: str | None, location: str | None) -> str:
    conf = f"{CONFIRM_PREFIX} {title} - {appt_date.isoformat()}"
    if start_time:
        conf += f" {start_time[:5]}"
    if location:
        conf += f" - {location}"
    return conf + "\nMình đã lưu giúp bạn rồi nè."


def _build_today_appointments_text(rows: list[dict]) -> str:
    today = local_today().strftime("%d/%m/%Y")
    lines = [f"Lich hen hom nay ({today}):"]
    if not rows:
        lines.append("- Khong co lich hen.")
        return "\n".join(lines)

    for idx, row in enumerate(rows, start=1):
        t = (row.get("start_time") or "").strip()
        t = t[:5] if len(t) >= 5 else "all day"
        title = row.get("title", "N/A")
        location = row.get("location") or ""
        if location:
            lines.append(f"{idx}. {t} - {title} @ {location}")
        else:
            lines.append(f"{idx}. {t} - {title}")
    return "\n".join(lines)


def _fallback_conversational_reply(user_text: str) -> str:
    lower = user_text.lower()
    if any(word in lower for word in ("chào", "hello", "hi")):
        return "Chào bạn, mình đây nè. Bạn muốn mình nhắc lịch hay trò chuyện một chút?"
    if "cảm ơn" in lower:
        return "Không có gì đâu, mình luôn sẵn sàng hỗ trợ bạn nè."
    if "buồn" in lower or "mệt" in lower:
        return "Ôm tinh thần bạn một cái nhẹ nha, nghỉ một chút rồi mình cùng sắp xếp lại lịch cho dễ thở hơn."
    return (
        "Mình vẫn ở đây để nghe bạn nè. "
        "Nếu cần tạo lịch hẹn, bạn cứ nhắn kiểu: họp nhóm-15/04 14:00-B402."
    )


def _build_conversational_reply(user_text: str) -> str:
    reply = generate_conversational_reply_with_gemini(user_text)
    if reply:
        return reply
    return _fallback_conversational_reply(user_text)


def run() -> None:
    _load_dotenv()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN.")
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        logger.info("GEMINI_API_KEY not set; fallback parser will be used.")

    logger.info("Telegram MVP bot started (long polling).")
    offset: int | None = None

    while True:
        try:
            payload = {"timeout": 30}
            if offset is not None:
                payload["offset"] = offset

            resp = requests.get(
                _telegram_api(token, "getUpdates"),
                params=payload,
                timeout=40,
            )
            resp.raise_for_status()
            data = resp.json()
            updates = data.get("result", [])

            for upd in updates:
                offset = int(upd.get("update_id", 0)) + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = _normalize_chat_id((msg.get("chat") or {}).get("id"))

                if not text or not chat_id:
                    continue

                if allowed_chat_id and chat_id != allowed_chat_id:
                    logger.info("Ignore message from unauthorized chat_id=%s", chat_id)
                    continue

                lowered = text.lower()
                if lowered in {"/start", "/help"}:
                    _send_text(token, chat_id, HELP_TEXT)
                    continue

                if lowered == "/today":
                    rows = get_today_appointments()
                    _send_text(token, chat_id, _build_today_appointments_text(rows))
                    continue

                if lowered == "/deadline":
                    rows = get_nearest_elearning_deadlines()
                    keyboard = _build_deadline_keyboard(rows)
                    if rows and keyboard["inline_keyboard"]:
                        _send_text_with_keyboard(token, chat_id, _build_deadline_list_text(rows), keyboard)
                    else:
                        _send_text(token, chat_id, _build_deadline_list_text(rows))
                    continue

                if lowered.startswith("/schedule") or lowered.startswith("/scheduel"):
                    parts = text.split(maxsplit=1)
                    try:
                        target_date = _parse_schedule_day_arg(parts[1] if len(parts) > 1 else None)
                    except ValueError as exc:
                        _send_text(token, chat_id, str(exc))
                        continue
                    rows = get_today_class_sessions(target_date=target_date)
                    _send_text(token, chat_id, _build_schedule_text(rows, target_date))
                    continue

                if lowered == "/add":
                    _send_text(token, chat_id, "Nhập lịch theo mẫu:\nThời gian: \nJob: \nWhere: ")
                    continue

                try:
                    structured_add = any(label in lowered for label in ("thời gian:", "thoi gian:", "time:", "job:", "where:", "địa điểm:", "dia diem:"))
                    gemini_payload = None
                    if structured_add:
                        title, appt_date, start_time, location = _parse_add_appointment_payload(text)
                        end_time = None
                        note = None
                        confidence = None
                    else:
                        gemini_payload = parse_appointment_with_gemini(text)
                    if gemini_payload:
                        if gemini_payload.get("needs_clarification", False):
                            question = gemini_payload.get("clarification_question") or (
                                CLARIFICATION_FALLBACK_TEXT
                            )
                            _send_text(token, chat_id, str(question))
                            continue

                        (
                            title,
                            appt_date,
                            start_time,
                            end_time,
                            location,
                            note,
                            confidence,
                        ) = _normalize_gemini_payload(gemini_payload)
                    elif not structured_add:
                        try:
                            title, appt_date, start_time, location = _parse_input(text)
                        except ValueError:
                            _send_text(token, chat_id, _build_conversational_reply(text))
                            continue
                        end_time = None
                        note = None
                        confidence = None

                    create_appointment(
                        title=title,
                        appointment_date=appt_date,
                        start_time=start_time,
                        end_time=end_time,
                        location=location,
                        note=note,
                        raw_user_input=text,
                        gemini_confidence=confidence,
                    )
                    _send_text(token, chat_id, _build_appointment_confirmation(title, appt_date, start_time, location))
                except Exception as exc:
                    _send_text(token, chat_id, f"{CREATE_ERROR_PREFIX}: {exc}. Ban thu gui lai giup minh nhe.")

        except Exception as exc:
            logger.error("Polling error: %s", exc)
            time.sleep(3)


if __name__ == "__main__":
    run()
