"""
Service-level unit tests for schedule and exam HTTP orchestration.
Tests weekly control missing, weekly parser None, valid empty weekly page,
malformed exam tab, valid empty exam tab, and semester verification by label and value.
"""

import unittest
from unittest.mock import MagicMock, patch

from tdtu.exceptions import TDTUProtocolError
from tdtu.exams.service import fetch_exam_schedule_http
from tdtu.schedule.service import fetch_schedule_http


class TestServicesOrchestration(unittest.TestCase):

    def _make_mock_client(self, initial_html: str) -> MagicMock:
        client = MagicMock()
        client.student_id = "TEST_STUDENT_001"
        page = MagicMock()
        page.html = initial_html
        client.open_schedule_page.return_value = page
        client.open_exam_page.return_value = page
        return client, page

    def test_schedule_weekly_control_missing_raises_protocol_error(self) -> None:
        html = """
        <select name="ThoiKhoaBieu1$cboHocKy"><option selected="selected" value="136">HK1/2026-2027</option></select>
        <div>No weekly radio control here</div>
        """
        client, page = self._make_mock_client(html)
        with self.assertRaises(TDTUProtocolError) as ctx:
            fetch_schedule_http(client)
        self.assertIn("radXemTKBTheoTuan", str(ctx.exception))

    def test_schedule_weekly_parser_none_raises_protocol_error(self) -> None:
        html = """
        <input type="radio" name="ThoiKhoaBieu1$radChonLua" id="ThoiKhoaBieu1_radXemTKBTheoTuan" value="radXemTKBTheoTuan" />
        <div>Malformed weekly page without btnTuanHienTai or headers</div>
        """
        client, page = self._make_mock_client(html)
        with self.assertRaises(TDTUProtocolError) as ctx:
            fetch_schedule_http(client)
        self.assertIn("grid table missing or malformed", str(ctx.exception))

    def test_schedule_valid_empty_weekly_page_returns_empty_list(self) -> None:
        html = """
        <input type="radio" name="ThoiKhoaBieu1$radChonLua" id="ThoiKhoaBieu1_radXemTKBTheoTuan" value="radXemTKBTheoTuan" />
        <input type="submit" name="ThoiKhoaBieu1$btnTuanHienTai" value="Tuần: 14/09/2026 - 20/09/2026" />
        <table id="ThoiKhoaBieu1_Table1">
            <tr class="Headerrow">
                <td>Tiết</td>
                <td>Thứ 2 (14/09)</td><td>Thứ 3 (15/09)</td><td>Thứ 4 (16/09)</td>
                <td>Thứ 5 (17/09)</td><td>Thứ 6 (18/09)</td><td>Thứ 7 (19/09)</td><td>Chủ nhật (20/09)</td>
            </tr>
            <tr><td>Tiết 1</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td></tr>
        </table>
        """
        client, page = self._make_mock_client(html)
        entries = fetch_schedule_http(client)
        self.assertEqual(entries, [])

    def test_schedule_semester_verification_by_label_and_value(self) -> None:
        html_before = """
        <select name="ThoiKhoaBieu1$cboHocKy">
            <option value="135">HK3/2025-2026</option>
            <option value="136">HK1/2026-2027</option>
        </select>
        <input type="radio" name="ThoiKhoaBieu1$radChonLua" id="ThoiKhoaBieu1_radXemTKBTheoTuan" value="radXemTKBTheoTuan" />
        <input type="submit" name="ThoiKhoaBieu1$btnTuanHienTai" value="Tuần: 14/09/2026 - 20/09/2026" />
        <table id="ThoiKhoaBieu1_Table1">
            <tr class="Headerrow"><td>Tiết</td><td>Thứ 2 (14/09)</td><td>Thứ 3 (15/09)</td><td>Thứ 4 (16/09)</td><td>Thứ 5 (17/09)</td><td>Thứ 6 (18/09)</td><td>Thứ 7 (19/09)</td><td>Chủ nhật (20/09)</td></tr>
        </table>
        """
        html_after = """
        <select name="ThoiKhoaBieu1$cboHocKy">
            <option value="135">HK3/2025-2026</option>
            <option value="136" selected="selected">HK1/2026-2027</option>
        </select>
        <input type="radio" name="ThoiKhoaBieu1$radChonLua" id="ThoiKhoaBieu1_radXemTKBTheoTuan" value="radXemTKBTheoTuan" />
        <input type="submit" name="ThoiKhoaBieu1$btnTuanHienTai" value="Tuần: 14/09/2026 - 20/09/2026" />
        <table id="ThoiKhoaBieu1_Table1">
            <tr class="Headerrow"><td>Tiết</td><td>Thứ 2 (14/09)</td><td>Thứ 3 (15/09)</td><td>Thứ 4 (16/09)</td><td>Thứ 5 (17/09)</td><td>Thứ 6 (18/09)</td><td>Thứ 7 (19/09)</td><td>Chủ nhật (20/09)</td></tr>
        </table>
        """
        # Test by option value "136"
        client, page = self._make_mock_client(html_before)
        def mock_postback(*args, **kwargs):
            page.html = html_after
        page.postback.side_effect = mock_postback

        entries = fetch_schedule_http(client, selected_semester="136")
        self.assertEqual(entries, [])

        # Test by option label "HK1/2026-2027"
        client, page = self._make_mock_client(html_before)
        page.postback.side_effect = mock_postback
        entries = fetch_schedule_http(client, selected_semester="HK1/2026-2027")
        self.assertEqual(entries, [])

    def test_exam_malformed_tab_raises_protocol_error(self) -> None:
        html = """
        <select name="LichThi1$cboHocKy"><option selected="selected" value="136">HK1/2026-2027</option></select>
        <table id="LichThi1_Menu1">
            <tr><td><a href="javascript:__doPostBack('LichThi1$Menu1','0')">Midterm</a></td></tr>
        </table>
        <div>Missing LichThi1_GiuaKyTable container here</div>
        """
        client, page = self._make_mock_client(html)
        with self.assertRaises(TDTUProtocolError) as ctx:
            fetch_exam_schedule_http(client)
        self.assertIn("Expected exam table container", str(ctx.exception))

    def test_exam_valid_empty_tab_returns_empty_list(self) -> None:
        html = """
        <select name="LichThi1$cboHocKy"><option selected="selected" value="136">HK1/2026-2027</option></select>
        <table id="LichThi1_Menu1">
            <tr><td><a href="javascript:__doPostBack('LichThi1$Menu1','0')">Midterm</a></td></tr>
        </table>
        <table id="LichThi1_GiuaKyTable">
            <tr class="Headerrow"><td>Môn học</td><td>Ngày thi</td><td>Giờ thi</td></tr>
        </table>
        """
        client, page = self._make_mock_client(html)
        with patch.dict("os.environ", {"TARGET_EXAM_TYPES": "midterm"}):
            exams = fetch_exam_schedule_http(client)
        self.assertEqual(exams, [])

    def test_exam_semester_verification_by_label_and_value(self) -> None:
        html_before = """
        <select name="LichThi1$cboHocKy">
            <option value="135">HK3/2025-2026</option>
            <option value="136">HK1/2026-2027</option>
        </select>
        <table id="LichThi1_Menu1">
            <tr><td><a href="javascript:__doPostBack('LichThi1$Menu1','0')">Midterm</a></td></tr>
        </table>
        <table id="LichThi1_GiuaKyTable"><tr class="Headerrow"><td>Môn</td></tr></table>
        """
        html_after = """
        <select name="LichThi1$cboHocKy">
            <option value="135">HK3/2025-2026</option>
            <option value="136" selected="selected">HK1/2026-2027</option>
        </select>
        <table id="LichThi1_Menu1">
            <tr><td><a href="javascript:__doPostBack('LichThi1$Menu1','0')">Midterm</a></td></tr>
        </table>
        <table id="LichThi1_GiuaKyTable"><tr class="Headerrow"><td>Môn</td></tr></table>
        """
        client, page = self._make_mock_client(html_before)
        def mock_postback(*args, **kwargs):
            page.html = html_after
        page.postback.side_effect = mock_postback

        with patch.dict("os.environ", {"TARGET_EXAM_TYPES": "midterm"}):
            # Test by value "136"
            exams = fetch_exam_schedule_http(client, selected_semester="136")
            self.assertEqual(exams, [])

            # Test by text "HK1/2026-2027"
            client, page = self._make_mock_client(html_before)
            page.postback.side_effect = mock_postback
            exams = fetch_exam_schedule_http(client, selected_semester="HK1/2026-2027")
            self.assertEqual(exams, [])


if __name__ == "__main__":
    unittest.main()
