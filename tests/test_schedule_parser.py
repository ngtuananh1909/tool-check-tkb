"""
Unit tests for BeautifulSoup HTML schedule parser.
Checks status detection, Headerrow filtering, and deduplication logic.
"""

from pathlib import Path
import unittest

from tdtu.schedule.parser import (
    _deduplicate_schedule,
    detect_status,
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

    def test_status_detection_keywords(self) -> None:
        self.assertEqual(detect_status("Kinh tế chính trị Mác-Lênin - Báo vắng"), "absent")
        self.assertEqual(detect_status("GV vắng tiết 123"), "absent")
        self.assertEqual(detect_status("Học bù tiết 456"), "makeup")
        self.assertEqual(detect_status("Lịch bù môn CSDL"), "makeup")
        self.assertEqual(detect_status("LHB - Phòng A704"), "makeup")
        self.assertEqual(detect_status("Hủy lớp tuần này"), "cancelled")
        self.assertEqual(detect_status("Dời lịch thi sang tuần sau"), "moved")
        self.assertEqual(detect_status("Kinh tế chính trị Mác-Lênin (306103)"), "scheduled")

    def test_deduplication_preserves_paired_absent_and_makeup_rows(self) -> None:
        rows = [
            {
                "subject_name": "CSDL",
                "room": "C311",
                "day_of_week": "Monday",
                "session_date": "2026-10-05",
                "start_period": 1,
                "end_period": 3,
                "status": "absent",
            },
            {
                "subject_name": "CSDL",
                "room": "C311",
                "day_of_week": "Monday",
                "session_date": "2026-10-05",
                "start_period": 1,
                "end_period": 3,
                "status": "makeup",
            },
        ]
        deduped = _deduplicate_schedule(rows)
        # BOTH rows must survive deduplication because their statuses differ! (Addresses Blocker 2)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0]["status"], "absent")
        self.assertEqual(deduped[1]["status"], "makeup")

    def test_headerrow_not_parsed_as_subjects(self) -> None:
        entries = parse_general_schedule_table(self.html, student_id="TEST_STUDENT_001")
        # Ensure header labels like "Thứ 2 | Monday" are NEVER present as subject names!
        subject_names = [e["subject_name"] for e in entries]
        for name in subject_names:
            self.assertNotIn("Thứ 2", name)
            self.assertNotIn("Monday", name)

    def test_parse_active_semester(self) -> None:
        sem = parse_active_semester(self.html)
        self.assertEqual(sem, "HK1/2026-2027")

    def test_parse_semester_options(self) -> None:
        opts = parse_semester_options(self.html)
        self.assertEqual(len(opts), 3)
        self.assertEqual(opts[1]["value"], "136")
        self.assertTrue(opts[1]["selected"])


    def test_parse_period_range(self) -> None:
        from tdtu.schedule.parser import parse_period_range
        # Single periods 1..16
        self.assertEqual(parse_period_range("Tiết: 1"), (1, 1))
        self.assertEqual(parse_period_range("Tiết: 10"), (10, 10))
        self.assertEqual(parse_period_range("Tiết: 11"), (11, 11))
        self.assertEqual(parse_period_range("Tiết: 12"), (12, 12))
        self.assertEqual(parse_period_range("Tiết: 15"), (15, 15))
        self.assertEqual(parse_period_range("Tiết: 16"), (16, 16))

        # Ranges and concatenated sequences
        self.assertEqual(parse_period_range("Tiết: 123"), (1, 3))
        self.assertEqual(parse_period_range("Tiết: 456"), (4, 6))
        self.assertEqual(parse_period_range("Tiết: 789"), (7, 9))
        self.assertEqual(parse_period_range("Tiết: 8910"), (8, 10))
        self.assertEqual(parse_period_range("Tiết: 10-12"), (10, 12))
        self.assertEqual(parse_period_range("Tiết: 101112"), (10, 12))
        self.assertEqual(parse_period_range("Tiết: 131415"), (13, 15))
        self.assertEqual(parse_period_range("Tiết: 1 - 3"), (1, 3))
        self.assertEqual(parse_period_range("Period: 10 to 12"), (10, 12))

        # Invalid periods outside 1..16
        self.assertEqual(parse_period_range("Tiết: 0"), (0, 0))
        self.assertEqual(parse_period_range("Tiết: 17"), (0, 0))

    def test_parse_weekly_grid_table_current_week(self) -> None:
        from tdtu.schedule.parser import parse_weekly_grid_table
        path = FIXTURES_DIR / "schedule_weekly_current.html"
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()

        entries = parse_weekly_grid_table(html, student_id="TEST_STUDENT_001")
        self.assertIsNotNone(entries)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["subject_name"], "Công nghệ phần mềm")
        self.assertEqual(entries[0]["room"], "A0101")
        self.assertEqual(entries[0]["day_of_week"], "Monday")
        self.assertEqual(entries[0]["session_date"], "2026-08-31")
        self.assertEqual(entries[0]["start_period"], 1)
        self.assertEqual(entries[0]["end_period"], 3)

    def test_parse_weekly_grid_table_next_week(self) -> None:
        from tdtu.schedule.parser import parse_weekly_grid_table
        path = FIXTURES_DIR / "schedule_weekly_next.html"
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()

        entries = parse_weekly_grid_table(html, student_id="TEST_STUDENT_001")
        self.assertIsNotNone(entries)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["subject_name"], "Hệ quản trị CSDL")
        self.assertEqual(entries[0]["room"], "B0202")
        self.assertEqual(entries[0]["day_of_week"], "Tuesday")
        self.assertEqual(entries[0]["session_date"], "2026-09-08")
        self.assertEqual(entries[0]["start_period"], 4)
        self.assertEqual(entries[0]["end_period"], 6)

    def test_parse_weekly_grid_table_valid_empty_week(self) -> None:
        from tdtu.schedule.parser import parse_weekly_grid_table
        path = FIXTURES_DIR / "schedule_weekly_empty.html"
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()

        entries = parse_weekly_grid_table(html, student_id="TEST_STUDENT_001")
        self.assertIsNotNone(entries)
        self.assertEqual(entries, [])

    def test_parse_weekly_grid_table_missing_dates_raises_protocol_error(self) -> None:
        from tdtu.exceptions import TDTUProtocolError
        from tdtu.schedule.parser import parse_weekly_grid_table
        path = FIXTURES_DIR / "schedule_weekly_missing_dates.html"
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()

        with self.assertRaises(TDTUProtocolError):
            parse_weekly_grid_table(html, student_id="TEST_STUDENT_001")

    def test_parse_weekly_grid_table_cross_month(self) -> None:
        from tdtu.schedule.parser import parse_weekly_grid_table
        html = """
        <input type="submit" name="ThoiKhoaBieu1$btnTuanHienTai" value="Tuần: 28/09/2026 - 04/10/2026" />
        <table id="ThoiKhoaBieu1_Table1">
            <tr class="Headerrow">
                <td>Tiết</td>
                <td>Thứ 2 (28/09)</td><td>Thứ 3 (29/09)</td><td>Thứ 4 (30/09)</td>
                <td>Thứ 5 (01/10)</td><td>Thứ 6 (02/10)</td><td>Thứ 7 (03/10)</td><td>Chủ nhật (04/10)</td>
            </tr>
            <tr>
                <td>Tiết 1</td>
                <td>-</td><td>-</td><td>-</td>
                <td><span>Phân tích thiết kế hệ thống<br/>Phòng: D0101<br/>Tiết: 101112</span></td>
                <td>-</td><td>-</td><td>-</td>
            </tr>
        </table>
        """
        entries = parse_weekly_grid_table(html, student_id="TEST_STUDENT_001")
        self.assertIsNotNone(entries)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["session_date"], "2026-10-01")
        self.assertEqual(entries[0]["day_of_week"], "Thursday")

    def test_parse_weekly_grid_table_cross_year(self) -> None:
        from tdtu.schedule.parser import parse_weekly_grid_table
        html = """
        <input type="submit" name="ThoiKhoaBieu1$btnTuanHienTai" value="Tuần: 28/12/2026 - 03/01/2027" />
        <table id="ThoiKhoaBieu1_Table1">
            <tr class="Headerrow">
                <td>Tiết</td>
                <td>Thứ 2 (28/12)</td><td>Thứ 3 (29/12)</td><td>Thứ 4 (30/12)</td>
                <td>Thứ 5 (31/12)</td><td>Thứ 6 (01/01)</td><td>Thứ 7 (02/01)</td><td>Chủ nhật (03/01)</td>
            </tr>
            <tr>
                <td>Tiết 1</td>
                <td>-</td><td>-</td><td>-</td><td>-</td>
                <td><span>Khai phá dữ liệu<br/>Phòng: C0101<br/>Tiết: 123</span></td>
                <td>-</td><td>-</td>
            </tr>
        </table>
        """
        entries = parse_weekly_grid_table(html, student_id="TEST_STUDENT_001")
        self.assertIsNotNone(entries)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["session_date"], "2027-01-01")
        self.assertEqual(entries[0]["day_of_week"], "Friday")

    def test_parse_weekly_grid_table_mismatched_weekday_header_raises_error(self) -> None:
        from tdtu.exceptions import TDTUProtocolError
        from tdtu.schedule.parser import parse_weekly_grid_table
        # Header claims Monday is 01/09/2026 (which is a Tuesday in 2026)
        html = """
        <input type="submit" name="ThoiKhoaBieu1$btnTuanHienTai" value="Tuần: 31/08/2026 - 06/09/2026" />
        <table id="ThoiKhoaBieu1_Table1">
            <tr class="Headerrow">
                <td>Tiết</td>
                <td>Thứ 2 (01/09)</td><td>Thứ 3 (02/09)</td><td>Thứ 4 (03/09)</td>
                <td>Thứ 5 (04/09)</td><td>Thứ 6 (05/09)</td><td>Thứ 7 (06/09)</td><td>Chủ nhật (07/09)</td>
            </tr>
        </table>
        """
        with self.assertRaises(TDTUProtocolError) as ctx:
            parse_weekly_grid_table(html)
        self.assertIn("weekday does not match", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
