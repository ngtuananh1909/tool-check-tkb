import unittest

from notifier import _build_combined_message, _compact_course_name


class NotifierFormattingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
