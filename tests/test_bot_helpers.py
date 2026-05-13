import datetime as dt
import unittest

from course_aliases import shorten_course_name
from telegram_mvp_bot import _parse_add_fields, _parse_schedule_day_arg


class BotHelperTests(unittest.TestCase):
    def test_course_name_manual_alias_overrides_auto_shortening(self) -> None:
        aliases = {"Lập trình hướng đối tượng": "OOP"}

        self.assertEqual(shorten_course_name("Lập trình hướng đối tượng", aliases), "OOP")

    def test_course_name_auto_removes_leading_code(self) -> None:
        self.assertEqual(
            shorten_course_name("503071 - Lập trình Web nâng cao - Nhóm 01"),
            "Lập trình Web nâng cao",
        )

    def test_parse_schedule_day_arg_supports_vietnamese_relative_and_weekday(self) -> None:
        today = dt.date(2026, 5, 13)

        self.assertEqual(_parse_schedule_day_arg("mai", today=today), dt.date(2026, 5, 14))
        self.assertEqual(_parse_schedule_day_arg("thứ 2", today=today), dt.date(2026, 5, 18))
        self.assertEqual(_parse_schedule_day_arg("20/05", today=today), dt.date(2026, 5, 20))

    def test_parse_add_fields_accepts_missing_values_but_rejects_all_blank(self) -> None:
        parsed = _parse_add_fields("Thời gian: 20/05 09:00\nJob: \nWhere: B402")

        self.assertEqual(parsed, {"time": "20/05 09:00", "job": None, "where": "B402"})
        with self.assertRaises(ValueError):
            _parse_add_fields("Thời gian: \nJob: \nWhere: ")


if __name__ == "__main__":
    unittest.main()
