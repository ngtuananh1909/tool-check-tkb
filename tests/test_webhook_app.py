import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import webhook_app


class WebhookAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_CHAT_ID": "",
                "TELEGRAM_WEBHOOK_URL": "",
                "TELEGRAM_WEBHOOK_SECRET": "secret-token",
                "GEMINI_API_KEY": "",
            },
            clear=False,
        )
        self.env.start()
        self.client = TestClient(webhook_app.app)

    def tearDown(self) -> None:
        self.env.stop()

    def test_health_endpoint_returns_ok(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_invalid_webhook_secret_is_rejected(self) -> None:
        response = self.client.post(
            "/telegram/webhook",
            json={"message": {"text": "/start", "chat": {"id": 123}}},
            headers={"x-telegram-bot-api-secret-token": "wrong"},
        )

        self.assertEqual(response.status_code, 401)

    def test_deadline_command_sends_keyboard_when_rows_exist(self) -> None:
        rows = [
            {
                "course_id": "47728",
                "course_name": "Operating Systems",
                "activity_name": "Final Report",
                "due_date": "2026-05-20T23:59:00+07:00",
                "activity_url": "https://example.test/mod/assign/view.php?id=1",
                "completion_status": "incomplete",
            }
        ]

        with patch.object(webhook_app, "get_nearest_elearning_deadlines", return_value=rows), patch.object(
            webhook_app, "_send_text_with_keyboard"
        ) as send_keyboard:
            response = self.client.post(
                "/telegram/webhook",
                json={"message": {"text": "/deadline", "chat": {"id": 123}}},
                headers={"x-telegram-bot-api-secret-token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        send_keyboard.assert_called_once()
        self.assertIn("Operating Systems", send_keyboard.call_args.args[2])
        self.assertTrue(send_keyboard.call_args.args[3]["inline_keyboard"])

    def test_webhook_info_endpoint_returns_sanitized_status(self) -> None:
        with patch.object(
            webhook_app,
            "_get_webhook_info",
            return_value={"ok": True, "result": {"url": "https://example.test/telegram/webhook", "pending_update_count": 0}},
        ):
            response = self.client.get("/telegram/webhook/info")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"ok": True, "url": "https://example.test/telegram/webhook", "pending_update_count": 0, "last_error_message": None},
        )

    def test_gemini_unavailable_chat_uses_fallback_reply(self) -> None:
        with patch.object(webhook_app, "parse_appointment_with_gemini", return_value=None), patch.object(
            webhook_app, "_send_text"
        ) as send_text:
            response = self.client.post(
                "/telegram/webhook",
                json={"message": {"text": "chào bạn", "chat": {"id": 123}}},
                headers={"x-telegram-bot-api-secret-token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        send_text.assert_called_once()
        self.assertIsInstance(send_text.call_args.args[2], str)
        self.assertTrue(send_text.call_args.args[2].strip())

    def test_gemini_health_reports_missing_key_without_calling_api(self) -> None:
        response = self.client.get("/gemini/health")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["api_key_set"])


if __name__ == "__main__":
    unittest.main()
