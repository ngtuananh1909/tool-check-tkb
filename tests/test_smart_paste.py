import datetime as dt
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from gemini_parser import parse_events_with_gemini
from telegram_mvp_bot import (
    SMART_PASTE_ADD_ALL_CALLBACK,
    SMART_PASTE_CANCEL_CALLBACK,
    _build_smart_paste_keyboard,
    _build_smart_paste_preview_text,
    _normalize_smart_paste_event,
    _normalize_time_value,
)
from webhook_app import (
    _ADD_FORM_STATES,
    _SMART_PASTE_STATES,
    HTTPException,
    telegram_webhook,
)


def _telegram_response(message_id: str = "m1") -> MagicMock:
    response = MagicMock(ok=True, status_code=200)
    response.json.return_value = {"ok": True, "result": {"message_id": message_id}}
    return response


class _DirectResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _DirectWebhookClient:
    """Small handler adapter used when the local Starlette TestClient is incompatible."""

    def post(self, _path: str, *, headers: dict[str, str], json: dict) -> _DirectResponse:
        try:
            payload = telegram_webhook(
                json,
                headers.get("X-Telegram-Bot-Api-Secret-Token"),
            )
            return _DirectResponse(200, payload)
        except HTTPException as exc:
            return _DirectResponse(exc.status_code, {"detail": exc.detail})


class GeminiParserTests(unittest.TestCase):
    def test_missing_gemini_key(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            result = parse_events_with_gemini("Hop luc 14h")
            self.assertIsNone(result)

    def test_single_event_parsed(self) -> None:
        payload = {
            "events": [
                {
                    "title": "Họp nhóm CNPM",
                    "appointment_date": "2026-09-15",
                    "start_time": "14:00:00",
                    "end_time": None,
                    "location": "B402",
                    "note": None,
                    "confidence": 0.95,
                    "needs_clarification": False,
                    "clarification_question": None,
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.text = json.dumps(payload)

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}),
            patch("google.genai.Client", return_value=mock_client),
        ):
            res = parse_events_with_gemini("Tuần sau thứ 3 họp nhóm CNPM lúc 14h tại B402")

        self.assertIsNotNone(res)
        self.assertEqual(len(res["events"]), 1)
        self.assertEqual(res["events"][0]["title"], "Họp nhóm CNPM")
        self.assertEqual(res["events"][0]["location"], "B402")

    def test_multi_event_parsed(self) -> None:
        payload = {
            "events": [
                {
                    "title": "Họp nhóm",
                    "appointment_date": "2026-09-15",
                    "start_time": "14:00:00",
                    "end_time": None,
                    "location": "B402",
                    "note": None,
                    "confidence": 0.95,
                    "needs_clarification": False,
                    "clarification_question": None,
                },
                {
                    "title": "Gặp thầy review project",
                    "appointment_date": "2026-09-17",
                    "start_time": "09:00:00",
                    "end_time": None,
                    "location": "C105",
                    "note": "Review project",
                    "confidence": 0.92,
                    "needs_clarification": False,
                    "clarification_question": None,
                },
            ]
        }
        mock_response = MagicMock()
        mock_response.text = json.dumps(payload)

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}),
            patch("google.genai.Client", return_value=mock_client),
        ):
            res = parse_events_with_gemini("Text with 2 events")

        self.assertIsNotNone(res)
        self.assertEqual(len(res["events"]), 2)
        self.assertEqual(res["events"][0]["title"], "Họp nhóm")
        self.assertEqual(res["events"][1]["title"], "Gặp thầy review project")

    def test_location_null(self) -> None:
        payload = {
            "events": [
                {
                    "title": "Làm bài tập",
                    "appointment_date": "2026-09-15",
                    "start_time": "20:00:00",
                    "end_time": None,
                    "location": None,
                    "note": None,
                    "confidence": 0.9,
                    "needs_clarification": False,
                    "clarification_question": None,
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.text = json.dumps(payload)

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}),
            patch("google.genai.Client", return_value=mock_client),
        ):
            res = parse_events_with_gemini("20h làm bài tập")

        self.assertIsNotNone(res)
        self.assertIsNone(res["events"][0]["location"])

    def test_invalid_json(self) -> None:
        mock_response = MagicMock()
        mock_response.text = "This is not json at all."

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}),
            patch("google.genai.Client", return_value=mock_client),
        ):
            res = parse_events_with_gemini("some text")

        self.assertIsNone(res)

    def test_ambiguous_event(self) -> None:
        payload = {
            "events": [
                {
                    "title": "Đi chơi",
                    "appointment_date": "2026-09-15",
                    "start_time": None,
                    "end_time": None,
                    "location": None,
                    "note": None,
                    "confidence": 0.4,
                    "needs_clarification": True,
                    "clarification_question": "Bạn muốn đi lúc mấy giờ?",
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.text = json.dumps(payload)

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}),
            patch("google.genai.Client", return_value=mock_client),
        ):
            res = parse_events_with_gemini("hôm nào đi chơi nhé")

        self.assertIsNotNone(res)
        self.assertTrue(res["events"][0]["needs_clarification"])


class SmartPasteHelperTests(unittest.TestCase):
    def test_normalize_time_value(self) -> None:
        self.assertEqual(_normalize_time_value("14:00"), "14:00:00")
        self.assertEqual(_normalize_time_value("14:00:00"), "14:00:00")
        self.assertEqual(_normalize_time_value("9:30"), "09:30:00")
        with self.assertRaises(ValueError):
            _normalize_time_value("25:00")
        self.assertIsNone(_normalize_time_value(None))
        self.assertIsNone(_normalize_time_value("null"))

    def test_normalize_smart_paste_event(self) -> None:
        raw = {
            "title": "Họp nhóm",
            "appointment_date": "2026-09-15",
            "start_time": "14:00",
            "end_time": "16:00",
            "location": "B402",
            "note": "Mang laptop",
        }
        ev = _normalize_smart_paste_event(raw)
        self.assertEqual(ev["title"], "Họp nhóm")
        self.assertEqual(ev["appointment_date"], dt.date(2026, 9, 15))
        self.assertEqual(ev["start_time"], "14:00:00")
        self.assertEqual(ev["end_time"], "16:00:00")
        self.assertEqual(ev["location"], "B402")
        self.assertEqual(ev["note"], "Mang laptop")

    def test_build_smart_paste_preview_text(self) -> None:
        events = [
            {
                "title": "Họp nhóm",
                "appointment_date": dt.date(2026, 9, 15),
                "start_time": "14:00:00",
                "end_time": None,
                "location": "B402",
                "note": None,
            },
            {
                "title": "Review project",
                "appointment_date": dt.date(2026, 9, 17),
                "start_time": "09:00:00",
                "end_time": "10:30:00",
                "location": None,
                "note": None,
            },
        ]
        text = _build_smart_paste_preview_text(events)
        self.assertIn("1️⃣ Họp nhóm", text)
        self.assertIn("📅 15/09/2026", text)
        self.assertIn("⏰ 14:00", text)
        self.assertIn("📍 B402", text)
        self.assertIn("2️⃣ Review project", text)
        self.assertIn("⏰ 09:00 - 10:30", text)
        self.assertIn("Kiểm tra lại trước khi lưu nhé.", text)

    def test_build_smart_paste_keyboard(self) -> None:
        kb = _build_smart_paste_keyboard()
        buttons = kb["inline_keyboard"][0]
        self.assertEqual(buttons[0]["callback_data"], SMART_PASTE_ADD_ALL_CALLBACK)
        self.assertEqual(buttons[1]["callback_data"], SMART_PASTE_CANCEL_CALLBACK)


class WebhookSmartPasteTests(unittest.TestCase):
    def setUp(self) -> None:
        _SMART_PASTE_STATES.clear()
        _ADD_FORM_STATES.clear()
        self.client = _DirectWebhookClient()
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}

    def test_plain_text_triggers_smart_paste_preview(self) -> None:
        parsed = {
            "events": [
                {
                    "title": "Họp nhóm CNPM",
                    "appointment_date": "2026-09-15",
                    "start_time": "14:00:00",
                    "end_time": None,
                    "location": "B402",
                    "note": None,
                    "confidence": 0.95,
                    "needs_clarification": False,
                    "clarification_question": None,
                }
            ]
        }
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123", "TELEGRAM_WEBHOOK_SECRET": "test-secret"}),
            patch("webhook_app.parse_events_with_gemini", return_value=parsed),
            patch("requests.post", return_value=_telegram_response()) as mock_post,
        ):
            resp = self.client.post(
                "/telegram/webhook",
                headers=self.headers,
                json={"update_id": 1, "message": {"message_id": 1, "chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "Mai 14h họp nhóm ở B402"}},
            )
            self.assertEqual(resp.status_code, 200)

            # Check state was stored
            self.assertIn("123", _SMART_PASTE_STATES)
            self.assertEqual(len(_SMART_PASTE_STATES["123"]["events"]), 1)

            # Check message sent to Telegram has inline keyboard
            self.assertTrue(mock_post.called)
            payload = mock_post.call_args[1]["json"]
            self.assertIn("Họp nhóm CNPM", payload["text"])
            self.assertIn("reply_markup", payload)

    def test_callback_smartpaste_add_all_inserts_events_and_clears_state(self) -> None:
        _SMART_PASTE_STATES["123"] = {
            "events": [
                {
                    "title": "Họp nhóm 1",
                    "appointment_date": dt.date(2026, 9, 15),
                    "start_time": "14:00:00",
                    "end_time": None,
                    "location": "B402",
                    "note": None,
                },
                {
                    "title": "Họp nhóm 2",
                    "appointment_date": dt.date(2026, 9, 16),
                    "start_time": "15:00:00",
                    "end_time": None,
                    "location": "B403",
                    "note": None,
                },
            ],
            "original_text": "...",
        }
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123", "TELEGRAM_WEBHOOK_SECRET": "test-secret"}),
            patch("webhook_app.insert_calendar_event") as mock_insert,
            patch("requests.post", return_value=_telegram_response()) as mock_post,
        ):
            resp = self.client.post(
                "/telegram/webhook",
                headers=self.headers,
                json={
                    "callback_query": {
                        "id": "cb1",
                        "message": {"message_id": "legacy", "chat": {"id": 123, "type": "private"}},
                        "from": {"id": 123},
                        "data": SMART_PASTE_ADD_ALL_CALLBACK,
                    }
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(mock_insert.call_count, 2)
            self.assertNotIn("123", _SMART_PASTE_STATES)

            # Check confirmation text sent
            last_call_payload = mock_post.call_args[1]["json"]
            self.assertIn("Đã thêm 2 lịch", last_call_payload["text"])

    def test_callback_smartpaste_cancel_clears_state_without_inserting(self) -> None:
        _SMART_PASTE_STATES["123"] = {
            "events": [
                {
                    "title": "Họp nhóm",
                    "appointment_date": dt.date(2026, 9, 15),
                    "start_time": "14:00:00",
                    "end_time": None,
                    "location": "B402",
                    "note": None,
                }
            ],
            "original_text": "...",
        }
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123", "TELEGRAM_WEBHOOK_SECRET": "test-secret"}),
            patch("webhook_app.insert_calendar_event") as mock_insert,
            patch("requests.post", return_value=_telegram_response()),
        ):
            resp = self.client.post(
                "/telegram/webhook",
                headers=self.headers,
                json={
                    "callback_query": {
                        "id": "cb1",
                        "message": {"message_id": "legacy", "chat": {"id": 123, "type": "private"}},
                        "from": {"id": 123},
                        "data": SMART_PASTE_CANCEL_CALLBACK,
                    }
                },
            )
            self.assertEqual(resp.status_code, 200)
            mock_insert.assert_not_called()
            self.assertNotIn("123", _SMART_PASTE_STATES)

    def test_ambiguous_event_requests_clarification(self) -> None:
        parsed = {
            "events": [
                {
                    "title": "Họp",
                    "appointment_date": "2026-09-15",
                    "start_time": None,
                    "end_time": None,
                    "location": None,
                    "note": None,
                    "confidence": 0.4,
                    "needs_clarification": True,
                    "clarification_question": "Mấy giờ họp bạn ơi?",
                }
            ]
        }
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123", "TELEGRAM_WEBHOOK_SECRET": "test-secret"}),
            patch("webhook_app.parse_events_with_gemini", return_value=parsed),
            patch("requests.post", return_value=_telegram_response()) as mock_post,
        ):
            resp = self.client.post(
                "/telegram/webhook",
                headers=self.headers,
                json={"update_id": 2, "message": {"message_id": 2, "chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "Họp nhé"}},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("123", _SMART_PASTE_STATES)
            payload = mock_post.call_args[1]["json"]
            self.assertIn("chưa đủ chắc", payload["text"])

    def test_gemini_unavailable_falls_back_gracefully(self) -> None:
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123", "TELEGRAM_WEBHOOK_SECRET": "test-secret"}),
            patch("webhook_app.parse_events_with_gemini", return_value=None),
            patch("requests.post", return_value=_telegram_response()) as mock_post,
        ):
            resp = self.client.post(
                "/telegram/webhook",
                headers=self.headers,
                json={"update_id": 3, "message": {"message_id": 3, "chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "Mai 14h họp"}},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("123", _SMART_PASTE_STATES)
            payload = mock_post.call_args[1]["json"]
            self.assertIn("chưa đọc tự động được", payload["text"])
            self.assertIn("/add", payload["text"])

    def test_add_form_still_takes_priority_over_smart_paste(self) -> None:
        _ADD_FORM_STATES["123"] = {
            "step": "date",
            "date": None,
            "time": None,
            "job": None,
            "where": None,
        }
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123", "TELEGRAM_WEBHOOK_SECRET": "test-secret"}),
            patch("webhook_app.parse_events_with_gemini") as mock_gemini,
            patch("requests.post", return_value=_telegram_response()),
        ):
            resp = self.client.post(
                "/telegram/webhook",
                headers=self.headers,
                json={"update_id": 4, "message": {"message_id": 4, "chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "15/09/2026"}},
            )
            self.assertEqual(resp.status_code, 200)
            # Gemini parser should not be called because add form is active
            mock_gemini.assert_not_called()
            self.assertEqual(_ADD_FORM_STATES["123"]["step"], "time")


if __name__ == "__main__":
    unittest.main()
