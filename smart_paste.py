"""Domain validation and short-lived state for Telegram Smart Paste.

The webhook deliberately keeps this module independent from FastAPI, Telegram,
Gemini, and Google Calendar.  It owns the trust boundary between model output
and persistence, plus the small in-memory state machine needed while a user is
confirming a preview.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import Enum
from typing import cast

SMART_PASTE_CONFIDENCE_THRESHOLD = 0.6
SMART_PASTE_MAX_INPUT_CHARS = 4096
SMART_PASTE_MAX_EVENTS = 10
SMART_PASTE_MAX_TITLE_CHARS = 120
SMART_PASTE_MAX_LOCATION_CHARS = 200
SMART_PASTE_MAX_NOTE_CHARS = 500
SMART_PASTE_MAX_QUESTION_CHARS = 300
SMART_PASTE_MAX_EVENT_TEXT_CHARS = 3000
SMART_PASTE_PENDING_TTL_SECONDS = 15 * 60
SMART_PASTE_TERMINAL_TTL_SECONDS = 60 * 60
SMART_PASTE_PROCESSING_LEASE_SECONDS = 2 * 60
SMART_PASTE_MAX_BATCHES = 100
SMART_PASTE_MAX_UPDATE_KEYS = 1000

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


class SmartPasteValidationError(ValueError):
    """Raised when untrusted model data cannot be safely persisted."""

    def __init__(self, code: str, message: str = "Invalid Smart Paste event") -> None:
        super().__init__(message)
        self.code = code


class SmartPasteClarificationError(SmartPasteValidationError):
    """Raised when the model explicitly marks one or more events ambiguous."""

    def __init__(self, questions: Iterable[str]) -> None:
        self.questions = tuple(str(q).strip() for q in questions if str(q).strip())
        super().__init__("needs_clarification", "Clarification is required")


class SmartPasteBatchStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class SmartPasteStateConflict(RuntimeError):
    """Raised when a new interaction conflicts with an active batch."""


@dataclass(frozen=True)
class SmartPasteEvent:
    title: str
    appointment_date: dt.date
    start_time: str | None
    end_time: str | None
    location: str | None
    note: str | None
    confidence: float
    event_id: str = ""


@dataclass
class SmartPasteEventState:
    event: SmartPasteEvent
    status: str = "pending"
    calendar_event_id: str | None = None
    error_code: str | None = None


@dataclass
class SmartPasteBatch:
    batch_id: str
    chat_id: str
    fingerprint: str
    events: list[SmartPasteEventState]
    status: SmartPasteBatchStatus = SmartPasteBatchStatus.PENDING
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    preview_message_id: str | None = None
    last_result_message_id: str | None = None

    def snapshot(self) -> SmartPasteBatch:
        return SmartPasteBatch(
            batch_id=self.batch_id,
            chat_id=self.chat_id,
            fingerprint=self.fingerprint,
            events=[replace(item) for item in self.events],
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            expires_at=self.expires_at,
            preview_message_id=self.preview_message_id,
            last_result_message_id=self.last_result_message_id,
        )


@dataclass(frozen=True)
class SmartPasteExtraction:
    """Validated model output ready for preview, but not yet persisted."""

    events: tuple[SmartPasteEvent, ...] = ()
    clarification_questions: tuple[str, ...] = ()

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarification_questions)


def _normalize_text_field(payload: dict, key: str, max_chars: int, *, required: bool) -> str | None:
    if key not in payload:
        if required:
            raise SmartPasteValidationError(f"missing_{key}", f"Missing {key}")
        return None
    value = payload[key]
    if value is None:
        if required:
            raise SmartPasteValidationError(f"null_{key}", f"Null {key}")
        return None
    if type(value) is not str:
        raise SmartPasteValidationError(f"type_{key}", f"Invalid {key} type")
    text = value.strip()
    if required and not text:
        raise SmartPasteValidationError(f"empty_{key}", f"Empty {key}")
    if len(text) > max_chars:
        raise SmartPasteValidationError(f"long_{key}", f"{key} is too long")
    return text or None


def _normalize_time_field(payload: dict, key: str) -> str | None:
    if key not in payload:
        raise SmartPasteValidationError(f"missing_{key}", f"Missing {key}")
    value = payload[key]
    if value is None:
        return None
    if type(value) is not str:
        raise SmartPasteValidationError(f"type_{key}", f"Invalid {key} type")
    text = value.strip()
    match = _TIME_RE.fullmatch(text)
    if not match:
        raise SmartPasteValidationError(f"invalid_{key}", f"Invalid {key}")
    hour, minute, second = (int(part or 0) for part in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        raise SmartPasteValidationError(f"invalid_{key}", f"Invalid {key}")
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def normalize_smart_paste_event(payload: object, *, require_confidence: bool = False) -> SmartPasteEvent:
    """Strictly validate and normalize one untrusted model event."""
    if not isinstance(payload, dict):
        raise SmartPasteValidationError("event_not_object", "Event is not an object")
    allowed_fields = {
        "title",
        "appointment_date",
        "start_time",
        "end_time",
        "location",
        "note",
        "confidence",
        "needs_clarification",
        "clarification_question",
    }
    if set(payload) - allowed_fields:
        raise SmartPasteValidationError("unexpected_event_fields", "Unexpected event fields")

    title = _normalize_text_field(payload, "title", SMART_PASTE_MAX_TITLE_CHARS, required=True)
    date_value = payload.get("appointment_date")
    if type(date_value) is not str or not date_value.strip():
        raise SmartPasteValidationError("invalid_appointment_date", "Invalid appointment date")
    date_text = date_value.strip()
    try:
        appointment_date = dt.date.fromisoformat(date_text)
    except ValueError as exc:
        raise SmartPasteValidationError("invalid_appointment_date", "Invalid appointment date") from exc

    confidence = payload.get("confidence")
    if require_confidence and (type(confidence) not in (int, float) or isinstance(confidence, bool)):
        raise SmartPasteValidationError("invalid_confidence", "Invalid confidence")
    if confidence is None:
        normalized_confidence = 1.0
    else:
        normalized_confidence = float(confidence)
        if not math.isfinite(normalized_confidence) or not 0.0 <= normalized_confidence <= 1.0:
            raise SmartPasteValidationError("invalid_confidence", "Invalid confidence")
        if normalized_confidence < SMART_PASTE_CONFIDENCE_THRESHOLD:
            raise SmartPasteValidationError("low_confidence", "Confidence is below threshold")

    needs = payload.get("needs_clarification", False)
    if type(needs) is not bool:
        raise SmartPasteValidationError("invalid_needs_clarification", "Invalid clarification flag")
    if needs:
        raise SmartPasteClarificationError((_normalize_question(payload.get("clarification_question")),))

    start_time = _normalize_time_field(payload, "start_time")
    end_time = _normalize_time_field(payload, "end_time")
    if end_time is not None and start_time is None:
        raise SmartPasteValidationError("end_without_start", "End time requires a start time")
    if start_time is not None and end_time is not None and end_time <= start_time:
        raise SmartPasteValidationError("end_not_after_start", "End time must be after start time")

    location = _normalize_text_field(
        payload, "location", SMART_PASTE_MAX_LOCATION_CHARS, required=False
    )
    note = _normalize_text_field(payload, "note", SMART_PASTE_MAX_NOTE_CHARS, required=False)
    assert title is not None
    return SmartPasteEvent(
        title=title,
        appointment_date=appointment_date,
        start_time=start_time,
        end_time=end_time,
        location=location,
        note=note,
        confidence=normalized_confidence,
    )


def _normalize_question(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SmartPasteValidationError("missing_clarification_question", "Missing clarification question")
    question = value.strip()
    if len(question) > SMART_PASTE_MAX_QUESTION_CHARS:
        raise SmartPasteValidationError("long_clarification_question", "Clarification question is too long")
    return question


def validate_smart_paste_payload(payload: object) -> SmartPasteExtraction:
    """Validate a complete Gemini payload; one bad/ambiguous event rejects all."""
    if not isinstance(payload, dict):
        raise SmartPasteValidationError("payload_not_object", "Gemini payload is not an object")
    if set(payload) - {"events"}:
        raise SmartPasteValidationError("unexpected_payload_fields", "Unexpected payload fields")
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise SmartPasteValidationError("events_not_list", "Gemini events is not a list")
    if len(raw_events) > SMART_PASTE_MAX_EVENTS:
        raise SmartPasteValidationError("too_many_events", "Too many events")
    if not raw_events:
        return SmartPasteExtraction()

    normalized: list[SmartPasteEvent] = []
    questions: list[str] = []
    for index, raw_event in enumerate(raw_events, start=1):
        if not isinstance(raw_event, dict):
            raise SmartPasteValidationError(f"event_{index}_not_object", "Event is not an object")
        needs = raw_event.get("needs_clarification")
        if type(needs) is not bool:
            raise SmartPasteValidationError(
                f"event_{index}_invalid_needs_clarification", "Invalid clarification flag"
            )
        confidence = raw_event.get("confidence")
        if type(confidence) not in (int, float) or isinstance(confidence, bool):
            raise SmartPasteValidationError(f"event_{index}_invalid_confidence", "Invalid confidence")
        confidence_number = float(cast(float, confidence))
        if not math.isfinite(confidence_number) or not 0.0 <= confidence_number <= 1.0:
            raise SmartPasteValidationError(f"event_{index}_invalid_confidence", "Invalid confidence")
        if confidence_number < SMART_PASTE_CONFIDENCE_THRESHOLD:
            questions.append(
                _normalize_question(raw_event.get("clarification_question"))
                if raw_event.get("clarification_question") is not None
                else f"Bạn có thể bổ sung thông tin cho lịch số {index} không?"
            )
            continue
        if needs:
            questions.append(_normalize_question(raw_event.get("clarification_question")))
            continue
        try:
            normalized.append(normalize_smart_paste_event(raw_event))
        except SmartPasteClarificationError as exc:
            questions.extend(exc.questions)
        except SmartPasteValidationError as exc:
            raise SmartPasteValidationError(f"event_{index}_{exc.code}", str(exc)) from exc

    if questions:
        unique_questions = tuple(dict.fromkeys(questions))[:3]
        raise SmartPasteClarificationError(unique_questions)
    if not normalized:
        raise SmartPasteValidationError("no_valid_events", "No valid events")
    serialized_length = sum(
        len(event.title)
        + len(event.location or "")
        + len(event.note or "")
        + 40
        for event in normalized
    )
    if serialized_length > SMART_PASTE_MAX_EVENT_TEXT_CHARS:
        raise SmartPasteValidationError("event_text_too_long", "Event data is too long")
    return SmartPasteExtraction(events=tuple(normalized))


def smart_paste_fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def _now_default() -> float:
    return time.monotonic()


@dataclass
class _UpdateRecord:
    seen_at: float
    batch_id: str | None


class SmartPasteStateStore:
    """Small lock-protected in-memory state store for one webhook process."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or _now_default
        self._lock = threading.RLock()
        self._batches: dict[str, SmartPasteBatch] = {}
        self._active_by_chat: dict[str, str] = {}
        self._updates: dict[str, _UpdateRecord] = {}

    @property
    def batches(self) -> dict[str, SmartPasteBatch]:
        with self._lock:
            self._cleanup_locked(self._clock())
            return {key: value.snapshot() for key, value in self._batches.items()}

    def _cleanup_locked(self, now: float) -> None:
        for batch_id, batch in list(self._batches.items()):
            if batch.expires_at and now >= batch.expires_at:
                if batch.status in {
                    SmartPasteBatchStatus.PENDING,
                    SmartPasteBatchStatus.PARTIAL_FAILED,
                    SmartPasteBatchStatus.FAILED,
                }:
                    batch.status = SmartPasteBatchStatus.EXPIRED
                if (
                    self._active_by_chat.get(batch.chat_id) == batch_id
                    and batch.status != SmartPasteBatchStatus.PROCESSING
                ):
                    self._active_by_chat.pop(batch.chat_id, None)
                if now >= batch.expires_at + SMART_PASTE_TERMINAL_TTL_SECONDS:
                    if self._active_by_chat.get(batch.chat_id) == batch_id:
                        self._active_by_chat.pop(batch.chat_id, None)
                    self._batches.pop(batch_id, None)
        for update_key, record in list(self._updates.items()):
            if now - record.seen_at >= SMART_PASTE_TERMINAL_TTL_SECONDS:
                self._updates.pop(update_key, None)
        if len(self._batches) > SMART_PASTE_MAX_BATCHES:
            removable = sorted(
                (
                    batch
                    for batch in self._batches.values()
                    if batch.status
                    not in {SmartPasteBatchStatus.PENDING, SmartPasteBatchStatus.PROCESSING}
                ),
                key=lambda item: item.updated_at,
            )
            for batch in removable[: max(0, len(self._batches) - SMART_PASTE_MAX_BATCHES)]:
                self._batches.pop(batch.batch_id, None)

    def lookup_update(self, update_key: str) -> str | None:
        with self._lock:
            now = self._clock()
            self._cleanup_locked(now)
            record = self._updates.get(update_key)
            return record.batch_id if record else None

    def active_batch_for_chat(self, chat_id: str) -> SmartPasteBatch | None:
        with self._lock:
            self._cleanup_locked(self._clock())
            batch_id = self._active_by_chat.get(chat_id)
            batch = self._batches.get(batch_id or "")
            return batch.snapshot() if batch else None

    def clear(self) -> None:
        with self._lock:
            self._batches.clear()
            self._active_by_chat.clear()
            self._updates.clear()

    def __contains__(self, chat_id: object) -> bool:
        return isinstance(chat_id, str) and self.active_batch_for_chat(chat_id) is not None

    def __getitem__(self, chat_id: str) -> dict[str, object]:
        batch = self.active_batch_for_chat(chat_id)
        if not batch:
            raise KeyError(chat_id)
        return {
            "batch_id": batch.batch_id,
            "events": [
                {
                    "title": item.event.title,
                    "appointment_date": item.event.appointment_date,
                    "start_time": item.event.start_time,
                    "end_time": item.event.end_time,
                    "location": item.event.location,
                    "note": item.event.note,
                }
                for item in batch.events
            ],
            "original_text": "",
        }

    def __len__(self) -> int:
        with self._lock:
            self._cleanup_locked(self._clock())
            return len(self._active_by_chat)

    def pop(self, chat_id: str, default: object = None) -> object:
        with self._lock:
            batch_id = self._active_by_chat.pop(chat_id, None)
            if not batch_id:
                return default
            batch = self._batches.pop(batch_id, None)
            if not batch:
                return default
            return {
                "batch_id": batch.batch_id,
                "events": [
                    {
                        "title": item.event.title,
                        "appointment_date": item.event.appointment_date,
                        "start_time": item.event.start_time,
                        "end_time": item.event.end_time,
                        "location": item.event.location,
                        "note": item.event.note,
                    }
                    for item in batch.events
                ],
                "original_text": "",
            }

    def __setitem__(self, chat_id: str, value: dict[str, object]) -> None:
        """Compatibility adapter for existing tests and local tooling."""
        raw_events = value.get("events") if isinstance(value, dict) else None
        if not isinstance(raw_events, list):
            raise TypeError("Smart Paste state events must be a list")
        events: list[SmartPasteEvent] = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                raise TypeError("Smart Paste state event must be an object")
            appt_date = raw.get("appointment_date")
            if isinstance(appt_date, str):
                appt_date = dt.date.fromisoformat(appt_date)
            if not isinstance(appt_date, dt.date):
                raise TypeError("Smart Paste state date is invalid")
            events.append(
                SmartPasteEvent(
                    title=str(raw.get("title") or "Lịch hẹn"),
                    appointment_date=appt_date,
                    start_time=raw.get("start_time") if isinstance(raw.get("start_time"), str) else None,
                    end_time=raw.get("end_time") if isinstance(raw.get("end_time"), str) else None,
                    location=raw.get("location") if isinstance(raw.get("location"), str) else None,
                    note=raw.get("note") if isinstance(raw.get("note"), str) else None,
                    confidence=1.0,
                )
            )
        with self._lock:
            active_id = self._active_by_chat.get(chat_id)
            if active_id:
                self._batches.pop(active_id, None)
            now = self._clock()
            batch_id = uuid.uuid4().hex
            states = [
                SmartPasteEventState(
                    replace(event, event_id=uuid.uuid5(uuid.NAMESPACE_URL, f"legacy:{batch_id}:{i}").hex)
                )
                for i, event in enumerate(events)
            ]
            self._batches[batch_id] = SmartPasteBatch(
                batch_id=batch_id,
                chat_id=chat_id,
                fingerprint="legacy",
                events=states,
                created_at=now,
                updated_at=now,
                expires_at=now + SMART_PASTE_PENDING_TTL_SECONDS,
            )
            self._active_by_chat[chat_id] = batch_id

    def __delitem__(self, chat_id: str) -> None:
        with self._lock:
            batch_id = self._active_by_chat.pop(chat_id)
            self._batches.pop(batch_id, None)

    def remember_update(self, update_key: str, batch_id: str | None) -> None:
        with self._lock:
            now = self._clock()
            self._cleanup_locked(now)
            self._updates[update_key] = _UpdateRecord(now, batch_id)
            if len(self._updates) > SMART_PASTE_MAX_UPDATE_KEYS:
                oldest = sorted(self._updates.items(), key=lambda item: item[1].seen_at)
                for key, _ in oldest[: len(self._updates) - SMART_PASTE_MAX_UPDATE_KEYS]:
                    self._updates.pop(key, None)

    def create_batch(
        self,
        chat_id: str,
        events: Iterable[SmartPasteEvent],
        fingerprint: str,
    ) -> tuple[SmartPasteBatch, bool]:
        with self._lock:
            now = self._clock()
            self._cleanup_locked(now)
            active_id = self._active_by_chat.get(chat_id)
            active = self._batches.get(active_id or "")
            if active and active.status == SmartPasteBatchStatus.PROCESSING:
                raise SmartPasteStateConflict("A Smart Paste batch is processing")
            if active and active.status == SmartPasteBatchStatus.PENDING and active.fingerprint == fingerprint:
                return active.snapshot(), False
            if active and active.status in {
                SmartPasteBatchStatus.PARTIAL_FAILED,
                SmartPasteBatchStatus.FAILED,
            }:
                raise SmartPasteStateConflict("Resolve the previous failed batch first")
            if active and active.status == SmartPasteBatchStatus.PENDING:
                active.status = SmartPasteBatchStatus.SUPERSEDED
                active.updated_at = now
                active.expires_at = now + SMART_PASTE_TERMINAL_TTL_SECONDS

            batch_id = uuid.uuid4().hex
            event_states: list[SmartPasteEventState] = []
            for index, event in enumerate(events):
                event_id = uuid.uuid5(
                    uuid.NAMESPACE_URL, f"tool-check-tkb:smart-paste:{batch_id}:{index}"
                ).hex
                event_states.append(SmartPasteEventState(replace(event, event_id=event_id)))
            batch = SmartPasteBatch(
                batch_id=batch_id,
                chat_id=chat_id,
                fingerprint=fingerprint,
                events=event_states,
                created_at=now,
                updated_at=now,
                expires_at=now + SMART_PASTE_PENDING_TTL_SECONDS,
            )
            self._batches[batch_id] = batch
            self._active_by_chat[chat_id] = batch_id
            self._cleanup_locked(now)
            return batch.snapshot(), True

    def get_batch(self, chat_id: str, batch_id: str) -> SmartPasteBatch | None:
        with self._lock:
            self._cleanup_locked(self._clock())
            batch = self._batches.get(batch_id)
            if not batch or batch.chat_id != chat_id:
                return None
            return batch.snapshot()

    def get(self, chat_id: str, batch_id: str | None = None, default: object = None) -> object:
        """Support both the domain lookup and the old chat-keyed mapping API."""
        if batch_id is None:
            try:
                return self[chat_id]
            except KeyError:
                return default
        return self.get_batch(chat_id, batch_id)

    def set_preview_message(self, chat_id: str, batch_id: str, message_id: str | None) -> bool:
        with self._lock:
            batch = self._batches.get(batch_id)
            if not batch or batch.chat_id != chat_id or batch.status != SmartPasteBatchStatus.PENDING:
                return False
            batch.preview_message_id = message_id
            return True

    def start_processing(self, chat_id: str, batch_id: str, message_id: str | None) -> tuple[str, SmartPasteBatch | None]:
        with self._lock:
            now = self._clock()
            self._cleanup_locked(now)
            batch = self._batches.get(batch_id)
            if not batch or batch.chat_id != chat_id:
                return "missing", None
            if batch.status in {SmartPasteBatchStatus.PENDING, SmartPasteBatchStatus.PARTIAL_FAILED, SmartPasteBatchStatus.FAILED}:
                if batch.status == SmartPasteBatchStatus.PENDING and now >= batch.expires_at:
                    batch.status = SmartPasteBatchStatus.EXPIRED
                    return "expired", batch.snapshot()
                if batch.preview_message_id and batch.preview_message_id != message_id:
                    return "wrong_message", batch.snapshot()
                batch.status = SmartPasteBatchStatus.PROCESSING
                batch.updated_at = now
                batch.expires_at = now + SMART_PASTE_PROCESSING_LEASE_SECONDS
                return "started", batch.snapshot()
            if batch.status == SmartPasteBatchStatus.PROCESSING:
                if now >= batch.expires_at:
                    batch.updated_at = now
                    batch.expires_at = now + SMART_PASTE_PROCESSING_LEASE_SECONDS
                    return "started", batch.snapshot()
                return "processing", batch.snapshot()
            return batch.status.value, batch.snapshot()

    def record_success(self, chat_id: str, batch_id: str, event_id: str, calendar_event_id: str) -> None:
        with self._lock:
            batch = self._batches.get(batch_id)
            if not batch or batch.chat_id != chat_id:
                return
            for item in batch.events:
                if item.event.event_id == event_id:
                    item.status = "succeeded"
                    item.calendar_event_id = calendar_event_id
                    item.error_code = None
                    batch.updated_at = self._clock()
                    return

    def record_failure(self, chat_id: str, batch_id: str, event_id: str, error_code: str) -> None:
        with self._lock:
            batch = self._batches.get(batch_id)
            if not batch or batch.chat_id != chat_id:
                return
            for item in batch.events:
                if item.event.event_id == event_id:
                    item.status = "failed"
                    item.error_code = error_code
                    batch.updated_at = self._clock()
                    return

    def finish_processing(self, chat_id: str, batch_id: str) -> SmartPasteBatch | None:
        with self._lock:
            now = self._clock()
            batch = self._batches.get(batch_id)
            if not batch or batch.chat_id != chat_id:
                return None
            succeeded = sum(item.status == "succeeded" for item in batch.events)
            if succeeded == len(batch.events):
                batch.status = SmartPasteBatchStatus.COMPLETED
            elif succeeded:
                batch.status = SmartPasteBatchStatus.PARTIAL_FAILED
            else:
                batch.status = SmartPasteBatchStatus.FAILED
            batch.updated_at = now
            batch.expires_at = now + (
                SMART_PASTE_TERMINAL_TTL_SECONDS
                if batch.status == SmartPasteBatchStatus.COMPLETED
                else SMART_PASTE_PENDING_TTL_SECONDS
            )
            if self._active_by_chat.get(chat_id) == batch_id and batch.status == SmartPasteBatchStatus.COMPLETED:
                self._active_by_chat.pop(chat_id, None)
            return batch.snapshot()

    def cancel(self, chat_id: str, batch_id: str) -> SmartPasteBatch | None:
        with self._lock:
            batch = self._batches.get(batch_id)
            if not batch or batch.chat_id != chat_id:
                return None
            if batch.status in {SmartPasteBatchStatus.PENDING, SmartPasteBatchStatus.PARTIAL_FAILED, SmartPasteBatchStatus.FAILED}:
                batch.status = SmartPasteBatchStatus.CANCELLED
                batch.updated_at = self._clock()
                batch.expires_at = batch.updated_at + SMART_PASTE_TERMINAL_TTL_SECONDS
                if self._active_by_chat.get(chat_id) == batch_id:
                    self._active_by_chat.pop(chat_id, None)
            return batch.snapshot()
