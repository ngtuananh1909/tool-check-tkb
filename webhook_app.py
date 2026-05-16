"""FastAPI Telegram webhook for appointment creation.

This is the production-friendly replacement for long polling:
- Telegram sends updates to POST /telegram/webhook
- The app parses the message (Gemini JSON first, rule-based fallback)
- The appointment is stored in Supabase

Environment variables:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID (optional; if set, only accept this chat)
    TELEGRAM_WEBHOOK_URL (optional; public HTTPS URL for auto-register)
    TELEGRAM_WEBHOOK_SECRET (optional; secret token checked on incoming requests)
    GEMINI_API_KEY (optional)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, Header, HTTPException, Request

from database import (
    create_appointment,
    get_nearest_elearning_deadlines,
    get_today_appointments,
    get_today_class_sessions,
)
from gemini_parser import parse_appointment_with_gemini
from telegram_mvp_bot import (
    ADD_FORM_CANCEL_CALLBACK,
    ADD_FORM_DONE_CALLBACK,
    _advance_add_form_state,
    _build_add_appointment_from_form,
    _build_add_form_keyboard,
    _build_add_form_prompt,
    _build_add_form_raw_input,
    _build_conversational_reply,
    _build_appointment_confirmation,
    _build_deadline_detail_text,
    _build_deadline_keyboard,
    _build_deadline_list_text,
    _build_schedule_text,
    _build_today_appointments_text,
    _deadline_callback_key,
    _looks_like_appointment_message,
    _normalize_chat_id,
    _normalize_gemini_payload,
    _new_add_form_state,
    _parse_add_appointment_payload,
    _parse_input,
    _parse_schedule_day_arg,
    _send_text,
    _send_text_with_keyboard,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

WEBHOOK_PATH = "/telegram/webhook"
HEALTH_PATH = "/health"
WEBHOOK_INFO_PATH = "/telegram/webhook/info"
GEMINI_HEALTH_PATH = "/gemini/health"
_ADD_FORM_STATES: dict[str, dict[str, object]] = {}


def _telegram_api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _load_env() -> tuple[str, str, str, str | None]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
    webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip() or None

    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN.")

    return token, allowed_chat_id, webhook_url, webhook_secret


def _safe_url_label(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return "<invalid-url>"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _telegram_post(token: str, method: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    response = requests.post(
        _telegram_api(token, method),
        json=payload or {},
        timeout=30,
    )
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "raw": response.text}

    if not response.ok:
        raise RuntimeError(
            f"Telegram {method} failed: status={response.status_code}, body={result}"
        )
    return result


def _get_webhook_info(token: str) -> dict[str, object]:
    return _telegram_post(token, "getWebhookInfo")


def _sanitize_webhook_info(info: dict[str, object]) -> dict[str, object]:
    result = info.get("result") if isinstance(info.get("result"), dict) else {}
    assert isinstance(result, dict)
    return {
        "ok": bool(info.get("ok")),
        "url": result.get("url") or None,
        "pending_update_count": result.get("pending_update_count", 0),
        "last_error_message": result.get("last_error_message") or None,
    }


def _register_webhook(token: str, webhook_url: str, webhook_secret: str | None) -> None:
    payload: dict[str, object] = {
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }
    if webhook_secret:
        payload["secret_token"] = webhook_secret

    logger.info(
        "Registering Telegram webhook url=%s secret_set=%s",
        _safe_url_label(webhook_url),
        bool(webhook_secret),
    )
    result = _telegram_post(token, "setWebhook", payload)

    if not result.get("ok"):
        raise RuntimeError(f"Failed to register webhook: {result}")

    logger.info("Telegram webhook registered: %s", result.get("description") or "ok")
    try:
        logger.info("Telegram webhook info: %s", _sanitize_webhook_info(_get_webhook_info(token)))
    except Exception as exc:
        logger.warning("Could not fetch Telegram webhook info after registration: %s", exc)


def _register_command_menu(token: str) -> None:
    commands = [
        {"command": "start", "description": "Hướng dẫn sử dụng bot"},
        {"command": "today", "description": "Xem lịch hẹn hôm nay"},
        {"command": "schedule", "description": "Xem lịch học"},
        {"command": "deadline", "description": "Xem deadline eLearning"},
        {"command": "add", "description": "Thêm lịch hẹn theo mẫu"},
    ]
    result = _telegram_post(token, "setMyCommands", {"commands": commands})
    if not result.get("ok"):
        raise RuntimeError(f"Failed to register Telegram command menu: {result}")
    logger.info("Telegram command menu registered with %d command(s).", len(commands))


def _delete_webhook(token: str) -> None:
    try:
        response = requests.post(
            _telegram_api(token, "deleteWebhook"),
            json={"drop_pending_updates": False},
            timeout=30,
        )
        response.raise_for_status()
        logger.info("Telegram webhook deleted.")
    except Exception as exc:
        logger.warning("Could not delete webhook cleanly: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    token, _, webhook_url, webhook_secret = _load_env()

    if webhook_url:
        try:
            _register_webhook(token, webhook_url, webhook_secret)
        except Exception as exc:
            logger.warning("Webhook auto-registration failed: %s", exc)
    else:
        logger.info("TELEGRAM_WEBHOOK_URL not set; webhook auto-registration skipped.")

    try:
        _register_command_menu(token)
    except Exception as exc:
        logger.warning("Command menu registration failed: %s", exc)

    yield

    # Keep shutdown gentle; do not force delete webhook unless explicitly desired.
    if os.environ.get("TELEGRAM_DELETE_WEBHOOK_ON_SHUTDOWN", "").strip().lower() in {"1", "true", "yes"}:
        _delete_webhook(token)


app = FastAPI(lifespan=lifespan)


@app.get(HEALTH_PATH)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(WEBHOOK_INFO_PATH)
def webhook_info() -> dict[str, object]:
    token, _, _, _ = _load_env()
    return _sanitize_webhook_info(_get_webhook_info(token))


@app.get(GEMINI_HEALTH_PATH)
def gemini_health() -> dict[str, object]:
    api_key_set = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    sdk_available = False
    error: str | None = None
    try:
        import google.generativeai  # noqa: F401
        sdk_available = True
    except Exception as exc:
        error = str(exc)
    return {"api_key_set": api_key_set, "sdk_available": sdk_available, "error": error}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    token, allowed_chat_id, _, webhook_secret = _load_env()

    if webhook_secret and x_telegram_bot_api_secret_token != webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret token")

    payload = await request.json()
    callback_query = payload.get("callback_query") or {}
    if callback_query:
        message = callback_query.get("message") or {}
        chat_id = _normalize_chat_id((message.get("chat") or {}).get("id"))
        logger.info("Telegram callback received chat_id=%s data_prefix=%s", chat_id, str(callback_query.get("data") or "").split(":", 1)[0])
        if allowed_chat_id and chat_id != allowed_chat_id:
            logger.info("Ignore callback from unauthorized chat_id=%s", chat_id)
            return {"ok": True}
        data = str(callback_query.get("data") or "")
        if data.startswith("deadline:"):
            key = data.split(":", 1)[1]
            rows = get_nearest_elearning_deadlines()
            selected = next((row for row in rows if _deadline_callback_key(row) == key), None)
            requests.post(
                _telegram_api(token, "answerCallbackQuery"),
                json={"callback_query_id": callback_query.get("id")},
                timeout=10,
            )
            _send_text(token, chat_id, _build_deadline_detail_text(selected))
        elif data == ADD_FORM_CANCEL_CALLBACK:
            _ADD_FORM_STATES.pop(chat_id, None)
            requests.post(
                _telegram_api(token, "answerCallbackQuery"),
                json={"callback_query_id": callback_query.get("id")},
                timeout=10,
            )
            _send_text(token, chat_id, "Đã hủy form thêm lịch.")
        elif data == ADD_FORM_DONE_CALLBACK:
            state = _ADD_FORM_STATES.get(chat_id)
            requests.post(
                _telegram_api(token, "answerCallbackQuery"),
                json={"callback_query_id": callback_query.get("id")},
                timeout=10,
            )
            if not state:
                _send_text(token, chat_id, "Form đã hết hạn. Bạn dùng /add để tạo lại nhé.")
                return {"ok": True}
            if not bool(state.get("awaiting_confirm")):
                _send_text(token, chat_id, "Bạn chưa điền xong form. " + _build_add_form_prompt(state))
                return {"ok": True}
            title, appt_date, start_time, location = _build_add_appointment_from_form(state)
            create_appointment(
                title=title,
                appointment_date=appt_date,
                start_time=start_time,
                end_time=None,
                location=location,
                note=None,
                raw_user_input=_build_add_form_raw_input(state),
                gemini_confidence=None,
            )
            _ADD_FORM_STATES.pop(chat_id, None)
            _send_text(token, chat_id, _build_appointment_confirmation(title, appt_date, start_time, location))
        return {"ok": True}

    message = payload.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = _normalize_chat_id((message.get("chat") or {}).get("id"))

    if not text or not chat_id:
        return {"ok": True}

    if allowed_chat_id and chat_id != allowed_chat_id:
        logger.info("Ignore message from unauthorized chat_id=%s", chat_id)
        return {"ok": True}

    lowered = text.lower()
    form_state = _ADD_FORM_STATES.get(chat_id)
    logger.info("Telegram message received chat_id=%s command=%s", chat_id, text.split(maxsplit=1)[0] if text.startswith("/") else "<text>")
    try:
        if lowered == "/cancel" and form_state:
            _ADD_FORM_STATES.pop(chat_id, None)
            _send_text(token, chat_id, "Đã hủy form thêm lịch.")
            return {"ok": True}

        if lowered == "/done" and form_state:
            if not bool(form_state.get("awaiting_confirm")):
                _send_text(token, chat_id, "Bạn chưa điền xong form. " + _build_add_form_prompt(form_state))
                return {"ok": True}
            title, appt_date, start_time, location = _build_add_appointment_from_form(form_state)
            create_appointment(
                title=title,
                appointment_date=appt_date,
                start_time=start_time,
                end_time=None,
                location=location,
                note=None,
                raw_user_input=_build_add_form_raw_input(form_state),
                gemini_confidence=None,
            )
            _ADD_FORM_STATES.pop(chat_id, None)
            _send_text(token, chat_id, _build_appointment_confirmation(title, appt_date, start_time, location))
            return {"ok": True}

        if form_state and (not lowered.startswith("/") or lowered in {"/skip", "skip"}):
            reply = _advance_add_form_state(form_state, text)
            if bool(form_state.get("awaiting_confirm")):
                _send_text_with_keyboard(token, chat_id, reply, _build_add_form_keyboard())
            else:
                _send_text(token, chat_id, reply)
            return {"ok": True}

        if lowered in {"/start", "/help"}:
            _send_text(
                token,
                chat_id,
                "MVP format:\n"
                "tieude-thoigian-diadiem(optional)\n\n"
                "Vi du:\n"
                "họp nhóm-15/04 14:00-B402\n"
                "đi khám-2026-04-16 09:30\n"
                "gym-18:00",
            )
            return {"ok": True}

        if lowered == "/today":
            rows = get_today_appointments()
            _send_text(token, chat_id, _build_today_appointments_text(rows))
            return {"ok": True}

        if lowered == "/deadline":
            rows = get_nearest_elearning_deadlines()
            keyboard = _build_deadline_keyboard(rows)
            if rows and keyboard["inline_keyboard"]:
                _send_text_with_keyboard(token, chat_id, _build_deadline_list_text(rows), keyboard)
            else:
                _send_text(token, chat_id, _build_deadline_list_text(rows))
            return {"ok": True}

        if lowered.startswith("/schedule") or lowered.startswith("/scheduel"):
            parts = text.split(maxsplit=1)
            try:
                target_date = _parse_schedule_day_arg(parts[1] if len(parts) > 1 else None)
            except ValueError as exc:
                _send_text(token, chat_id, str(exc))
                return {"ok": True}
            rows = get_today_class_sessions(target_date=target_date)
            _send_text(token, chat_id, _build_schedule_text(rows, target_date))
            return {"ok": True}

        if lowered == "/add":
            state = _new_add_form_state()
            _ADD_FORM_STATES[chat_id] = state
            _send_text(token, chat_id, "Bắt đầu form thêm lịch.\n" + _build_add_form_prompt(state))
            return {"ok": True}

        structured_add = any(label in lowered for label in ("thời gian:", "thoi gian:", "time:", "job:", "where:", "địa điểm:", "dia diem:"))
        gemini_payload = None if structured_add else parse_appointment_with_gemini(text)
        if gemini_payload:
            if gemini_payload.get("needs_clarification", False):
                if _looks_like_appointment_message(text):
                    question = gemini_payload.get("clarification_question") or (
                        "Mình chưa hiểu rõ lịch hẹn này, bạn gửi lại giúp mình theo format: tiêu đề-thời gian-địa điểm(optional) nhé."
                    )
                    _send_text(token, chat_id, str(question))
                else:
                    _send_text(token, chat_id, _build_conversational_reply(text))
                return {"ok": True}

            (
                title,
                appt_date,
                start_time,
                end_time,
                location,
                note,
                confidence,
            ) = _normalize_gemini_payload(gemini_payload)
        elif structured_add:
            try:
                title, appt_date, start_time, location = _parse_add_appointment_payload(text)
            except ValueError as exc:
                _send_text(token, chat_id, str(exc))
                return {"ok": True}
            end_time = None
            note = None
            confidence = None
        else:
            try:
                title, appt_date, start_time, location = _parse_input(text)
            except ValueError:
                _send_text(token, chat_id, _build_conversational_reply(text))
                return {"ok": True}
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
        return {"ok": True}
    except Exception as exc:
        logger.exception("Webhook processing failed: %s", exc)
        _send_text(token, chat_id, f"Khong tao duoc lich hen: {exc}")
        return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("webhook_app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
