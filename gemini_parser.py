"""Gemini-based appointment parser.

The parser converts free-form Telegram text into a structured JSON-like dict.
It prefers explicit JSON output from Gemini and falls back to None if Gemini is
missing or the response cannot be decoded.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from typing import Any

from time_utils import local_today

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash-lite"


def parse_appointment_with_gemini(text: str, *, reference_date: dt.date | None = None) -> dict[str, Any] | None:
    """Parse a natural-language appointment message into structured JSON.

    Returns None when Gemini is unavailable or the response cannot be parsed.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None

    try:
        import google.generativeai as genai
    except Exception as exc:
        logger.warning("Gemini SDK is not available: %s", exc)
        return None

    ref_date = reference_date or local_today()
    genai.configure(api_key=api_key)

    prompt = f"""
You are a strict JSON extractor for personal appointments.
Return ONLY a valid JSON object and nothing else.

Input message:
{text}

Reference date:
{ref_date.isoformat()}

Rules:
- Infer the appointment date in YYYY-MM-DD.
- If time is missing, use null for start_time and end_time.
- If a location exists, put it in location; otherwise null.
- If extra descriptive text exists, put it in note; otherwise null.
- Set title to a short human-readable summary.
- Set confidence between 0 and 1.
- Set needs_clarification to true if the intent is ambiguous or a date/time is unclear.
- If needs_clarification is true, provide a short clarification_question.

JSON schema:
{{
  "title": "string",
  "appointment_date": "YYYY-MM-DD",
  "start_time": "HH:MM:SS or null",
  "end_time": "HH:MM:SS or null",
  "location": "string or null",
  "note": "string or null",
  "confidence": 0.0,
  "needs_clarification": false,
  "clarification_question": "string or null"
}}
""".strip()

    try:
        model = genai.GenerativeModel(DEFAULT_MODEL)
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0,
                "response_mime_type": "application/json",
            },
        )
        raw_text = _extract_text(response)
        payload = _load_json(raw_text)
        if not isinstance(payload, dict):
            return None
        return payload
    except Exception as exc:
        logger.warning("Gemini appointment parse failed: %s", exc)
        return None


def _extract_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text).strip()

    candidates = getattr(response, "candidates", None) or []
    if candidates:
        parts: list[str] = []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", []) or []:
                piece = getattr(part, "text", None)
                if piece:
                    parts.append(str(piece))
        if parts:
            return "".join(parts).strip()

    return ""


def _load_json(raw_text: str) -> Any:
    text = raw_text.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
