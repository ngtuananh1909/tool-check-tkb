import datetime as dt
import unittest

from course_aliases import shorten_course_name
from telegram_mvp_bot import (
    ADD_FORM_SKIP_WHERE_CALLBACK,
    _advance_add_form_state,
    _build_add_appointment_from_form,
    _build_add_form_input_markup,
    _build_add_form_step_keyboard,
    _build_deadline_detail_text,
    _build_deadline_keyboard,
    _build_deadline_list_text,
    _is_add_form_complete,
    _new_add_form_state,
    _skip_add_form_optional_step,
    _deadline_callback_key,
    _format_deadline_due,
    _parse_add_fields,
    _parse_schedule_day_arg,
)


class BotHelperTests(unittest.TestCase):
    def test_course_name_manual_alias_overrides_auto_shortening(self) -> None:
        aliases = {"Lập trình hướng đối tượng": "OOP"}

        self.assertEqual(shorten_course_name("Lập trình hướng đối tượng", aliases), "OOP")

    def test_course_name_auto_removes_leading_code(self) -> None:
        self.assertEqual(
            shorten_course_name("503071 - Lập trình Web nâng cao - Nhóm 01"),
            "Lập trình Web nâng cao",
        )

    def test_course_name_auto_removes_moodle_prefix_but_keeps_group_suffix(self) -> None:
        self.assertEqual(
            shorten_course_name("HK2_2025_501032_Đại số tuyến tính cho Công nghệ thông tin_N02"),
            "Đại số tuyến tính cho Công nghệ thông tin_N02",
        )

    def test_format_deadline_due_uses_vietnam_local_time(self) -> None:
        self.assertEqual(_format_deadline_due("2026-05-20T00:00:00+07:00"), "20/05/2026 00:00")
        self.assertEqual(_format_deadline_due("2026-05-19T17:00:00+00:00"), "20/05/2026 00:00")

    def test_deadline_text_includes_progress_when_present(self) -> None:
        rows = [
            {
                "course_id": "501032",
                "course_name": "HK2_2025_501032_Đại số tuyến tính cho Công nghệ thông tin_N02",
                "activity_name": "Bài tập cuối kỳ",
                "due_date": "2026-05-20T00:00:00+07:00",
                "progress_percent": 75,
                "lessons_completed": 15,
                "lessons_total": 20,
            }
        ]

        list_text = _build_deadline_list_text(rows)
        detail_text = _build_deadline_detail_text(rows[0])

        self.assertIn("Đại số tuyến tính cho Công nghệ thông tin_N02", list_text)
        self.assertIn("75%", list_text)
        self.assertIn("Tiến độ: 75% (15/20 bài)", detail_text)

    def test_deadline_callback_key_is_unique_per_deadline_activity(self) -> None:
        row_a = {
            "course_id": "501032",
            "activity_name": "Bài tập 1",
            "activity_url": "https://example.test/mod/assign/view.php?id=11",
            "due_date": "2026-05-20T00:00:00+07:00",
        }
        row_b = {
            "course_id": "501032",
            "activity_name": "Bài tập 2",
            "activity_url": "https://example.test/mod/quiz/view.php?id=22",
            "due_date": "2026-05-21T00:00:00+07:00",
        }

        self.assertNotEqual(_deadline_callback_key(row_a), _deadline_callback_key(row_b))

    def test_deadline_keyboard_uses_distinct_callback_data_for_same_course(self) -> None:
        rows = [
            {
                "course_id": "501032",
                "course_name": "HK2_2025_501032_Đại số tuyến tính cho Công nghệ thông tin_N02",
                "activity_name": "Bài tập 1",
                "activity_url": "https://example.test/mod/assign/view.php?id=11",
                "due_date": "2026-05-20T00:00:00+07:00",
            },
            {
                "course_id": "501032",
                "course_name": "HK2_2025_501032_Đại số tuyến tính cho Công nghệ thông tin_N02",
                "activity_name": "Bài tập 2",
                "activity_url": "https://example.test/mod/quiz/view.php?id=22",
                "due_date": "2026-05-21T00:00:00+07:00",
            },
        ]

        keyboard = _build_deadline_keyboard(rows)
        callbacks = [btn[0]["callback_data"] for btn in keyboard["inline_keyboard"]]

        self.assertEqual(len(callbacks), 2)
        self.assertEqual(len(set(callbacks)), 2)

    def test_parse_schedule_day_arg_supports_vietnamese_relative_and_weekday(self) -> None:
        today = dt.date(2026, 5, 13)

        self.assertEqual(_parse_schedule_day_arg("mai", today=today), dt.date(2026, 5, 14))
        self.assertEqual(_parse_schedule_day_arg("thứ 2", today=today), dt.date(2026, 5, 18))
        self.assertEqual(_parse_schedule_day_arg("20/05", today=today), dt.date(2026, 5, 20))

    def test_parse_add_fields_accepts_missing_values_but_rejects_all_blank(self) -> None:
        parsed = _parse_add_fields("Ngày: 16/5\nGiờ: 9:00\nLàm gì: Họp nhóm\nỞ đâu: B402")

        self.assertEqual(parsed, {"date": "16/5", "time": "9:00", "job": "Họp nhóm", "where": "B402"})
        with self.assertRaises(ValueError):
            _parse_add_fields("Ngày: \nGiờ: 9:00\nLàm gì: Họp nhóm\nỞ đâu: ")

    def test_add_form_state_collects_values_step_by_step_and_builds_payload(self) -> None:
        state = _new_add_form_state()
        self.assertEqual(state["step"], "date")

        self.assertIn("16/5", _build_add_form_input_markup(state)["input_field_placeholder"])
        _advance_add_form_state(state, "16/5")
        self.assertEqual(state["step"], "time")

        _advance_add_form_state(state, "9:00")
        self.assertEqual(state["step"], "job")

        review_prompt = _advance_add_form_state(state, "Họp nhóm")
        self.assertEqual(state["step"], "where")
        self.assertIn("địa điểm", review_prompt.lower())
        step_keyboard = _build_add_form_step_keyboard(state)
        self.assertEqual(step_keyboard["inline_keyboard"][0][0]["callback_data"], ADD_FORM_SKIP_WHERE_CALLBACK)

        review = _skip_add_form_optional_step(state)
        self.assertTrue(_is_add_form_complete(state))
        self.assertEqual(state["date"], "16/5")
        self.assertEqual(state["time"], "9:00")
        self.assertIsNone(state["where"])
        self.assertIn("Done", review)
        title, appointment_date, start_time, location = _build_add_appointment_from_form(state)

        self.assertEqual(title, "Họp nhóm")
        self.assertEqual(appointment_date, dt.date(dt.date.today().year, 5, 16))
        self.assertEqual(start_time, "09:00:00")
        self.assertIsNone(location)


if __name__ == "__main__":
    unittest.main()
