"""
Unit tests for BeautifulSoup HTML exam schedule parser.
"""

from pathlib import Path
import unittest

from tdtu.exams.parser import parse_exam_html

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tdtu"


class TestExamParser(unittest.TestCase):

    def setUp(self) -> None:
        fixture_path = FIXTURES_DIR / "exams_final.html"
        with open(fixture_path, "r", encoding="utf-8") as f:
            self.html = f.read()

    def test_parse_exam_tables(self) -> None:
        exams = parse_exam_html(self.html, default_exam_type="Cuối kỳ")
        self.assertEqual(len(exams), 2)

        ex1 = exams[0]
        self.assertEqual(ex1["subject_name"], "Hệ cơ sở dữ liệu")
        self.assertEqual(ex1["exam_date"], "2026-12-15")
        self.assertEqual(ex1["start_time"], "07:30")
        self.assertEqual(ex1["end_time"], "09:00")
        self.assertEqual(ex1["exam_room"], "C311")
        self.assertEqual(ex1["exam_type"], "Tự luận")

        ex2 = exams[1]
        self.assertEqual(ex2["subject_name"], "Cấu trúc dữ liệu và giải thuật")
        self.assertEqual(ex2["exam_date"], "2026-12-18")
        self.assertEqual(ex2["start_time"], "13:30")
        self.assertEqual(ex2["end_time"], "15:00")
        self.assertEqual(ex2["exam_room"], "B311")
        self.assertEqual(ex2["exam_type"], "Trắc nghiệm")


if __name__ == "__main__":
    unittest.main()
