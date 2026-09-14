import unittest

from notifier import (
    _build_combined_message,
    _compact_course_name,
    _redact_telegram_error,
)


class NotifierFormattingTests(unittest.TestCase):
    def test_telegram_request_errors_redact_bot_token(self) -> None:
        for error in (
            "HTTPSConnectionPool(https://api.telegram.org/bot123456:secret/sendMessage)",
            "HTTPSConnectionPool(host='api.telegram.org', url: /bot123456:secret/sendMessage)",
        ):
            with self.subTest(error=error):
                redacted = _redact_telegram_error(error)

                self.assertNotIn("123456:secret", redacted)
                self.assertIn("bot[redacted]/sendMessage", redacted)

    def test_compact_course_name_removes_moodle_prefix(self) -> None:
        self.assertEqual(
            _compact_course_name("HK2_2025_501032_Đại số tuyến tính cho Công nghệ thông tin_N02"),
            "Đại số tuyến tính cho Công nghệ thông tin_N02",
        )

    def test_daily_summary_omits_standalone_elearning_progress_section(self) -> None:
        text = _build_combined_message(
            classes=[],
            appointments=[],
            upcoming_exams=[],
            elearning_progress=[
                {
                    "course_name": "HK2_2025_501032_Đại số tuyến tính cho Công nghệ thông tin_N02",
                    "progress_percent": 75,
                    "lessons_completed": 15,
                    "lessons_total": 20,
                }
            ],
        )

        self.assertNotIn("Tiến độ eLearning theo môn", text)
        self.assertNotIn("75%", text)

    def test_class_session_with_periods_and_times_formats_properly(self) -> None:
        cls = {
            "subject_name": "Web Programming",
            "room": "C204",
            "start_period": 1,
            "end_period": 3,
            "start_time": "06:50",
            "end_time": "09:20",
            "status": "scheduled",
        }
        text = _build_combined_message(
            classes=[cls],
            appointments=[],
            upcoming_exams=[],
            elearning_progress=[],
        )
        self.assertIn("Tiết 1→3 · 06:50–09:20", text)
        self.assertNotIn("0→0", text)
        self.assertIn("Web Programming", text)
        self.assertIn("C204", text)
        self.assertIn("Học bình thường", text)

    def test_class_session_without_periods_falls_back_to_time_range(self) -> None:
        cls = {
            "subject_name": "Web Programming",
            "room": "C204",
            "start_time": "06:50",
            "end_time": "09:20",
            "status": "makeup",
        }
        text = _build_combined_message(
            classes=[cls],
            appointments=[],
            upcoming_exams=[],
            elearning_progress=[],
        )
        self.assertIn("06:50–09:20", text)
        self.assertNotIn("Tiết", text)
        self.assertNotIn("0→0", text)
        self.assertIn("Học bù", text)

    def test_class_session_missing_both_period_and_time(self) -> None:
        cls = {
            "subject_name": "Web Programming",
            "room": "C204",
            "status": "absent",
        }
        text = _build_combined_message(
            classes=[cls],
            appointments=[],
            upcoming_exams=[],
            elearning_progress=[],
        )
        self.assertIn("Chưa rõ thời gian", text)
        self.assertNotIn("0→0", text)
        self.assertIn("Báo vắng", text)


if __name__ == "__main__":
    unittest.main()

