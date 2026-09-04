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


    def test_parse_date_iso_semester_boundary(self) -> None:
        from tdtu.exams.parser import parse_date_iso
        self.assertEqual(parse_date_iso("15/12", semester_hint="HK1/2026-2027"), "2026-12-15")
        self.assertEqual(parse_date_iso("15/04", semester_hint="HK2/2026-2027"), "2027-04-15")
        self.assertEqual(parse_date_iso("15/09/2026"), "2026-09-15")
        self.assertEqual(parse_date_iso("31/02/2026"), "")  # Invalid date rejected

    def test_validate_exam_tab_structure(self) -> None:
        from tdtu.exams.parser import validate_exam_tab_structure
        # Valid final exam tab structure
        self.assertTrue(validate_exam_tab_structure(self.html, "1"))

        # Valid empty table container for midterm ("0")
        valid_empty_midterm = """
        <select name="LichThi1$cboHocKy"><option selected="selected" value="136">HK1/2026-2027</option></select>
        <table id="LichThi1_GiuaKyTable"><tr><td>No data</td></tr></table>
        """
        self.assertTrue(validate_exam_tab_structure(valid_empty_midterm, "0"))

        # Malformed page missing midterm table container
        malformed_midterm = """
        <select name="LichThi1$cboHocKy"><option selected="selected" value="136">HK1/2026-2027</option></select>
        <table id="LichThi1_Menu1"><tr><td><a href="...">Xem lịch thi giữa kỳ</a></td></tr></table>
        <div>Some random text without exam table</div>
        """
        self.assertFalse(validate_exam_tab_structure(malformed_midterm, "0"))


if __name__ == "__main__":
    unittest.main()
