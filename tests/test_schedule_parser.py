"""
Unit tests for BeautifulSoup HTML schedule parser.
"""

from pathlib import Path
import unittest

from tdtu.schedule.parser import (
    parse_active_semester,
    parse_general_schedule_table,
    parse_schedule_html,
    parse_semester_options,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tdtu"


class TestScheduleParser(unittest.TestCase):

    def setUp(self) -> None:
        fixture_path = FIXTURES_DIR / "schedule_general.html"
        with open(fixture_path, "r", encoding="utf-8") as f:
            self.html = f.read()

    def test_parse_active_semester(self) -> None:
        sem = parse_active_semester(self.html)
        self.assertEqual(sem, "HK1/2026-2027")

    def test_parse_semester_options(self) -> None:
        opts = parse_semester_options(self.html)
        self.assertEqual(len(opts), 3)
        self.assertEqual(opts[1]["value"], "136")
        self.assertTrue(opts[1]["selected"])

    def test_parse_general_schedule(self) -> None:
        entries = parse_general_schedule_table(self.html, student_id="52500028")
        self.assertGreater(len(entries), 0)

        # Check first entry
        item = entries[0]
        self.assertEqual(item["student_id"], "52500028")
        self.assertIn("Kinh tế chính trị", item["subject_name"])
        self.assertEqual(item["room"], "B204")
        self.assertEqual(item["day_of_week"], "Tuesday")
        self.assertEqual(item["start_period"], 1)
        self.assertEqual(item["end_period"], 3)
        self.assertEqual(item["status"], "Học")

    def test_parse_schedule_html_convenience(self) -> None:
        entries = parse_schedule_html(self.html, student_id="52500028")
        self.assertGreater(len(entries), 0)


if __name__ == "__main__":
    unittest.main()
