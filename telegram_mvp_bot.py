"""
telegram_mvp_bot.py - MVP Telegram listener for creating appointments.

Appointments are created only through the /add form flow.

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
    get_nearest_elearning_deadlines,
)
from calendar_sync import insert_calendar_event, fetch_events_from_calendar
from gemini_parser import generate_conversational_reply_with_gemini
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
    "/add - Mo form them lich\n\n"
    "Muon tao lich moi thi dung /add, bot se hoi lan luot Ngay, Gio, Lam gi, O dau."
)

ADD_ONLY_GUIDANCE_TEXT = "De them lich, ban dung /add. Bot se hoi lan luot tung muc de ban nhap nhanh hon nhe."

CONFIRM_PREFIX = "Xong roi ne, minh da ghi lich cho ban:"
CREATE_ERROR_PREFIX = "Minh chua tao duoc lich hen luc nay"
ADD_FORM_DONE_CALLBACK = "addform:done"
ADD_FORM_CANCEL_CALLBACK = "addform:cancel"
ADD_FORM_SKIP_WHERE_CALLBACK = "addform:skip_where"


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


def _send_text_with_markup(token: str, chat_id: str, text: str, markup: dict) -> None:
    _send_message_payload(token, {"chat_id": chat_id, "text": text, "reply_markup": markup})


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
    fields = {"date": None, "time": None, "job": None, "where": None}
    labels = {
        "ngày": "date", "ngay": "date", "date": "date",
        "thời gian": "time", "thoi gian": "time", "time": "time",
        "giờ": "time", "gio": "time",
        "job": "job", "việc": "job", "viec": "job", "làm gì": "job", "lam gi": "job",
        "where": "where", "địa điểm": "where", "dia diem": "where", "location": "where", "ở đâu": "where", "o dau": "where",
    }
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        label, raw_value = line.split(":", 1)
        key = labels.get(label.strip().lower())
        if key:
            value = raw_value.strip()
            fields[key] = value or None
    if not fields["date"]:
        raise ValueError("Thiếu mục 'Ngày'.")
    if not fields["time"]:
        raise ValueError("Thiếu mục 'Giờ'.")
    if not fields["job"]:
        raise ValueError("Thiếu mục 'Làm gì'.")
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

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", value)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        return dt.date(y, mo, d), _validate_hhmm(h, mi)

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})", value)
    if m:
        d, mo, y, h, mi = map(int, m.groups())
        return dt.date(y, mo, d), _validate_hhmm(h, mi)

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", value)
    if m:
        d, mo, h, mi = map(int, m.groups())
        return dt.date(now.year, mo, d), _validate_hhmm(h, mi)

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
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


def _parse_date_field(raw: str, *, reference_date: dt.date | None = None) -> dt.date:
    """Return date from flexible user input."""
    value = str(raw or "").strip()
    base = reference_date or local_today()

    if not value:
        raise ValueError("Ngày không được để trống.")

    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
        year, month, day = map(int, value.split("-"))
        return dt.date(year, month, day)

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", value)
    if m:
        day, month, year = m.groups()
        return dt.date(int(year or base.year), int(month), int(day))

    raise ValueError("Không đọc được ngày. Dùng YYYY-MM-DD hoặc DD/MM hoặc DD/MM/YYYY.")


def _parse_clock_field(raw: str) -> str:
    value = str(raw or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not m:
        raise ValueError("Không đọc được giờ. Dùng HH:MM, ví dụ 9:00 hoặc 09:00.")
    hour, minute = map(int, m.groups())
    return f"{_validate_hhmm(hour, minute)}:00"


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
    appointment_date = _parse_date_field(fields["date"] or "")
    start_time = _parse_clock_field(fields["time"] or "")
    return job, appointment_date, start_time, where


def _new_add_form_state() -> dict[str, object]:
    return {
        "step": "date",
        "date": None,
        "time": None,
        "job": None,
        "where": None,
    }


def _is_add_form_complete(state: dict[str, object]) -> bool:
    return bool(state.get("date") and state.get("time") and state.get("job") and state.get("step") == "confirm")


def _build_add_form_prompt(state: dict[str, object]) -> str:
    step = str(state.get("step") or "date")
    if step == "date":
        return "Nhập ngày cho lịch hẹn.\nVí dụ: 16/5 hoặc 2026-05-16\nGõ /cancel để hủy."
    if step == "time":
        return "Nhập giờ bắt đầu.\nVí dụ: 9:00 hoặc 09:00\nGõ /cancel để hủy."
    if step == "job":
        return "Nhập nội dung công việc.\nVí dụ: Họp nhóm\nGõ /cancel để hủy."
    if step == "where":
        return "Nhập địa điểm.\nVí dụ: B402\nHoặc bấm Skip nếu không có.\nGõ /cancel để hủy."
    return _build_add_form_review_text(state)


def _build_add_form_input_markup(state: dict[str, object]) -> dict[str, object]:
    step = str(state.get("step") or "date")
    placeholders = {
        "date": "Ví dụ: 16/5 hoặc 2026-05-16",
        "time": "Ví dụ: 9:00 hoặc 09:00",
        "job": "Ví dụ: Họp nhóm",
        "where": "Ví dụ: B402",
    }
    return {
        "force_reply": True,
        "input_field_placeholder": placeholders.get(step, "Nhập thông tin"),
    }


def _format_add_form_value(value: object) -> str:
    text = str(value or "").strip()
    return text if text else "(bỏ trống)"


def _build_add_form_review_text(state: dict[str, object]) -> str:
    return (
        "Mình đã nhận form:\n"
        f"- Ngày: {_format_add_form_value(state.get('date'))}\n"
        f"- Giờ: {_format_add_form_value(state.get('time'))}\n"
        f"- Làm gì: {_format_add_form_value(state.get('job'))}\n"
        f"- Ở đâu: {_format_add_form_value(state.get('where'))}\n\n"
        "Nếu ổn thì bấm Done (hoặc gửi /done) để lưu.\n"
        "Muốn bỏ thì bấm Cancel (hoặc gửi /cancel)."
    )


def _build_add_form_keyboard() -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Done", "callback_data": ADD_FORM_DONE_CALLBACK},
                {"text": "❌ Cancel", "callback_data": ADD_FORM_CANCEL_CALLBACK},
            ]
        ]
    }


def _build_add_form_step_keyboard(state: dict[str, object]) -> dict[str, list[list[dict[str, str]]]] | None:
    if str(state.get("step") or "") != "where":
        return None
    return {
        "inline_keyboard": [
            [{"text": "Bỏ qua", "callback_data": ADD_FORM_SKIP_WHERE_CALLBACK}],
            [{"text": "❌ Cancel", "callback_data": ADD_FORM_CANCEL_CALLBACK}],
        ]
    }


def _advance_add_form_state(state: dict[str, object], user_text: str) -> str:
    step = str(state.get("step") or "date")
    value = str(user_text or "").strip()
    if step == "date":
        _parse_date_field(value)
        state["date"] = value
        state["step"] = "time"
        return _build_add_form_prompt(state)
    if step == "time":
        _parse_clock_field(value)
        state["time"] = value
        state["step"] = "job"
        return _build_add_form_prompt(state)
    if step == "job":
        if not value:
            raise ValueError("Mục 'Làm gì' không được để trống.")
        state["job"] = value
        state["step"] = "where"
        return _build_add_form_prompt(state)
    if step == "where":
        state["where"] = value or None
        state["step"] = "confirm"
        return _build_add_form_review_text(state)
    raise ValueError("Form thêm lịch đang ở trạng thái không hợp lệ.")


def _skip_add_form_optional_step(state: dict[str, object]) -> str:
    if str(state.get("step") or "") != "where":
        raise ValueError("Không thể bỏ qua ở bước hiện tại.")
    state["where"] = None
    state["step"] = "confirm"
    return _build_add_form_review_text(state)


def _send_add_form_step(token: str, chat_id: str, state: dict[str, object], *, prefix: str | None = None) -> None:
    text = _build_add_form_prompt(state)
    if prefix:
        text = f"{prefix}\n{text}"
    step_keyboard = _build_add_form_step_keyboard(state)
    if step_keyboard:
        _send_text_with_keyboard(token, chat_id, text, step_keyboard)
        return
    _send_text_with_markup(token, chat_id, text, _build_add_form_input_markup(state))


def _build_add_appointment_from_form(state: dict[str, object]) -> tuple[str, dt.date, str | None, str | None]:
    appointment_date = _parse_date_field(state.get("date") or "")
    start_time = _parse_clock_field(state.get("time") or "")
    title = str(state.get("job") or "").strip()
    if not title:
        raise ValueError("Mục 'Làm gì' không được để trống.")
    location = _normalize_optional_text(state.get("where"))
    return title, appointment_date, start_time, location


def _build_add_form_raw_input(state: dict[str, object]) -> str:
    return (
        "FORM_ADD\n"
        f"Ngày: {_format_add_form_value(state.get('date'))}\n"
        f"Giờ: {_format_add_form_value(state.get('time'))}\n"
        f"Làm gì: {_format_add_form_value(state.get('job'))}\n"
        f"Ở đâu: {_format_add_form_value(state.get('where'))}"
    )


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
        logger.info("GEMINI_API_KEY not set; /add form flow remains available.")

    logger.info("Telegram MVP bot started (long polling).")
    offset: int | None = None
    add_form_states: dict[str, dict[str, object]] = {}

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
                callback_query = upd.get("callback_query") or {}
                if callback_query:
                    message = callback_query.get("message") or {}
                    chat_id = _normalize_chat_id((message.get("chat") or {}).get("id"))
                    if not chat_id:
                        continue
                    if allowed_chat_id and chat_id != allowed_chat_id:
                        logger.info("Ignore callback from unauthorized chat_id=%s", chat_id)
                        continue

                    data_value = str(callback_query.get("data") or "")
                    form_state = add_form_states.get(chat_id)

                    try:
                        requests.post(
                            _telegram_api(token, "answerCallbackQuery"),
                            json={"callback_query_id": callback_query.get("id")},
                            timeout=10,
                        )
                    except RequestException as exc:
                        logger.warning("Failed to answer callback query: %s", exc)

                    if data_value == ADD_FORM_CANCEL_CALLBACK:
                        add_form_states.pop(chat_id, None)
                        _send_text(token, chat_id, "Đã hủy form thêm lịch.")
                        continue
                    if data_value == ADD_FORM_SKIP_WHERE_CALLBACK:
                        if not form_state:
                            _send_text(token, chat_id, "Form đã hết hạn. Bạn dùng /add để tạo lại nhé.")
                            continue
                        try:
                            reply = _skip_add_form_optional_step(form_state)
                        except ValueError as exc:
                            _send_text(token, chat_id, str(exc))
                            continue
                        _send_text_with_keyboard(token, chat_id, reply, _build_add_form_keyboard())
                        continue
                    if data_value == ADD_FORM_DONE_CALLBACK:
                        if not form_state:
                            _send_text(token, chat_id, "Form đã hết hạn. Bạn dùng /add để tạo lại nhé.")
                            continue
                        if not _is_add_form_complete(form_state):
                            _send_add_form_step(token, chat_id, form_state, prefix="Bạn chưa điền xong form.")
                            continue
                        title, appt_date, start_time, location = _build_add_appointment_from_form(form_state)
                        insert_calendar_event(
                            title=title,
                            appointment_date=appt_date,
                            start_time=start_time,
                            end_time=None,
                            location=location,
                            note=_build_add_form_raw_input(form_state),
                        )
                        add_form_states.pop(chat_id, None)
                        _send_text(token, chat_id, _build_appointment_confirmation(title, appt_date, start_time, location))
                        continue

                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = _normalize_chat_id((msg.get("chat") or {}).get("id"))

                if not text or not chat_id:
                    continue

                if allowed_chat_id and chat_id != allowed_chat_id:
                    logger.info("Ignore message from unauthorized chat_id=%s", chat_id)
                    continue

                lowered = text.lower()
                form_state = add_form_states.get(chat_id)

                if lowered == "/cancel" and form_state:
                    add_form_states.pop(chat_id, None)
                    _send_text(token, chat_id, "Đã hủy form thêm lịch.")
                    continue

                if lowered == "/done" and form_state:
                    if not _is_add_form_complete(form_state):
                        _send_add_form_step(token, chat_id, form_state, prefix="Bạn chưa điền xong form.")
                        continue
                    title, appt_date, start_time, location = _build_add_appointment_from_form(form_state)
                    insert_calendar_event(
                        title=title,
                        appointment_date=appt_date,
                        start_time=start_time,
                        end_time=None,
                        location=location,
                        note=_build_add_form_raw_input(form_state),
                    )
                    add_form_states.pop(chat_id, None)
                    _send_text(token, chat_id, _build_appointment_confirmation(title, appt_date, start_time, location))
                    continue

                if form_state and not lowered.startswith("/"):
                    try:
                        reply = _advance_add_form_state(form_state, text)
                    except ValueError as exc:
                        _send_add_form_step(token, chat_id, form_state, prefix=str(exc))
                        continue
                    if _is_add_form_complete(form_state):
                        _send_text_with_keyboard(token, chat_id, reply, _build_add_form_keyboard())
                    else:
                        _send_add_form_step(token, chat_id, form_state)
                    continue

                if lowered in {"/start", "/help"}:
                    _send_text(token, chat_id, HELP_TEXT)
                    continue

                if lowered == "/today":
                    _, rows, _ = fetch_events_from_calendar(local_today())
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
                    rows, _, _ = fetch_events_from_calendar(target_date)
                    _send_text(token, chat_id, _build_schedule_text(rows, target_date))
                    continue

                if lowered == "/add":
                    state = _new_add_form_state()
                    add_form_states[chat_id] = state
                    _send_add_form_step(token, chat_id, state, prefix="Bắt đầu form thêm lịch.")
                    continue

                _send_text(token, chat_id, ADD_ONLY_GUIDANCE_TEXT)

        except Exception as exc:
            logger.error("Polling error: %s", exc)
            time.sleep(3)


if __name__ == "__main__":
    run()
