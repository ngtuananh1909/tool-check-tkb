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

import requests
from fastapi import FastAPI, Header, HTTPException, Request

from database import create_appointment, get_today_appointments
from gemini_parser import parse_appointment_with_gemini
from telegram_mvp_bot import (
    _build_conversational_reply,
    _build_today_appointments_text,
    _looks_like_appointment_message,
    _normalize_chat_id,
    _normalize_gemini_payload,
    _parse_input,
    _send_text,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

WEBHOOK_PATH = "/telegram/webhook"
HEALTH_PATH = "/health"


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


def _register_webhook(token: str, webhook_url: str, webhook_secret: str | None) -> None:
    payload: dict[str, object] = {
        "url": webhook_url,
        "allowed_updates": ["message"],
        "drop_pending_updates": True,
    }
    if webhook_secret:
        payload["secret_token"] = webhook_secret

    response = requests.post(
        _telegram_api(token, "setWebhook"),
        json=payload,
        timeout=30,
    )
    result: dict[str, object]
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "raw": response.text}

    if not response.ok:
        raise RuntimeError(
            f"Telegram setWebhook failed: status={response.status_code}, body={result}"
        )

    if not result.get("ok"):
        raise RuntimeError(f"Failed to register webhook: {result}")

    logger.info("Telegram webhook registered at %s", webhook_url)


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

    yield

    # Keep shutdown gentle; do not force delete webhook unless explicitly desired.
    if os.environ.get("TELEGRAM_DELETE_WEBHOOK_ON_SHUTDOWN", "").strip().lower() in {"1", "true", "yes"}:
        _delete_webhook(token)


app = FastAPI(lifespan=lifespan)


@app.get(HEALTH_PATH)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    token, allowed_chat_id, _, webhook_secret = _load_env()

    if webhook_secret and x_telegram_bot_api_secret_token != webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret token")

    payload = await request.json()
    message = payload.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = _normalize_chat_id((message.get("chat") or {}).get("id"))

    if not text or not chat_id:
        return {"ok": True}

    if allowed_chat_id and chat_id != allowed_chat_id:
        logger.info("Ignore message from unauthorized chat_id=%s", chat_id)
        return {"ok": True}

    lowered = text.lower()
    try:
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

        gemini_payload = parse_appointment_with_gemini(text)
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

        conf = f"OK. Da tao lich hen: {title} - {appt_date.isoformat()} {start_time[:5]}"
        if location:
            conf += f" - {location}"
        conf += "\nMình đã lưu giúp bạn rồi nè."
        _send_text(token, chat_id, conf)
        return {"ok": True}
    except Exception as exc:
        logger.exception("Webhook processing failed: %s", exc)
        _send_text(token, chat_id, f"Khong tao duoc lich hen: {exc}")
        return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("webhook_app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
