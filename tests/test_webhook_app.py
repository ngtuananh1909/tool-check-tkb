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
        webhook_app._ADD_FORM_STATES.clear()
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

    def test_non_command_message_guides_user_to_add_form(self) -> None:
        with patch.object(webhook_app, "_send_text") as send_text, patch.object(
            webhook_app, "create_appointment"
        ) as create_appointment:
            response = self.client.post(
                "/telegram/webhook",
                json={"message": {"text": "chào bạn", "chat": {"id": 123}}},
                headers={"x-telegram-bot-api-secret-token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        send_text.assert_called_once()
        self.assertEqual(send_text.call_args.args[2], webhook_app.ADD_ONLY_GUIDANCE_TEXT)
        create_appointment.assert_not_called()

    def test_start_command_mentions_add_form(self) -> None:
        with patch.object(webhook_app, "_send_text") as send_text:
            response = self.client.post(
                "/telegram/webhook",
                json={"message": {"text": "/start", "chat": {"id": 123}}},
                headers={"x-telegram-bot-api-secret-token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        send_text.assert_called_once()
        self.assertIn("/add", send_text.call_args.args[2])
        self.assertIn("Mở form thêm lịch", send_text.call_args.args[2])
        self.assertIn("Ngày, Giờ, Làm gì, Ở đâu", send_text.call_args.args[2])
        self.assertIn("hỏi lần lượt", send_text.call_args.args[2])

    def test_free_text_appointment_does_not_create_appointment(self) -> None:
        with patch.object(webhook_app, "_send_text") as send_text, patch.object(
            webhook_app, "create_appointment"
        ) as create_appointment:
            response = self.client.post(
                "/telegram/webhook",
                json={"message": {"text": "Họp nhóm-2026-05-20 09:00-B402", "chat": {"id": 123}}},
                headers={"x-telegram-bot-api-secret-token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        send_text.assert_called_once()
        self.assertEqual(send_text.call_args.args[2], webhook_app.ADD_ONLY_GUIDANCE_TEXT)
        create_appointment.assert_not_called()

    def test_gemini_health_reports_missing_key_without_calling_api(self) -> None:
        response = self.client.get("/gemini/health")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["api_key_set"])

    def test_register_command_menu_sets_telegram_commands(self) -> None:
        with patch.object(webhook_app, "_telegram_post", return_value={"ok": True}) as telegram_post:
            webhook_app._register_command_menu("test-token")

        telegram_post.assert_called_once()
        self.assertEqual(telegram_post.call_args.args[1], "setMyCommands")
        commands = telegram_post.call_args.args[2]["commands"]
        self.assertEqual([command["command"] for command in commands], ["start", "today", "schedule", "deadline", "add"])

    def test_add_form_flow_with_done_text_creates_appointment(self) -> None:
        with patch.object(webhook_app, "create_appointment") as create_appointment, patch.object(
            webhook_app, "_send_text"
        ) as send_text, patch.object(webhook_app, "_send_text_with_keyboard") as send_text_with_keyboard:
            with patch.object(webhook_app, "_send_add_form_step") as send_add_form_step:
                for text in ("/add", "16/5", "9:00", "Họp nhóm", "B402", "/done"):
                    response = self.client.post(
                        "/telegram/webhook",
                        json={"message": {"text": text, "chat": {"id": 123}}},
                        headers={"x-telegram-bot-api-secret-token": "secret-token"},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"ok": True})

        create_appointment.assert_called_once()
        self.assertEqual(create_appointment.call_args.kwargs["title"], "Họp nhóm")
        self.assertEqual(create_appointment.call_args.kwargs["start_time"], "09:00:00")
        self.assertEqual(create_appointment.call_args.kwargs["location"], "B402")
        self.assertTrue(str(create_appointment.call_args.kwargs["appointment_date"]).endswith("-05-16"))
        self.assertEqual(send_add_form_step.call_count, 4)
        send_text_with_keyboard.assert_called_once()
        self.assertTrue(send_text.called)

    def test_add_form_skip_where_then_callback_done_creates_appointment(self) -> None:
        with patch.object(webhook_app, "create_appointment") as create_appointment, patch.object(
            webhook_app, "_send_text"
        ) as send_text, patch.object(webhook_app, "_send_text_with_keyboard") as send_text_with_keyboard, patch.object(
            webhook_app.requests, "post"
        ), patch.object(webhook_app, "_send_add_form_step"):
            for text in ("/add", "16/5", "9:00", "Họp nhóm"):
                response = self.client.post(
                    "/telegram/webhook",
                    json={"message": {"text": text, "chat": {"id": 123}}},
                    headers={"x-telegram-bot-api-secret-token": "secret-token"},
                )
                self.assertEqual(response.status_code, 200)

            skip_response = self.client.post(
                "/telegram/webhook",
                json={
                    "callback_query": {
                        "id": "cbq-skip",
                        "data": webhook_app.ADD_FORM_SKIP_WHERE_CALLBACK,
                        "message": {"chat": {"id": 123}},
                    }
                },
                headers={"x-telegram-bot-api-secret-token": "secret-token"},
            )
            callback_response = self.client.post(
                "/telegram/webhook",
                json={
                    "callback_query": {
                        "id": "cbq-done",
                        "data": webhook_app.ADD_FORM_DONE_CALLBACK,
                        "message": {"chat": {"id": 123}},
                    }
                },
                headers={"x-telegram-bot-api-secret-token": "secret-token"},
            )

        self.assertEqual(skip_response.status_code, 200)
        self.assertEqual(callback_response.status_code, 200)
        self.assertEqual(callback_response.json(), {"ok": True})
        create_appointment.assert_called_once()
        self.assertEqual(create_appointment.call_args.kwargs["title"], "Họp nhóm")
        self.assertIsNone(create_appointment.call_args.kwargs["location"])
        self.assertTrue(send_text_with_keyboard.called)
        self.assertTrue(send_text.called)

    def test_add_form_invalid_step_returns_prompt_with_error(self) -> None:
        with patch.object(webhook_app, "_send_add_form_step") as send_add_form_step, patch.object(
            webhook_app, "create_appointment"
        ) as create_appointment:
            for text in ("/add", "abc"):
                response = self.client.post(
                    "/telegram/webhook",
                    json={"message": {"text": text, "chat": {"id": 123}}},
                    headers={"x-telegram-bot-api-secret-token": "secret-token"},
                )
                self.assertEqual(response.status_code, 200)

        self.assertEqual(send_add_form_step.call_count, 2)
        self.assertEqual(send_add_form_step.call_args.kwargs["prefix"], "Không đọc được ngày. Dùng YYYY-MM-DD hoặc DD/MM hoặc DD/MM/YYYY.")
        create_appointment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
