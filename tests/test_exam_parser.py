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

    def test_validate_exam_tab_structure_exact_ids(self) -> None:
        from tdtu.exams.parser import validate_exam_tab_structure

        giua_ky_html = '<table id="LichThi1_GiuaKyTable"><tr><th>Thứ</th></tr></table>'
        cuoi_ky_html = '<table id="LichThi1_CuoiKyTable"><tr><th>Thứ</th></tr></table>'
        cuoi_ky2_html = '<table id="LichThi1_CuoiKy2Table"><tr><th>Thứ</th></tr></table>'

        self.assertTrue(validate_exam_tab_structure(giua_ky_html, "0"))
        self.assertTrue(validate_exam_tab_structure(cuoi_ky_html, "1"))
        self.assertTrue(validate_exam_tab_structure(cuoi_ky2_html, "2"))

        # CuoiKy2Table must NOT satisfy tab 1 (CuoiKy)
        self.assertFalse(validate_exam_tab_structure(cuoi_ky2_html, "1"))
        # CuoiKyTable must NOT satisfy tab 2 (CuoiKy2)
        self.assertFalse(validate_exam_tab_structure(cuoi_ky_html, "2"))

    def test_parse_exam_tab_scoping(self) -> None:
        dual_tab_html = """
        <table id="LichThi1_CuoiKyTable">
            <tr><th>Mã MH</th><th>Tên môn học</th><th>Ngày thi</th><th>Giờ thi</th><th>Phòng thi</th><th>Hình thức thi</th></tr>
            <tr><td>001</td><td>Môn Cuối Kỳ 1</td><td>15/12/2026</td><td>07:30 - 09:00</td><td>C311</td><td>Tự luận</td></tr>
        </table>
        <table id="LichThi1_CuoiKy2Table">
            <tr><th>Mã MH</th><th>Tên môn học</th><th>Ngày thi</th><th>Giờ thi</th><th>Phòng thi</th><th>Hình thức thi</th></tr>
            <tr><td>002</td><td>Môn Cuối Kỳ 2</td><td>20/12/2026</td><td>13:30 - 15:00</td><td>B311</td><td>Trắc nghiệm</td></tr>
        </table>
        """
        # Tab 1 should ONLY parse CuoiKyTable
        exams_tab1 = parse_exam_html(dual_tab_html, tab_arg="1")
        self.assertEqual(len(exams_tab1), 1)
        self.assertEqual(exams_tab1[0]["subject_name"], "Môn Cuối Kỳ 1")

        # Tab 2 should ONLY parse CuoiKy2Table
        exams_tab2 = parse_exam_html(dual_tab_html, tab_arg="2")
        self.assertEqual(len(exams_tab2), 1)
        self.assertEqual(exams_tab2[0]["subject_name"], "Môn Cuối Kỳ 2")

    def test_empty_vs_malformed_nonempty_exam_table(self) -> None:
        from tdtu.exceptions import TDTUParsingError

        header_only_html = """
        <table id="LichThi1_CuoiKyTable">
            <tr><th>Mã MH</th><th>Tên môn học</th><th>Ngày thi</th></tr>
        </table>
        """
        self.assertEqual(parse_exam_html(header_only_html, tab_arg="1"), [])

        valid_row_html = """
        <table id="LichThi1_CuoiKyTable">
            <tr><th>Mã MH</th><th>Tên môn học</th><th>Ngày thi</th><th>Giờ thi</th><th>Phòng thi</th><th>Hình thức thi</th></tr>
            <tr><td>001</td><td>Môn Học A</td><td>15/12/2026</td><td>07:30 - 09:00</td><td>C311</td><td>Tự luận</td></tr>
        </table>
        """
        exams = parse_exam_html(valid_row_html, tab_arg="1")
        self.assertEqual(len(exams), 1)

        malformed_nonempty_html = """
        <table id="LichThi1_CuoiKyTable">
            <tr><th>Header 1</th><th>Header 2</th></tr>
            <tr><td>Malformed Row Without Any Parseable Subject/Date Data</td><td>Broken Column</td></tr>
        </table>
        """
        with self.assertRaises(TDTUParsingError):
            parse_exam_html(malformed_nonempty_html, tab_arg="1")

    def test_parse_calendar_grid_exam_table(self) -> None:
        grid_html = """
        <table id="LichThi1_GiuaKyTable">
            <tr class="Headerrow">
                <td>Thứ 2|Monday</td>
                <td>Thứ 3|Tuesday</td>
                <td>Thứ 4|Wednesday</td>
                <td>Thứ 5|Thursday</td>
                <td>Thứ 6|Friday</td>
                <td>Thứ 7|Saturday</td>
                <td>Chủ nhật|Sunday</td>
            </tr>
            <tr class="rowContent" id="LichThi1_row1">
                <td class="cell" id="LichThi1_row1col1" valign="top">
                    <span style="display:inline-block;background-color:#e67b09;width:150px;">
                        <p style="padding:0px 0px 10px 0px; margin:0px;">
                            <b><span style="color:black">Kinh tế chính trị Mác-Lênin<br/><label class="lbl-lang" style="color:black;">| Political Economics of Marxism and Leninism</label></span></b>
                        </p>
                    </span>
                    <br/>(306103)<br/>
                    Nhóm<label class="lbl-lang" style="color:black;">|Group:</label><b> 13</b><br/>
                    Tổ<label class="lbl-lang" style="color:black;">|Sub-group:</label><b> 003</b><br/>
                    Ngày thi<label class="lbl-lang" style="color:black;">|Date:</label> 28/09/2026<br/>
                    Giờ thi<label class="lbl-lang" style="color:black;">|Time:</label> 19:05<br/>
                    Phòng<label class="lbl-lang" style="color:black;">|Room:</label> A703<br/>
                    Thời lượng<label class="lbl-lang" style="color:black;">|Duration:</label> 30 phút<label class="lbl-lang" style="color:black;">|minute</label>
                </td>
                <td class="cell" id="LichThi1_row1col2">
                    <span style="display:inline-block;background-color:#6db33f;width:150px;">
                        <p style="padding:0px 0px 10px 0px; margin:0px;">
                            <b><span style="color:black">Hệ cơ sở dữ liệu<br/><label class="lbl-lang" style="color:black;">| Database Systems</label></span></b>
                        </p>
                    </span>
                    <br/>(502051)<br/>
                    Nhóm<label class="lbl-lang" style="color:black;">|Group:</label><b> 01</b><br/>
                    Tổ<label class="lbl-lang" style="color:black;">|Sub-group:</label><b> 001</b><br/>
                    Ngày thi<label class="lbl-lang" style="color:black;">|Date:</label> 29/09/2026<br/>
                    Giờ thi<label class="lbl-lang" style="color:black;">|Time:</label> 16:10<br/>
                    Phòng<label class="lbl-lang" style="color:black;">|Room:</label> A510<br/>
                    Thời lượng<label class="lbl-lang" style="color:black;">|Duration:</label> 45 phút<label class="lbl-lang" style="color:black;">|minute</label>
                </td>
                <td class="cell" id="LichThi1_row1col3"></td>
                <td class="cell" id="LichThi1_row1col4"></td>
                <td class="cell" id="LichThi1_row1col5">
                    <span style="display:inline-block;background-color:#e32d5d;width:150px;">
                        <p style="padding:0px 0px 10px 0px; margin:0px;">
                            <b><span style="color:black">Cấu trúc dữ liệu và giải thuật<br/><label class="lbl-lang" style="color:black;">| Data Structures And Algorithms</label></span></b>
                        </p>
                    </span>
                    <br/>(504008)<br/>
                    Nhóm<label class="lbl-lang" style="color:black;">|Group:</label><b> 01</b><br/>
                    Tổ<label class="lbl-lang" style="color:black;">|Sub-group:</label><b> 002</b><br/>
                    Ngày thi<label class="lbl-lang" style="color:black;">|Date:</label> 02/10/2026<br/>
                    Giờ thi<label class="lbl-lang" style="color:black;">|Time:</label> 07:30<br/>
                    Phòng<label class="lbl-lang" style="color:black;">|Room:</label> A603<br/>
                    Thời lượng<label class="lbl-lang" style="color:black;">|Duration:</label> 45 phút<label class="lbl-lang" style="color:black;">|minute</label>
                </td>
                <td class="cell" id="LichThi1_row1col6"></td>
                <td class="cell" id="LichThi1_row1col7"></td>
            </tr>
        </table>
        """
        # Test with tab_arg="0"
        exams = parse_exam_html(grid_html, default_exam_type="Giữa kỳ", tab_arg="0")
        self.assertEqual(len(exams), 3)

        ex1 = exams[0]
        self.assertEqual(ex1["subject_name"], "Kinh tế chính trị Mác-Lênin")
        self.assertEqual(ex1["exam_date"], "2026-09-28")
        self.assertEqual(ex1["start_time"], "19:05")
        self.assertEqual(ex1["end_time"], "19:35")
        self.assertEqual(ex1["exam_room"], "A703")
        self.assertEqual(ex1["exam_type"], "Giữa kỳ")
        self.assertIn("306103", ex1["notes"])
        self.assertIn("13", ex1["notes"])
        self.assertIn("003", ex1["notes"])

        ex2 = exams[1]
        self.assertEqual(ex2["subject_name"], "Hệ cơ sở dữ liệu")
        self.assertEqual(ex2["exam_date"], "2026-09-29")
        self.assertEqual(ex2["start_time"], "16:10")
        self.assertEqual(ex2["end_time"], "16:55")
        self.assertEqual(ex2["exam_room"], "A510")
        self.assertEqual(ex2["exam_type"], "Giữa kỳ")

        ex3 = exams[2]
        self.assertEqual(ex3["subject_name"], "Cấu trúc dữ liệu và giải thuật")
        self.assertEqual(ex3["exam_date"], "2026-10-02")
        self.assertEqual(ex3["start_time"], "07:30")
        self.assertEqual(ex3["end_time"], "08:15")
        self.assertEqual(ex3["exam_room"], "A603")
        self.assertEqual(ex3["exam_type"], "Giữa kỳ")

    def test_parse_calendar_grid_empty_tab_returns_empty(self) -> None:
        empty_grid_html = """
        <table id="LichThi1_CuoiKyTable">
            <tr class="Headerrow">
                <td>Thứ 2|Monday</td>
                <td>Thứ 3|Tuesday</td>
                <td>Thứ 4|Wednesday</td>
                <td>Thứ 5|Thursday</td>
                <td>Thứ 6|Friday</td>
                <td>Thứ 7|Saturday</td>
                <td>Chủ nhật|Sunday</td>
            </tr>
            <tr class="rowContent" id="LichThi1_row1">
                <td class="cell" id="LichThi1_row1col1"></td>
                <td class="cell" id="LichThi1_row1col2"></td>
                <td class="cell" id="LichThi1_row1col3"></td>
                <td class="cell" id="LichThi1_row1col4"></td>
                <td class="cell" id="LichThi1_row1col5"></td>
                <td class="cell" id="LichThi1_row1col6"></td>
                <td class="cell" id="LichThi1_row1col7"></td>
            </tr>
        </table>
        """
        exams = parse_exam_html(empty_grid_html, default_exam_type="Cuối kỳ", tab_arg="1")
        self.assertEqual(exams, [])


if __name__ == "__main__":
    unittest.main()


