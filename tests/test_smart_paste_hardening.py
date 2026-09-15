import datetime as dt
import os
import unittest
from typing import ClassVar
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError
from httplib2 import Response

import calendar_sync
import webhook_app
from calendar_sync import CalendarConfigurationError, CalendarPersistenceError
from smart_paste import (
    SMART_PASTE_MAX_EVENTS,
    SMART_PASTE_MAX_INPUT_CHARS,
    SMART_PASTE_PENDING_TTL_SECONDS,
    SmartPasteBatchStatus,
    SmartPasteClarificationError,
    SmartPasteStateStore,
    SmartPasteValidationError,
    normalize_smart_paste_event,
    validate_smart_paste_payload,
)
from telegram_mvp_bot import (
    SMART_PASTE_ADD_PREFIX,
    TelegramDeliveryError,
    _send_message_payload,
)


def _event_payload(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "title": "Họp nhóm",
        "appointment_date": "2026-09-15",
        "start_time": "14:00",
        "end_time": None,
        "location": "B402",
        "note": None,
        "confidence": 0.95,
        "needs_clarification": False,
        "clarification_question": None,
    }
    value.update(overrides)
    return value


class SmartPasteValidationHardeningTests(unittest.TestCase):
    def test_valid_event_is_strictly_normalized(self) -> None:
        extraction = validate_smart_paste_payload({"events": [_event_payload()]})
        self.assertEqual(extraction.events[0].start_time, "14:00:00")
        self.assertEqual(extraction.events[0].appointment_date, dt.date(2026, 9, 15))

    def test_invalid_non_null_time_does_not_become_all_day(self) -> None:
        with self.assertRaises(SmartPasteValidationError) as ctx:
            validate_smart_paste_payload({"events": [_event_payload(start_time="25:00")]})
        self.assertIn("invalid_start_time", str(ctx.exception.code))

    def test_missing_confidence_fails_closed(self) -> None:
        event = _event_payload()
        event.pop("confidence")
        with self.assertRaises(SmartPasteValidationError):
            validate_smart_paste_payload({"events": [event]})

    def test_malformed_confidence_fails_closed(self) -> None:
        for confidence in ("very confident", True, float("nan"), 1.1, -0.1):
            with self.subTest(confidence=confidence), self.assertRaises(SmartPasteValidationError):
                validate_smart_paste_payload({"events": [_event_payload(confidence=confidence)]})

    def test_end_time_must_follow_start(self) -> None:
        for end_time in ("14:00", "13:59"):
            with self.subTest(end_time=end_time), self.assertRaises(SmartPasteValidationError):
                validate_smart_paste_payload({"events": [_event_payload(end_time=end_time)]})

    def test_end_without_start_is_invalid(self) -> None:
        with self.assertRaises(SmartPasteValidationError):
            validate_smart_paste_payload(
                {"events": [_event_payload(start_time=None, end_time="15:00")]}
            )

    def test_all_day_event_remains_valid(self) -> None:
        extraction = validate_smart_paste_payload(
            {"events": [_event_payload(start_time=None, end_time=None)]}
        )
        self.assertIsNone(extraction.events[0].start_time)

    def test_any_ambiguous_event_rejects_the_whole_batch(self) -> None:
        with self.assertRaises(SmartPasteClarificationError) as ctx:
            validate_smart_paste_payload(
                {
                    "events": [
                        _event_payload(),
                        _event_payload(
                            needs_clarification=True,
                            clarification_question="Mấy giờ vậy?",
                        ),
                    ]
                }
            )
        self.assertEqual(ctx.exception.questions, ("Mấy giờ vậy?",))

    def test_normalize_helper_rejects_non_string_fields(self) -> None:
        with self.assertRaises(SmartPasteValidationError):
            normalize_smart_paste_event(_event_payload(title={"bad": "value"}))

    def test_event_count_and_field_limits_fail_closed(self) -> None:
        with self.assertRaises(SmartPasteValidationError):
            validate_smart_paste_payload(
                {"events": [_event_payload(title=f"event-{i}") for i in range(SMART_PASTE_MAX_EVENTS + 1)]}
            )
        with self.assertRaises(SmartPasteValidationError):
            validate_smart_paste_payload(
                {"events": [_event_payload(note="x" * (SMART_PASTE_MAX_INPUT_CHARS + 1))]}
            )


class SmartPasteStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [100.0]
        self.store = SmartPasteStateStore(clock=lambda: self.now[0])

    def test_duplicate_pending_fingerprint_reuses_batch(self) -> None:
        event = normalize_smart_paste_event(_event_payload())
        first, created = self.store.create_batch("123", [event], "same")
        second, reused = self.store.create_batch("123", [event], "same")
        self.assertTrue(created)
        self.assertFalse(reused)
        self.assertEqual(first.batch_id, second.batch_id)

    def test_different_pending_batch_supersedes_old_batch(self) -> None:
        event = normalize_smart_paste_event(_event_payload())
        old, _ = self.store.create_batch("123", [event], "old")
        new, _ = self.store.create_batch("123", [event], "new")
        self.assertNotEqual(old.batch_id, new.batch_id)
        self.assertEqual(self.store.get("123", old.batch_id).status, SmartPasteBatchStatus.SUPERSEDED)

    def test_expired_pending_batch_cannot_start(self) -> None:
        event = normalize_smart_paste_event(_event_payload())
        batch, _ = self.store.create_batch("123", [event], "same")
        self.now[0] += SMART_PASTE_PENDING_TTL_SECONDS + 1
        status, _ = self.store.start_processing("123", batch.batch_id, None)
        self.assertEqual(status, "expired")

    def test_processing_batch_can_be_retried_after_stale_lease(self) -> None:
        event = normalize_smart_paste_event(_event_payload())
        batch, _ = self.store.create_batch("123", [event], "same")
        status, _ = self.store.start_processing("123", batch.batch_id, None)
        self.assertEqual(status, "started")
        self.now[0] += 121
        status, _ = self.store.start_processing("123", batch.batch_id, None)
        self.assertEqual(status, "started")


class SmartPasteWebhookHardeningTests(unittest.TestCase):
    ENV: ClassVar[dict[str, str]] = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_CHAT_ID": "123",
        "TELEGRAM_WEBHOOK_SECRET": "test-secret",
    }

    def setUp(self) -> None:
        webhook_app._SMART_PASTE_STATES.clear()
        webhook_app._ADD_FORM_STATES.clear()

    @staticmethod
    def _telegram_response(message_id: str = "m1") -> MagicMock:
        response = MagicMock(ok=True, status_code=200)
        response.json.return_value = {"ok": True, "result": {"message_id": message_id}}
        return response

    @staticmethod
    def _message(text: str = "Mai 14h họp", message_id: int = 1) -> dict:
        return {
            "message_id": message_id,
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 123},
            "text": text,
        }

    def test_confirmation_uses_batch_id_and_keeps_partial_state(self) -> None:
        parsed = {"events": [_event_payload(), _event_payload(title="Review")]}
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.parse_events_with_gemini", return_value=parsed
        ), patch("requests.post", return_value=self._telegram_response()):
            webhook_app.telegram_webhook(
                {"update_id": 1, "message": self._message()}, "test-secret"
            )
        batch = webhook_app._SMART_PASTE_STATES.active_batch_for_chat("123")
        self.assertIsNotNone(batch)
        assert batch is not None
        callback = {
            "update_id": 2,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 123},
                "message": {
                    "message_id": "m1",
                    "chat": {"id": 123, "type": "private"},
                },
                "data": f"{SMART_PASTE_ADD_PREFIX}{batch.batch_id}",
            },
        }
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.insert_calendar_event", side_effect=["calendar-a", RuntimeError("boom")]
        ) as insert, patch("requests.post", return_value=self._telegram_response()):
            result = webhook_app.telegram_webhook(callback, "test-secret")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(insert.call_count, 2)
        current = webhook_app._SMART_PASTE_STATES.active_batch_for_chat("123")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status, SmartPasteBatchStatus.PARTIAL_FAILED)

    def test_invalid_secret_is_rejected_before_processing(self) -> None:
        with patch.dict(os.environ, self.ENV, clear=False), self.assertRaises(Exception) as ctx:
            webhook_app.telegram_webhook(
                {"update_id": 1, "message": self._message()}, "wrong-secret"
            )
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)

    def test_webhook_security_configuration_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_CHAT_ID": "",
                "TELEGRAM_WEBHOOK_SECRET": "",
            },
            clear=False,
        ), self.assertRaises(RuntimeError):
            webhook_app._load_env()

    def test_webhook_registration_preserves_pending_updates(self) -> None:
        with patch(
            "webhook_app._get_webhook_info",
            return_value={"ok": True, "result": {}},
        ), patch("requests.post", return_value=self._telegram_response()) as post:
            webhook_app._register_webhook(
                "test-token", "https://example.test/telegram/webhook", "test-secret"
            )
        registration = post.call_args_list[0].kwargs["json"]
        self.assertFalse(registration["drop_pending_updates"])
        self.assertEqual(registration["secret_token"], "test-secret")

    def test_calendar_configuration_errors_are_explicit(self) -> None:
        from calendar_sync import insert_calendar_event

        with patch.dict(
            os.environ,
            {
                "GOOGLE_CALENDAR_ID": "",
                "GOOGLE_SERVICE_ACCOUNT_JSON": "",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
            },
            clear=False,
        ), self.assertRaises(CalendarConfigurationError):
            insert_calendar_event("x", dt.date(2026, 9, 15), None, None, None, None)

        with patch.dict(
            os.environ,
            {
                "GOOGLE_CALENDAR_ID": "primary",
                "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
            },
            clear=False,
        ), self.assertRaises(CalendarConfigurationError):
            insert_calendar_event("x", dt.date(2026, 9, 15), None, None, None, None)

    def test_calendar_insert_uses_stable_id_and_recovers_409(self) -> None:
        class Request:
            def __init__(self, result: dict):
                self.result = result

            def execute(self):
                return self.result

        class Events:
            def __init__(self):
                self.saved: dict[str, dict] = {}

            def insert(self, **kwargs):
                body = kwargs["body"]
                event_id = body["id"]
                if event_id in self.saved:
                    raise HttpError(Response({"status": "409"}), b"duplicate")
                self.saved[event_id] = body
                return Request({"id": event_id})

            def get(self, **kwargs):
                return Request(self.saved[kwargs["eventId"]])

        class Service:
            def __init__(self):
                self._events = Events()

            def events(self):
                return self._events

        service = Service()
        with (
            patch.dict(
                os.environ,
                {
                    "GOOGLE_CALENDAR_ID": "calendar-id",
                    "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
                    "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                    "APP_TIMEZONE": "Asia/Ho_Chi_Minh",
                },
                clear=False,
            ),
            patch.object(calendar_sync, "_build_calendar_service", return_value=(service, "svc@example.com")),
        ):
            first = calendar_sync.insert_calendar_event(
                "x", dt.date(2026, 9, 15), "14:00:00", None, None, None, appointment_id="same-id"
            )
            second = calendar_sync.insert_calendar_event(
                "x", dt.date(2026, 9, 15), "14:00:00", None, None, None, appointment_id="same-id"
            )
        self.assertEqual(first, second)
        self.assertEqual(len(service._events.saved), 1)
        body = next(iter(service._events.saved.values()))
        self.assertEqual(body["extendedProperties"]["private"]["source_type"], "appointment")
        self.assertEqual(body["extendedProperties"]["private"]["source_key"], "appointment:same-id")

    def test_calendar_empty_response_is_not_success(self) -> None:
        class Request:
            def execute(self):
                return {}

        class Events:
            def insert(self, **_kwargs):
                return Request()

        class Service:
            def events(self):
                return Events()

        with (
            patch.dict(
                os.environ,
                {
                    "GOOGLE_CALENDAR_ID": "calendar-id",
                    "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
                    "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                },
                clear=False,
            ),
            patch.object(calendar_sync, "_build_calendar_service", return_value=(Service(), "svc@example.com")),
            self.assertRaises(CalendarPersistenceError),
        ):
            calendar_sync.insert_calendar_event("x", dt.date(2026, 9, 15), None, None, None, None)

    def test_unauthorized_chat_never_calls_gemini(self) -> None:
        message = self._message()
        message["chat"] = {"id": 999, "type": "private"}
        message["from"] = {"id": 999}
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.parse_events_with_gemini"
        ) as parse, patch("requests.post", return_value=self._telegram_response()):
            result = webhook_app.telegram_webhook(
                {"update_id": 10, "message": message}, "test-secret"
            )
        self.assertEqual(result, {"ok": True})
        parse.assert_not_called()

    def test_unknown_slash_command_never_calls_gemini(self) -> None:
        message = self._message("/unknown", message_id=11)
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.parse_events_with_gemini"
        ) as parse, patch("requests.post", return_value=self._telegram_response()):
            result = webhook_app.telegram_webhook(
                {"update_id": 11, "message": message}, "test-secret"
            )
        self.assertEqual(result, {"ok": True})
        parse.assert_not_called()

    def test_oversized_message_is_rejected_before_gemini(self) -> None:
        message = self._message("x" * (SMART_PASTE_MAX_INPUT_CHARS + 1), message_id=111)
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.parse_events_with_gemini"
        ) as parse, patch("requests.post", return_value=self._telegram_response()):
            result = webhook_app.telegram_webhook(
                {"update_id": 111, "message": message}, "test-secret"
            )
        self.assertEqual(result, {"ok": True})
        parse.assert_not_called()

    def test_duplicate_update_id_does_not_parse_twice(self) -> None:
        parsed = {"events": [_event_payload()]}
        update = {"update_id": 12, "message": self._message(message_id=12)}
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.parse_events_with_gemini", return_value=parsed
        ) as parse, patch("requests.post", return_value=self._telegram_response()):
            webhook_app.telegram_webhook(update, "test-secret")
            webhook_app.telegram_webhook(update, "test-secret")
        parse.assert_called_once()

    def test_old_dynamic_callback_cannot_target_new_batch(self) -> None:
        parsed = {"events": [_event_payload()]}
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.parse_events_with_gemini", return_value=parsed
        ), patch("requests.post", return_value=self._telegram_response("m1")):
            webhook_app.telegram_webhook(
                {"update_id": 13, "message": self._message("first", 13)}, "test-secret"
            )
        first = webhook_app._SMART_PASTE_STATES.active_batch_for_chat("123")
        assert first is not None
        first_callback = f"{SMART_PASTE_ADD_PREFIX}{first.batch_id}"
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.parse_events_with_gemini", return_value=parsed
        ), patch("requests.post", return_value=self._telegram_response("m2")):
            webhook_app.telegram_webhook(
                {"update_id": 14, "message": self._message("second", 14)}, "test-secret"
            )
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.insert_calendar_event"
        ) as insert, patch("requests.post", return_value=self._telegram_response()):
            webhook_app.telegram_webhook(
                {
                    "update_id": 15,
                    "callback_query": {
                        "id": "old-callback",
                        "from": {"id": 123},
                        "message": {
                            "message_id": "m1",
                            "chat": {"id": 123, "type": "private"},
                        },
                        "data": first_callback,
                    },
                },
                "test-secret",
            )
        insert.assert_not_called()

    def test_telegram_delivery_failure_returns_503_and_keeps_preview(self) -> None:
        parsed = {"events": [_event_payload()]}
        failed_response = MagicMock(ok=False, status_code=500)
        failed_response.json.return_value = {"ok": False}
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.parse_events_with_gemini", return_value=parsed
        ), patch("requests.post", return_value=failed_response), self.assertRaises(Exception) as ctx:
            webhook_app.telegram_webhook(
                {"update_id": 16, "message": self._message(message_id=16)}, "test-secret"
            )
        self.assertEqual(getattr(ctx.exception, "status_code", None), 503)
        self.assertIsNotNone(webhook_app._SMART_PASTE_STATES.active_batch_for_chat("123"))

    def test_completed_callback_retry_resends_result_without_reinserting(self) -> None:
        parsed = {"events": [_event_payload()]}
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.parse_events_with_gemini", return_value=parsed
        ), patch("requests.post", return_value=self._telegram_response("m1")):
            webhook_app.telegram_webhook(
                {"update_id": 17, "message": self._message(message_id=17)}, "test-secret"
            )
        batch = webhook_app._SMART_PASTE_STATES.active_batch_for_chat("123")
        assert batch is not None
        callback = {
            "update_id": 18,
            "callback_query": {
                "id": "complete-1",
                "from": {"id": 123},
                "message": {
                    "message_id": "m1",
                    "chat": {"id": 123, "type": "private"},
                },
                "data": f"{SMART_PASTE_ADD_PREFIX}{batch.batch_id}",
            },
        }
        with patch.dict(os.environ, self.ENV, clear=False), patch(
            "webhook_app.insert_calendar_event", return_value="calendar-id"
        ) as insert, patch("requests.post", return_value=self._telegram_response()):
            webhook_app.telegram_webhook(callback, "test-secret")
            webhook_app.telegram_webhook(
                {**callback, "update_id": 19, "callback_query": {**callback["callback_query"], "id": "complete-2"}},
                "test-secret",
            )
        insert.assert_called_once()

    def test_telegram_non_success_response_is_not_silently_accepted(self) -> None:
        failed_response = MagicMock(ok=False, status_code=500)
        failed_response.json.return_value = {"ok": False}
        with patch("requests.post", return_value=failed_response), self.assertRaises(TelegramDeliveryError):
            _send_message_payload("123456:secret", {"chat_id": "123", "text": "x"})

    def test_telegram_network_error_redacts_token(self) -> None:
        import requests

        with patch(
            "requests.post",
            side_effect=requests.RequestException(
                "https://api.telegram.org/bot123456:secret/sendMessage"
            ),
        ), patch("telegram_mvp_bot.time.sleep"), self.assertRaises(TelegramDeliveryError) as ctx:
            _send_message_payload("123456:secret", {"chat_id": "123", "text": "x"})
        self.assertNotIn("123456:secret", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
