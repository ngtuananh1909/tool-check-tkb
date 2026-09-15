"""FastAPI Telegram webhook backed directly by Google Calendar.

This is the production-friendly replacement for long polling:
- Telegram sends updates to POST /telegram/webhook
- The app previews AI-extracted appointments and writes only after confirmation
- The appointment is inserted into Google Calendar

Environment variables:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID (required owner private chat)
    TELEGRAM_WEBHOOK_URL (optional; public HTTPS URL for auto-register)
    TELEGRAM_WEBHOOK_SECRET (required secret token checked on incoming requests)
    GEMINI_API_KEY (optional; Smart Paste falls back to /add when absent)
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, Header, HTTPException

from calendar_sync import (
    SYNC_SOURCE_DEADLINE,
    SYNC_SOURCE_EXAM,
    CalendarConfigurationError,
    CalendarPersistenceError,
    fetch_events_from_calendar,
    fetch_tagged_calendar_events,
    find_tagged_calendar_event,
    insert_calendar_event,
)
from gemini_parser import parse_events_with_gemini
from smart_paste import (
    SMART_PASTE_MAX_INPUT_CHARS,
    SmartPasteBatch,
    SmartPasteBatchStatus,
    SmartPasteClarificationError,
    SmartPasteStateConflict,
    SmartPasteStateStore,
    SmartPasteValidationError,
    smart_paste_fingerprint,
    validate_smart_paste_payload,
)
from telegram_mvp_bot import (
    ADD_FORM_CANCEL_CALLBACK,
    ADD_FORM_DONE_CALLBACK,
    ADD_FORM_SKIP_WHERE_CALLBACK,
    SMART_PASTE_ADD_ALL_CALLBACK,
    SMART_PASTE_ADD_PREFIX,
    SMART_PASTE_CANCEL_CALLBACK,
    SMART_PASTE_CANCEL_PREFIX,
    SMART_PASTE_RETRY_PREFIX,
    TelegramDeliveryError,
    _advance_add_form_state,
    _build_add_appointment_from_form,
    _build_add_form_keyboard,
    _build_add_form_raw_input,
    _build_appointment_confirmation,
    _build_deadline_detail_text,
    _build_deadline_keyboard,
    _build_deadline_list_text,
    _build_exam_list_text,
    _build_schedule_text,
    _build_smart_paste_keyboard,
    _build_smart_paste_preview_text,
    _build_smart_paste_retry_keyboard,
    _build_today_appointments_text,
    _is_add_form_complete,
    _new_add_form_state,
    _normalize_chat_id,
    _parse_schedule_day_arg,
    _send_add_form_step,
    _send_text,
    _send_text_with_keyboard,
    _skip_add_form_optional_step,
)
from time_utils import local_today

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

WEBHOOK_PATH = "/telegram/webhook"
HEALTH_PATH = "/health"
WEBHOOK_INFO_PATH = "/telegram/webhook/info"
GEMINI_HEALTH_PATH = "/gemini/health"
_TELEGRAM_URL_TOKEN_RE = re.compile(r"(https://api\.telegram\.org/bot)[^/\s]+", re.IGNORECASE)
_TELEGRAM_PATH_TOKEN_RE = re.compile(r"(/bot)[^/\s]+", re.IGNORECASE)
_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_ADD_FORM_STATES: dict[str, dict[str, object]] = {}
_SMART_PASTE_STATES = SmartPasteStateStore()

ADD_ONLY_GUIDANCE_TEXT = "Bạn có thể dán lịch tự nhiên để xem trước, hoặc dùng /add để nhập từng mục thủ công."
START_HELP_TEXT = (
    "Bot hiện hỗ trợ các lệnh sau:\n"
    "/today - Xem lịch hẹn hôm nay\n"
    "/schedule - Xem lịch học\n"
    "/deadline - Xem deadline eLearning\n"
    "/exam - Xem lịch thi 90 ngày tới\n"
    "/add - Mở form thêm lịch\n\n"
    "Bạn cũng có thể dán tin nhắn tự nhiên, ví dụ: Mai 14h họp nhóm ở B402. "
    "Bot sẽ xem trước và chỉ lưu khi bạn bấm Thêm tất cả."
)


def _telegram_api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _load_env() -> tuple[str, str, str, str | None]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
    webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip() or None

    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN.")
    if not allowed_chat_id:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID; webhook owner allowlist is required.")
    if not webhook_secret or not _WEBHOOK_SECRET_RE.fullmatch(webhook_secret):
        raise RuntimeError("Missing or invalid TELEGRAM_WEBHOOK_SECRET.")

    return token, allowed_chat_id, webhook_url, webhook_secret


def _safe_url_label(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return "<invalid-url>"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _redact_telegram_error(value: object) -> str:
    redacted = _TELEGRAM_URL_TOKEN_RE.sub(r"\1[redacted]", str(value))
    return _TELEGRAM_PATH_TOKEN_RE.sub(r"\1[redacted]", redacted)


def _telegram_post(token: str, method: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    try:
        response = requests.post(
            _telegram_api(token, method),
            json=payload or {},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise TelegramDeliveryError(
            f"Telegram {method} request failed: {_redact_telegram_error(exc)}"
        ) from exc
    try:
        result = response.json()
    except ValueError:
        raise TelegramDeliveryError(f"Telegram {method} returned invalid JSON.")

    if not response.ok or not isinstance(result, dict) or not result.get("ok"):
        raise TelegramDeliveryError(
            f"Telegram {method} failed with HTTP {response.status_code}."
        )
    return result


def _get_webhook_info(token: str) -> dict[str, object]:
    return _telegram_post(token, "getWebhookInfo")


def _sanitize_webhook_info(info: dict[str, object]) -> dict[str, object]:
    result = info.get("result") if isinstance(info.get("result"), dict) else {}
    assert isinstance(result, dict)
    return {
        "ok": bool(info.get("ok")),
        "url": _safe_url_label(str(result.get("url") or "")) if result.get("url") else None,
        "pending_update_count": result.get("pending_update_count", 0),
        "last_error_message": _redact_telegram_error(result.get("last_error_message"))
        if result.get("last_error_message")
        else None,
    }


def _register_webhook(token: str, webhook_url: str, webhook_secret: str | None) -> None:
    if _safe_url_label(webhook_url) == "<invalid-url>":
        raise RuntimeError("TELEGRAM_WEBHOOK_URL must be an HTTPS URL.")
    payload: dict[str, object] = {
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    payload["secret_token"] = webhook_secret

    logger.info(
        "Registering Telegram webhook url=%s secret_set=%s",
        _safe_url_label(webhook_url),
        bool(webhook_secret),
    )
    result = _telegram_post(token, "setWebhook", payload)

    logger.info("Telegram webhook registered: %s", result.get("description") or "ok")
    try:
        logger.info("Telegram webhook info: %s", _sanitize_webhook_info(_get_webhook_info(token)))
    except (TelegramDeliveryError, requests.RequestException, RuntimeError) as exc:
        logger.warning("Could not fetch Telegram webhook info after registration: %s", type(exc).__name__)


def _register_command_menu(token: str) -> None:
    commands = [
        {"command": "start", "description": "Hướng dẫn sử dụng bot"},
        {"command": "today", "description": "Xem lịch hẹn hôm nay"},
        {"command": "schedule", "description": "Xem lịch học"},
        {"command": "deadline", "description": "Xem deadline eLearning"},
        {"command": "exam", "description": "Xem lịch thi 90 ngày tới"},
        {"command": "add", "description": "Thêm lịch hẹn theo mẫu"},
    ]
    result = _telegram_post(token, "setMyCommands", {"commands": commands})
    if not result.get("ok"):
        raise RuntimeError(f"Failed to register Telegram command menu: {result}")
    logger.info("Telegram command menu registered with %d command(s).", len(commands))


def _delete_webhook(token: str) -> None:
    try:
        _telegram_post(token, "deleteWebhook", {"drop_pending_updates": False})
        logger.info("Telegram webhook deleted.")
    except (TelegramDeliveryError, requests.RequestException) as exc:
        logger.warning("Could not delete webhook cleanly: %s", type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_dotenv()
    token, _, webhook_url, webhook_secret = _load_env()

    if webhook_url:
        _register_webhook(token, webhook_url, webhook_secret)
    else:
        logger.info("TELEGRAM_WEBHOOK_URL not set; webhook auto-registration skipped.")

    try:
        _register_command_menu(token)
    except (TelegramDeliveryError, requests.RequestException, RuntimeError) as exc:
        logger.warning("Command menu registration failed: %s", type(exc).__name__)

    yield

    # Keep shutdown gentle; do not force delete webhook unless explicitly desired.
    if os.environ.get("TELEGRAM_DELETE_WEBHOOK_ON_SHUTDOWN", "").strip().lower() in {"1", "true", "yes"}:
        _delete_webhook(token)


app = FastAPI(lifespan=lifespan)


@app.get(HEALTH_PATH)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(WEBHOOK_INFO_PATH)
def webhook_info(
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, object]:
    token, _, _, webhook_secret = _load_env()
    if not webhook_secret or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return _sanitize_webhook_info(_get_webhook_info(token))


@app.get(GEMINI_HEALTH_PATH)
def gemini_health(
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, object]:
    _, _, _, webhook_secret = _load_env()
    if not webhook_secret or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    api_key_set = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    sdk_available = False
    sdk_name: str | None = None
    try:
        import google.genai
        sdk_available = True
        sdk_name = "google-genai"
    except ImportError:
        try:
            import google.generativeai  # noqa: F401
            sdk_available = True
            sdk_name = "google-generativeai-legacy"
        except ImportError:
            pass
    return {"api_key_set": api_key_set, "sdk_available": sdk_available, "sdk": sdk_name}


def _is_private_owner_message(
    message: dict, allowed_chat_id: str, *, sender: dict | None = None
) -> bool:
    chat = message.get("chat") or {}
    sender = sender or message.get("from") or {}
    chat_type = str(chat.get("type") or "")
    chat_id = _normalize_chat_id(chat.get("id"))
    sender_id = _normalize_chat_id(sender.get("id"))
    return chat_type == "private" and chat_id == allowed_chat_id and sender_id == allowed_chat_id


def _update_key(payload: dict) -> str | None:
    update_id = payload.get("update_id")
    if update_id is not None:
        return f"update:{update_id}"
    callback = payload.get("callback_query") or {}
    callback_id = callback.get("id")
    if callback_id:
        return f"callback:{callback_id}"
    message = payload.get("message") or {}
    message_id = message.get("message_id")
    chat_id = _normalize_chat_id((message.get("chat") or {}).get("id"))
    if message_id and chat_id:
        return f"message:{chat_id}:{message_id}"
    return None


def _callback_message_id(callback_query: dict) -> str | None:
    message_id = ((callback_query.get("message") or {}).get("message_id"))
    return str(message_id) if message_id is not None else None


def _answer_callback(token: str, callback_query: dict, *, text: str | None = None) -> None:
    callback_id = callback_query.get("id")
    if not callback_id:
        return
    payload: dict[str, object] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:200]
    try:
        _telegram_post(token, "answerCallbackQuery", payload)
    except TelegramDeliveryError as exc:
        logger.warning("Could not answer Telegram callback: %s", type(exc).__name__)


def _message_id_from_telegram_result(result: dict[str, object]) -> str | None:
    value = result.get("result")
    if not isinstance(value, dict) or value.get("message_id") is None:
        return None
    return str(value.get("message_id"))


def _event_dicts(batch: SmartPasteBatch) -> list[dict[str, object]]:
    return [
        {
            "title": item.event.title,
            "appointment_date": item.event.appointment_date,
            "start_time": item.event.start_time,
            "end_time": item.event.end_time,
            "location": item.event.location,
            "note": item.event.note,
        }
        for item in batch.events
    ]


def _build_smart_paste_result_text(batch: SmartPasteBatch) -> str:
    succeeded = [item.event.title for item in batch.events if item.status == "succeeded"]
    failed = [item.event.title for item in batch.events if item.status == "failed"]
    if batch.status == SmartPasteBatchStatus.COMPLETED:
        lines = [f"✅ Đã thêm {len(succeeded)} lịch vào Google Calendar:"]
        lines.extend(f"- {title}" for title in succeeded)
        return "\n".join(lines)
    if succeeded:
        lines = [f"⚠️ Đã thêm {len(succeeded)}/{len(batch.events)} lịch vào Google Calendar."]
        lines.append("Các lịch chưa thêm được:")
        lines.extend(f"- {title}" for title in failed)
        lines.append("Bạn có thể thử lại các lịch lỗi hoặc hủy phần còn lại.")
        return "\n".join(lines)
    lines = ["❌ Chưa thêm được lịch nào vào Google Calendar."]
    lines.append("Các lịch chưa thêm được:")
    lines.extend(f"- {item.event.title}" for item in batch.events if item.status == "failed")
    lines.append("Bạn có thể thử lại hoặc hủy các lịch còn lại.")
    return "\n".join(lines)


def _clear_inline_keyboard(token: str, chat_id: str, message_id: str | None) -> None:
    if not message_id:
        return
    try:
        _telegram_post(
            token,
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )
    except TelegramDeliveryError as exc:
        logger.debug("Could not clear Smart Paste keyboard: %s", type(exc).__name__)


def _parse_smart_paste_callback(data: str, chat_id: str) -> tuple[str, str | None]:
    if data == SMART_PASTE_ADD_ALL_CALLBACK:
        batch = _SMART_PASTE_STATES.active_batch_for_chat(chat_id)
        # Compatibility for pre-hardening buttons only when no preview message
        # was recorded; real new previews always carry a batch-specific token.
        if batch and batch.fingerprint == "legacy":
            return "add", batch.batch_id
        return "unknown", None
    if data == SMART_PASTE_CANCEL_CALLBACK:
        batch = _SMART_PASTE_STATES.active_batch_for_chat(chat_id)
        if batch and batch.fingerprint == "legacy":
            return "cancel", batch.batch_id
        return "unknown", None
    for prefix, action in (
        (SMART_PASTE_ADD_PREFIX, "add"),
        (SMART_PASTE_RETRY_PREFIX, "retry"),
        (SMART_PASTE_CANCEL_PREFIX, "cancel"),
    ):
        if data.startswith(prefix):
            batch_id = data[len(prefix) :]
            if re.fullmatch(r"[0-9a-f]{32}", batch_id):
                return action, batch_id
            return "unknown", None
    return "unknown", None


@app.post(WEBHOOK_PATH)
def telegram_webhook(
    payload: dict,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    token, allowed_chat_id, _, webhook_secret = _load_env()
    if not webhook_secret or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook secret token")

    chat_id = ""
    try:
        callback_query = payload.get("callback_query") or {}
        if callback_query:
            message = callback_query.get("message") or {}
            chat_id = _normalize_chat_id((message.get("chat") or {}).get("id"))
            if not _is_private_owner_message(
                message, allowed_chat_id, sender=callback_query.get("from") or {}
            ) or not chat_id:
                logger.info("Ignore unauthorized Telegram callback.")
                return {"ok": True}

            data = str(callback_query.get("data") or "")
            if data.startswith("deadline:"):
                _answer_callback(token, callback_query)
                selected = find_tagged_calendar_event(SYNC_SOURCE_DEADLINE, data.split(":", 1)[1])
                _send_text(token, chat_id, _build_deadline_detail_text(selected))
                return {"ok": True}

            _answer_callback(token, callback_query)
            if data == ADD_FORM_CANCEL_CALLBACK:
                _ADD_FORM_STATES.pop(chat_id, None)
                _send_text(token, chat_id, "Đã hủy form thêm lịch.")
                return {"ok": True}

            if data in {ADD_FORM_DONE_CALLBACK, ADD_FORM_SKIP_WHERE_CALLBACK}:
                state = _ADD_FORM_STATES.get(chat_id)
                if not state:
                    _send_text(token, chat_id, "Form đã hết hạn. Bạn dùng /add để tạo lại nhé.")
                    return {"ok": True}
                if data == ADD_FORM_SKIP_WHERE_CALLBACK:
                    try:
                        reply = _skip_add_form_optional_step(state)
                    except ValueError as exc:
                        _send_text(token, chat_id, str(exc))
                        return {"ok": True}
                    _send_text_with_keyboard(token, chat_id, reply, _build_add_form_keyboard())
                    return {"ok": True}
                if not _is_add_form_complete(state):
                    _send_add_form_step(token, chat_id, state, prefix="Bạn chưa điền xong form.")
                    return {"ok": True}
                title, appt_date, start_time, location = _build_add_appointment_from_form(state)
                appointment_id = str(state.setdefault("appointment_id", uuid.uuid4().hex))
                insert_calendar_event(
                    title=title,
                    appointment_date=appt_date,
                    start_time=start_time,
                    end_time=None,
                    location=location,
                    note=_build_add_form_raw_input(state),
                    appointment_id=appointment_id,
                )
                _ADD_FORM_STATES.pop(chat_id, None)
                _send_text(token, chat_id, _build_appointment_confirmation(title, appt_date, start_time, location))
                return {"ok": True}

            action, batch_id = _parse_smart_paste_callback(data, chat_id)
            if action == "unknown" or not batch_id:
                _send_text(token, chat_id, "Nút Smart Paste này đã hết hạn. Bạn hãy dán lại nội dung nhé.")
                return {"ok": True}
            if action == "cancel":
                callback_message_id = _callback_message_id(callback_query)
                current = _SMART_PASTE_STATES.get_batch(chat_id, batch_id)
                if not current:
                    _send_text(token, chat_id, "Không tìm thấy lịch chờ xác nhận hoặc đã hết hạn.")
                elif current.action_message_id and current.action_message_id != callback_message_id:
                    _send_text(token, chat_id, "Nút này không thuộc preview hiện tại.")
                elif current.status == SmartPasteBatchStatus.PROCESSING:
                    _send_text(token, chat_id, "Batch đang được xử lý, bạn thử lại sau nhé.")
                elif current.status == SmartPasteBatchStatus.COMPLETED:
                    _send_text(token, chat_id, "Các lịch trong batch này đã được thêm rồi.")
                elif current.status in {
                    SmartPasteBatchStatus.CANCELLED,
                    SmartPasteBatchStatus.EXPIRED,
                    SmartPasteBatchStatus.SUPERSEDED,
                }:
                    _send_text(token, chat_id, "Batch này đã hết hạn hoặc đã được xử lý trước đó.")
                else:
                    batch = _SMART_PASTE_STATES.cancel(
                        chat_id, batch_id, message_id=callback_message_id
                    )
                    if not batch:
                        _send_text(token, chat_id, "Nút này không thuộc preview hiện tại.")
                    else:
                        _clear_inline_keyboard(token, chat_id, current.action_message_id)
                        _send_text(token, chat_id, "Đã hủy các lịch chưa lưu.")
                return {"ok": True}

            status, batch = _SMART_PASTE_STATES.start_processing(
                chat_id, batch_id, _callback_message_id(callback_query)
            )
            if not batch:
                _send_text(token, chat_id, "Không tìm thấy lịch chờ xác nhận hoặc đã hết hạn.")
                return {"ok": True}
            if status == "expired":
                _send_text(token, chat_id, "Preview đã hết hạn. Bạn hãy dán lại nội dung nhé.")
                return {"ok": True}
            if status == "wrong_message":
                _send_text(token, chat_id, "Nút này không thuộc preview hiện tại.")
                return {"ok": True}
            if status == "processing":
                _send_text(token, chat_id, "Batch đang được xử lý, bạn chờ một chút nhé.")
                return {"ok": True}
            if status == SmartPasteBatchStatus.COMPLETED.value:
                _send_text(token, chat_id, _build_smart_paste_result_text(batch))
                return {"ok": True}
            if status == SmartPasteBatchStatus.EXPIRED.value:
                _send_text(token, chat_id, "Preview đã hết hạn. Bạn hãy dán lại nội dung nhé.")
                return {"ok": True}
            if status not in {"started"}:
                _send_text(token, chat_id, "Batch này đã được xử lý hoặc đã hủy.")
                return {"ok": True}

            for item in batch.events:
                if item.status == "succeeded":
                    continue
                event = item.event
                try:
                    calendar_event_id = insert_calendar_event(
                        title=event.title,
                        appointment_date=event.appointment_date,
                        start_time=event.start_time,
                        end_time=event.end_time,
                        location=event.location,
                        note=event.note,
                        appointment_id=event.event_id,
                    )
                    if not calendar_event_id:
                        raise CalendarPersistenceError("Calendar returned no event ID")
                    _SMART_PASTE_STATES.record_success(
                        chat_id, batch.batch_id, event.event_id, calendar_event_id
                    )
                except (CalendarConfigurationError, CalendarPersistenceError) as exc:
                    logger.warning(
                        "Smart Paste Calendar event failed batch=%s event=%s code=%s",
                        batch.batch_id,
                        event.event_id,
                        type(exc).__name__,
                    )
                    _SMART_PASTE_STATES.record_failure(
                        chat_id, batch.batch_id, event.event_id, type(exc).__name__
                    )
                except Exception:
                    logger.exception(
                        "Unexpected Smart Paste Calendar failure batch=%s event=%s",
                        batch.batch_id,
                        event.event_id,
                    )
                    _SMART_PASTE_STATES.record_failure(
                        chat_id, batch.batch_id, event.event_id, "unexpected_error"
                    )

            finished = _SMART_PASTE_STATES.finish_processing(chat_id, batch.batch_id)
            if not finished:
                _send_text(token, chat_id, "Không tìm thấy kết quả xử lý Smart Paste.")
                return {"ok": True}
            previous_action_message_id = finished.action_message_id
            markup = (
                _build_smart_paste_retry_keyboard(finished.batch_id)
                if finished.status in {SmartPasteBatchStatus.PARTIAL_FAILED, SmartPasteBatchStatus.FAILED}
                else None
            )
            if markup:
                result = _send_text_with_keyboard(
                    token, chat_id, _build_smart_paste_result_text(finished), markup
                )
                result_message_id = _message_id_from_telegram_result(result)
                if _SMART_PASTE_STATES.set_action_message(
                    chat_id, finished.batch_id, result_message_id
                ):
                    if previous_action_message_id != result_message_id:
                        _clear_inline_keyboard(token, chat_id, previous_action_message_id)
                else:
                    logger.warning(
                        "Smart Paste result message had no usable message ID batch=%s",
                        finished.batch_id,
                    )
            else:
                _send_text(token, chat_id, _build_smart_paste_result_text(finished))
                _clear_inline_keyboard(token, chat_id, previous_action_message_id)
            return {"ok": True}

        message = payload.get("message") or {}
        chat_id = _normalize_chat_id((message.get("chat") or {}).get("id"))
        if not _is_private_owner_message(message, allowed_chat_id) or not chat_id:
            logger.info("Ignore unauthorized Telegram message.")
            return {"ok": True}
        text = (message.get("text") or "").strip()
        if not text:
            return {"ok": True}
        if len(text) > SMART_PASTE_MAX_INPUT_CHARS:
            _send_text(token, chat_id, "Tin nhắn quá dài. Bạn hãy chia thành vài đoạn nhỏ nhé.")
            return {"ok": True}

        update_key = _update_key(payload)
        if update_key:
            previous_batch_id = _SMART_PASTE_STATES.lookup_update(update_key)
            if previous_batch_id:
                previous = _SMART_PASTE_STATES.get_batch(chat_id, previous_batch_id)
                if previous and previous.status == SmartPasteBatchStatus.PENDING:
                    sent = _send_text_with_keyboard(
                        token,
                        chat_id,
                        _build_smart_paste_preview_text(_event_dicts(previous)),
                        _build_smart_paste_keyboard(previous.batch_id),
                    )
                    _SMART_PASTE_STATES.set_preview_message(
                        chat_id, previous.batch_id, _message_id_from_telegram_result(sent)
                    )
                return {"ok": True}

        lowered = text.lower()
        form_state = _ADD_FORM_STATES.get(chat_id)
        command_token = lowered.split(maxsplit=1)[0] if lowered.startswith("/") else ""
        command = command_token.split("@", 1)[0]
        logger.info("Telegram message received command=%s", command or "<text>")

        if command == "/cancel" and lowered == command_token:
            if form_state:
                _ADD_FORM_STATES.pop(chat_id, None)
                _send_text(token, chat_id, "Đã hủy form thêm lịch.")
                return {"ok": True}
            active = _SMART_PASTE_STATES.active_batch_for_chat(chat_id)
            if active:
                action_message_id = active.action_message_id
                cancelled = _SMART_PASTE_STATES.cancel(chat_id, active.batch_id)
                if cancelled and cancelled.status == SmartPasteBatchStatus.CANCELLED:
                    _clear_inline_keyboard(token, chat_id, action_message_id)
                    _send_text(token, chat_id, "Đã hủy các lịch chưa lưu.")
                else:
                    _send_text(token, chat_id, "Batch này đang được xử lý hoặc đã hết hạn.")
            else:
                _send_text(token, chat_id, "Hiện không có form hoặc preview nào đang chờ.")
            return {"ok": True}

        if command == "/done" and lowered == command_token:
            if not form_state:
                _send_text(token, chat_id, "Hiện không có form /add đang chờ.")
                return {"ok": True}
            if not _is_add_form_complete(form_state):
                _send_add_form_step(token, chat_id, form_state, prefix="Bạn chưa điền xong form.")
                return {"ok": True}
            title, appt_date, start_time, location = _build_add_appointment_from_form(form_state)
            appointment_id = str(form_state.setdefault("appointment_id", uuid.uuid4().hex))
            insert_calendar_event(
                title=title,
                appointment_date=appt_date,
                start_time=start_time,
                end_time=None,
                location=location,
                note=_build_add_form_raw_input(form_state),
                appointment_id=appointment_id,
            )
            _ADD_FORM_STATES.pop(chat_id, None)
            _send_text(token, chat_id, _build_appointment_confirmation(title, appt_date, start_time, location))
            return {"ok": True}

        if form_state and not lowered.startswith("/"):
            try:
                reply = _advance_add_form_state(form_state, text)
            except ValueError as exc:
                _send_add_form_step(token, chat_id, form_state, prefix=str(exc))
                return {"ok": True}
            if _is_add_form_complete(form_state):
                _send_text_with_keyboard(token, chat_id, reply, _build_add_form_keyboard())
            else:
                _send_add_form_step(token, chat_id, form_state)
            return {"ok": True}

        if command in {"/start", "/help"} and lowered == command_token:
            _send_text(token, chat_id, START_HELP_TEXT)
            return {"ok": True}
        if command == "/today" and lowered == command_token:
            _, rows, _ = fetch_events_from_calendar(local_today())
            _send_text(token, chat_id, _build_today_appointments_text(rows))
            return {"ok": True}
        if command == "/deadline" and lowered == command_token:
            rows = fetch_tagged_calendar_events(SYNC_SOURCE_DEADLINE)
            keyboard = _build_deadline_keyboard(rows)
            if rows and keyboard["inline_keyboard"]:
                _send_text_with_keyboard(token, chat_id, _build_deadline_list_text(rows), keyboard)
            else:
                _send_text(token, chat_id, _build_deadline_list_text(rows))
            return {"ok": True}
        if command == "/exam" and lowered == command_token:
            rows = fetch_tagged_calendar_events(SYNC_SOURCE_EXAM)
            _send_text(token, chat_id, _build_exam_list_text(rows))
            return {"ok": True}
        if command in {"/schedule", "/scheduel"}:
            parts = text.split(maxsplit=1)
            try:
                target_date = _parse_schedule_day_arg(parts[1] if len(parts) > 1 else None)
            except ValueError as exc:
                _send_text(token, chat_id, str(exc))
                return {"ok": True}
            rows, _, _ = fetch_events_from_calendar(target_date)
            _send_text(token, chat_id, _build_schedule_text(rows, target_date))
            return {"ok": True}
        if command == "/add" and lowered == command_token:
            active = _SMART_PASTE_STATES.active_batch_for_chat(chat_id)
            if active:
                _SMART_PASTE_STATES.cancel(chat_id, active.batch_id)
            state = _new_add_form_state()
            _ADD_FORM_STATES[chat_id] = state
            _send_add_form_step(token, chat_id, state, prefix="Bắt đầu form thêm lịch.")
            return {"ok": True}
        if lowered.startswith("/"):
            _send_text(token, chat_id, START_HELP_TEXT)
            return {"ok": True}
        gemini_res = parse_events_with_gemini(text, reference_date=local_today())
        if gemini_res is None:
            _send_text(
                token,
                chat_id,
                "Mình chưa đọc tự động được đoạn này lúc này. Bạn có thể dùng /add để thêm lịch thủ công.",
            )
            return {"ok": True}
        try:
            extraction = validate_smart_paste_payload(gemini_res)
        except SmartPasteClarificationError as exc:
            question_text = "\n".join(f"- {question}" for question in exc.questions)
            _send_text(
                token,
                chat_id,
                "Mình chưa đủ chắc để tạo lịch. Bạn bổ sung giúp mình:\n" + question_text,
            )
            return {"ok": True}
        except SmartPasteValidationError as exc:
            logger.info("Smart Paste validation rejected code=%s", exc.code)
            _send_text(
                token,
                chat_id,
                "Mình chưa đủ chắc để tạo lịch từ đoạn này. Bạn bổ sung ngày/giờ rõ hơn nhé.",
            )
            return {"ok": True}
        if not extraction.events:
            _send_text(
                token,
                chat_id,
                "Mình không tìm thấy lịch hẹn nào. Bạn có thể dán lịch tự nhiên hoặc dùng /add để thêm thủ công.",
            )
            return {"ok": True}

        fingerprint = smart_paste_fingerprint(text)
        try:
            batch, _ = _SMART_PASTE_STATES.create_batch(chat_id, extraction.events, fingerprint)
        except SmartPasteStateConflict:
            _send_text(
                token,
                chat_id,
                "Bạn hãy xử lý preview Smart Paste trước (thêm, thử lại hoặc hủy) nhé.",
            )
            return {"ok": True}
        if update_key:
            _SMART_PASTE_STATES.remember_update(update_key, batch.batch_id)
        preview_result = _send_text_with_keyboard(
            token,
            chat_id,
            _build_smart_paste_preview_text(_event_dicts(batch)),
            _build_smart_paste_keyboard(batch.batch_id),
        )
        _SMART_PASTE_STATES.set_preview_message(
            chat_id, batch.batch_id, _message_id_from_telegram_result(preview_result)
        )
        return {"ok": True}
    except HTTPException:
        raise
    except TelegramDeliveryError as exc:
        logger.warning("Telegram delivery failed while processing webhook: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Telegram delivery temporarily failed") from exc
    except Exception:
        logger.exception("Webhook processing failed")
        if chat_id:
            try:
                _send_text(token, chat_id, "Mình chưa xử lý được yêu cầu lúc này. Bạn thử lại sau nhé.")
            except TelegramDeliveryError:
                raise HTTPException(status_code=503, detail="Telegram delivery temporarily failed")
        return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("webhook_app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
