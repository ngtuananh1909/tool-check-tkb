"""
telegram_mvp_bot.py - MVP Telegram listener for creating appointments.

Message format (plain text):
    tieude-thoigian-diadiem(optional)

Examples:
    hop nhom-15/04 14:00-B402
    di kham-2026-04-16 09:30
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

from database import create_appointment, get_today_appointments
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


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore[import]
        load_dotenv()
    except Exception:
        pass


def _telegram_api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _send_text(token: str, chat_id: str, text: str) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                _telegram_api(token, "sendMessage"),
                json={"chat_id": chat_id, "text": text},
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
    if "-" in text:
        return True
    if re.search(r"\b\d{1,2}:\d{2}\b", text):
        return True
    if re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{4})?\b", text):
        return True
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
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
        "Nếu cần tạo lịch hẹn, bạn cứ nhắn kiểu: hop nhom-15/04 14:00-B402."
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
                    _send_text(
                        token,
                        chat_id,
                        "MVP format:\n"
                        "tieude-thoigian-diadiem(optional)\n\n"
                        "Vi du:\n"
                        "hop nhom-15/04 14:00-B402\n"
                        "di kham-2026-04-16 09:30\n"
                        "gym-18:00",
                    )
                    continue

                if lowered == "/today":
                    rows = get_today_appointments()
                    _send_text(token, chat_id, _build_today_appointments_text(rows))
                    continue

                try:
                    gemini_payload = parse_appointment_with_gemini(text)
                    if gemini_payload:
                        if gemini_payload.get("needs_clarification", False):
                            if _looks_like_appointment_message(text):
                                question = gemini_payload.get("clarification_question") or (
                                    "Mình chưa hiểu rõ lịch hẹn này, bạn gửi lại giúp mình theo format: tieu de-thoi gian-dia diem(optional) nhé."
                                )
                                _send_text(token, chat_id, str(question))
                            else:
                                _send_text(token, chat_id, _build_conversational_reply(text))
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
                    else:
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
                    conf = f"OK. Da tao lich hen: {title} - {appt_date.isoformat()} {start_time[:5]}"
                    if location:
                        conf += f" - {location}"
                    conf += "\nMình đã lưu giúp bạn rồi nè."
                    _send_text(token, chat_id, conf)
                except Exception as exc:
                    _send_text(token, chat_id, f"Khong tao duoc lich hen: {exc}")

        except Exception as exc:
            logger.error("Polling error: %s", exc)
            time.sleep(3)


if __name__ == "__main__":
    run()
