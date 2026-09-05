import unittest
from unittest.mock import patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from crawler import (
  ELEARNING_SELECTOR_USERNAME,
  _click_portal_control,
  _configure_schedule_filters,
  _deduplicate_schedule_rows,
  _login_and_open_elearning_dashboard,
  _launch_chromium,
  _parse_elearning_progress,
  _parse_exam_table,
  _parse_weekly_grid_table,
  _sanitize_url_for_log,
  _switch_to_week_view_if_available,
)


class PortalControlRetryTests(unittest.TestCase):
    def test_portal_control_retries_after_timeout(self) -> None:
        class FakeControl:
            def __init__(self) -> None:
                self.timeouts: list[int] = []

            def click(self, *, timeout: int) -> None:
                self.timeouts.append(timeout)
                if len(self.timeouts) == 1:
                    raise PlaywrightTimeoutError("temporary portal delay")

        class FakePage:
            def __init__(self) -> None:
                self.waits: list[tuple[str, int]] = []

            def wait_for_load_state(self, state: str, *, timeout: int) -> None:
                self.waits.append((state, timeout))

        control = FakeControl()
        page = FakePage()

        _click_portal_control(page, control, "test portal control")

        self.assertEqual(control.timeouts, [30_000, 30_000])
        self.assertEqual(page.waits, [("domcontentloaded", 30_000)])


class ElearningNavigationTests(unittest.TestCase):
    def test_elearning_login_uses_domcontentloaded_and_waits_for_dashboard(self) -> None:
        class FakeLocator:
            def __init__(self, page) -> None:
                self.page = page
                self.first = self

            def click(self, *, timeout: int) -> None:
                self.page.actions.append(("click", timeout))

        class FakePage:
            def __init__(self) -> None:
                self.url = "https://elearning.tdtu.edu.vn/login/index.php"
                self.actions = []

            def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
                self.actions.append(("goto", url, wait_until, timeout))
                if url.endswith("/my/"):
                    self.url = url

            def wait_for_selector(self, selector: str, **kwargs) -> None:
                self.actions.append(("wait_for_selector", selector, kwargs))

            def fill(self, selector: str, value: str) -> None:
                self.actions.append(("fill", selector, value))

            def locator(self, selector: str) -> FakeLocator:
                self.actions.append(("locator", selector))
                return FakeLocator(self)

            def wait_for_url(self, predicate, *, timeout: int) -> None:
                self.actions.append(("wait_for_url", timeout))
                self.url = "https://elearning.tdtu.edu.vn/my/"
                self.asserted_url = predicate(self.url)

        page = FakePage()
        _login_and_open_elearning_dashboard(page, "student", "password")

        navigations = [action for action in page.actions if action[0] == "goto"]
        self.assertEqual([action[2] for action in navigations], ["domcontentloaded", "domcontentloaded"])
        self.assertTrue(page.asserted_url)
        self.assertIn(("fill", ELEARNING_SELECTOR_USERNAME, "student"), page.actions)

    def test_elearning_login_timeout_reports_stuck_login_page(self) -> None:
        class FakePage:
            url = "https://elearning.tdtu.edu.vn/login/index.php"

            def goto(self, *args, **kwargs) -> None:
                pass

            def wait_for_selector(self, *args, **kwargs) -> None:
                pass

            def fill(self, *args, **kwargs) -> None:
                pass

            def locator(self, *args, **kwargs):
                class Locator:
                    first = None

                    def click(self, **kwargs) -> None:
                        pass

                locator = Locator()
                locator.first = locator
                return locator

            def wait_for_url(self, *args, **kwargs) -> None:
                raise PlaywrightTimeoutError("login timed out")

        with self.assertRaisesRegex(RuntimeError, "did not leave the login page"):
            _login_and_open_elearning_dashboard(FakePage(), "student", "password")

    def test_elearning_dashboard_redirect_to_login_is_rejected(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.url = "https://elearning.tdtu.edu.vn/login/index.php"

            def goto(self, url: str, **kwargs) -> None:
                if url.endswith("/my/"):
                    self.url = "https://elearning.tdtu.edu.vn/login/index.php"

            def wait_for_selector(self, *args, **kwargs) -> None:
                pass

            def fill(self, *args, **kwargs) -> None:
                pass

            def locator(self, *args, **kwargs):
                class Locator:
                    first = None

                    def click(self, **kwargs) -> None:
                        pass

                locator = Locator()
                locator.first = locator
                return locator

            def wait_for_url(self, predicate, **kwargs) -> None:
                self.url = "https://elearning.tdtu.edu.vn/"

        with self.assertRaisesRegex(RuntimeError, "dashboard redirected to the login page"):
            _login_and_open_elearning_dashboard(FakePage(), "student", "password")


class CrawlerParserTests(unittest.TestCase):

    def test_sanitize_url_for_log_redacts_portal_session_values(self) -> None:
        url = "https://example.test/tkb?Token=secret-token&RequestId=secret-request&week=1"

        self.assertEqual(
            _sanitize_url_for_log(url),
            "https://example.test/tkb?Token=[redacted]&RequestId=[redacted]&week=1",
        )

    def test_launch_chromium_explains_how_to_install_missing_browser(self) -> None:
        class FakeChromium:
            executable_path = "/tmp/tool-check-tkb-missing-chromium"

            def launch(self, **kwargs):
                raise AssertionError("launch must not be called when the executable is missing")

        class FakePlaywright:
            chromium = FakeChromium()

        with self.assertRaisesRegex(RuntimeError, r"python -m playwright install chromium"):
            _launch_chromium(FakePlaywright())

    def setUp(self) -> None:
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=True)
            self.page = self.browser.new_page()
        except Exception as exc:
            self.skipTest(f"Playwright browser unavailable: {exc}")

    def tearDown(self) -> None:
        if hasattr(self, "browser"):
            self.browser.close()
        if hasattr(self, "playwright"):
            self.playwright.stop()



    def test_parse_elearning_progress_extracts_percentages(self) -> None:
        self.page.set_content(
            """
            <html><body>
              <div class="dashboard-card" data-course-id="101">
                <a class="aalink coursename" href="/course/view.php?id=101">Toan Cao Cap</a>
                <div class="progress-bar" style="width: 78%"></div>
                <div>78%</div>
                <div>7/9</div>
              </div>
              <div class="dashboard-card" data-course-id="102">
                <h3>Lap trinh Python</h3>
                <div aria-valuenow="55"></div>
                <div>55%</div>
                <div>11/20</div>
              </div>
            </body></html>
            """
        )

        rows = _parse_elearning_progress(self.page)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["course_id"], "101")
        self.assertEqual(rows[0]["course_name"], "Toan Cao Cap")
        self.assertEqual(rows[0]["progress_percent"], 78)
        self.assertEqual(rows[0]["lessons_completed"], 7)
        self.assertEqual(rows[0]["lessons_total"], 9)
        self.assertEqual(rows[1]["course_id"], "102")
        self.assertEqual(rows[1]["course_name"], "Lap trinh Python")
        self.assertEqual(rows[1]["progress_percent"], 55)
        self.assertEqual(rows[1]["lessons_completed"], 11)
        self.assertEqual(rows[1]["lessons_total"], 20)

    def test_parse_exam_table_keeps_full_end_time(self) -> None:
        self.page.set_content(
            """
            <html><body>
              <table>
                <tr><th>Môn</th><th>Ngày thi</th><th>Giờ</th><th>Phòng</th><th>Hình thức</th></tr>
                <tr><td>Toan Cao Cap</td><td>25/06/2026</td><td>07:30 - 09:00</td><td>A101</td><td>Truc tiep</td></tr>
                <tr><td>Lap trinh Python</td><td>26/06/2026</td><td>13h00-15h00</td><td>B202</td><td>Online</td></tr>
              </table>
            </body></html>
            """
        )

        rows = _parse_exam_table(self.page)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["subject_name"], "Toan Cao Cap")
        self.assertEqual(rows[0]["exam_date"], "2026-06-25")
        self.assertEqual(rows[0]["start_time"], "07:30")
        self.assertEqual(rows[0]["end_time"], "09:00")
        self.assertEqual(rows[1]["subject_name"], "Lap trinh Python")
        self.assertEqual(rows[1]["exam_date"], "2026-06-26")
        self.assertEqual(rows[1]["start_time"], "13:00")
        self.assertEqual(rows[1]["end_time"], "15:00")

    def test_parse_exam_table_from_grid_cells(self) -> None:
        self.page.set_content(
            """
            <html><body>
              <table>
                <tr><th>Thứ 2 | Monday</th><th>Thứ 5 | Thursday</th></tr>
                <tr>
                  <td>
                    Triết học Mác - Lênin<br>
                    Ngày thi|Date: 16/05/2026<br>
                    Giờ thi|Time: 07:30-09:00<br>
                    Phòng|Room: A707
                  </td>
                  <td>
                    Nhập môn hệ điều hành<br>
                    Ngày thi|Date: 28/05/2026<br>
                    Giờ thi|Time: 07:30<br>
                    Phòng|Room: A504
                  </td>
                </tr>
              </table>
            </body></html>
            """
        )

        rows = _parse_exam_table(self.page)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["subject_name"], "Triết học Mác - Lênin")
        self.assertEqual(rows[0]["exam_date"], "2026-05-16")
        self.assertEqual(rows[0]["start_time"], "07:30")
        self.assertEqual(rows[0]["end_time"], "09:00")
        self.assertEqual(rows[0]["exam_room"], "A707")
        self.assertEqual(rows[1]["subject_name"], "Nhập môn hệ điều hành")
        self.assertEqual(rows[1]["exam_date"], "2026-05-28")
        self.assertEqual(rows[1]["start_time"], "07:30")
        self.assertEqual(rows[1]["exam_room"], "A504")

    def test_parse_weekly_grid_table_extracts_status(self) -> None:
        self.page.set_content(
            """
            <html><body>
              <table>
                <tr>
                  <th>Period</th>
                  <th>Thu 2 25/04/2026</th>
                  <th>Thu 3 26/04/2026</th>
                </tr>
                <tr>
                  <td>1</td>
                  <td>Toan cao cap\nPhong|Room: A101</td>
                  <td></td>
                </tr>
                <tr>
                  <td>2</td>
                  <td>Lap trinh web\nGV bao vang\nPhong|Room: B202</td>
                  <td></td>
                </tr>
                <tr>
                  <td>3</td>
                  <td></td>
                  <td>Co so du lieu\nHoc bu\nPhong|Room: C303</td>
                </tr>
              </table>
            </body></html>
            """
        )

        rows = _parse_weekly_grid_table(self.page, "520H0001")

        self.assertIsNotNone(rows)
        assert rows is not None
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["status"], "scheduled")
        self.assertEqual(rows[0]["session_date"], "2026-04-25")
        self.assertEqual(rows[1]["status"], "absent")
        self.assertEqual(rows[2]["status"], "makeup")

    def test_deduplicate_schedule_rows_keeps_status_variants(self) -> None:
        rows = [
            {
                "subject_name": "Lập trình hướng đối tượng",
                "room": "B311",
                "day_of_week": "Monday",
                "session_date": "2026-04-25",
                "start_period": 1,
                "end_period": 2,
                "status": "absent",
            },
            {
                "subject_name": "Lập trình hướng đối tượng",
                "room": "B311",
                "day_of_week": "Monday",
                "session_date": "2026-04-25",
                "start_period": 1,
                "end_period": 2,
                "status": "makeup",
            },
        ]

        deduped = _deduplicate_schedule_rows(rows)

        self.assertEqual(len(deduped), 2)
        self.assertEqual({row["status"] for row in deduped}, {"absent", "makeup"})

    def test_parse_weekly_grid_table_nested_table_cell(self) -> None:
        # A cell that contains a nested table with two inner <td> entries
        # (e.g., absent + makeup in the same parent cell) should be split
        # into two separate schedule rows.
        self.page.set_content(
            '''
            <html><body>
              <table>
                <tr>
                  <th>Period</th>
                  <th>Thu 2 04/05/2026</th>
                  <th>Thu 3 05/05/2026</th>
                </tr>
                <tr>
                  <td>1</td>
                  <td></td>
                  <td class="cell" rowspan="3" style="color:White;background-color:#ff3b3b;">
                    <table width="100%" cellpadding="0" cellspacing="0"><tbody>
                      <tr>
                        <td style="border-right-width: 1px;border-right-style: dotted;">
                          <b>Lập trình hướng đối tượng</b><br>
                          Phòng|Room: B311<br>
                          <b style="color:DarkRed;">GV báo vắng</b>
                        </td>
                        <td>
                          <b>Lập trình hướng đối tượng</b><br>
                          Phòng|Room: C407<br>
                          <b style="color:yellow;">GV dạy bù</b>
                        </td>
                      </tr>
                    </tbody></table>
                  </td>
                </tr>
              </table>
            </body></html>
            '''
        )

        rows = _parse_weekly_grid_table(self.page, "520H0001")
        self.assertIsNotNone(rows)
        assert rows is not None
        # Should extract two entries from the nested table cell
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["status"] for r in rows}, {"absent", "makeup"})
        self.assertEqual({r.get("room") for r in rows}, {"B311", "C407"})
        for r in rows:
          self.assertEqual(r["subject_name"], "Lập trình hướng đối tượng")

    def test_switch_to_week_view_clicks_exact_label(self) -> None:
        self.page.set_content(
            """
            <label for="ThoiKhoaBieu1_radXemTKBTheoTuan"
                   onclick="window.weeklyLabelClicks = (window.weeklyLabelClicks || 0) + 1">
              Xem thời khóa biểu theo tuần | See the weekly schedule
            </label>
            <input id="ThoiKhoaBieu1_radXemTKBTheoTuan"
                   type="radio" name="schedule-view">
            """
        )

        changed = _switch_to_week_view_if_available(self.page)

        self.assertTrue(changed)
        self.assertTrue(self.page.locator("#ThoiKhoaBieu1_radXemTKBTheoTuan").is_checked())
        self.assertEqual(self.page.evaluate("window.weeklyLabelClicks || 0"), 1)

    def test_configure_schedule_filters_waits_for_weekly_table(self) -> None:
        events: list[object] = []

        class FakePage:
            def wait_for_selector(self, selector: str, *, state: str, timeout: int) -> None:
                events.append(("wait", selector, state, timeout))

        with (
            patch("crawler._select_semester_if_available", side_effect=lambda page: events.append("semester") or True),
            patch("crawler._switch_to_week_view_if_available", side_effect=lambda page: events.append("week") or True),
        ):
            _configure_schedule_filters(FakePage())

        self.assertEqual(
            events,
            [
                "semester",
                "week",
                ("wait", "#ThoiKhoaBieu1_tbTKBTheoTuan", "visible", 30_000),
            ],
        )

    def test_parse_real_weekly_grid_uses_week_range_year_and_cleans_room(self) -> None:
        self.page.set_content(
            """
            <input id="ThoiKhoaBieu1_btnTuanHienTai" value="29/12/2025 - 04/01/2026">
            <table id="ThoiKhoaBieu1_tbTKBTheoTuan">
              <tr class="Headerrow">
                <td>Tiết|Thứ<br>Period | Day</td>
                <td>Thứ 2 | Monday<br>(29/12)</td>
                <td>Chủ nhật | Sunday<br>(04/01)</td>
              </tr>
              <tr>
                <td class="cellbuoi">1</td>
                <td class="cell" rowspan="3"><table><tr><td><b>Giải tích</b><br>(501031 - Nhóm|Groups: 1)<br>Phòng|Room:</td></tr></table></td>
                <td class="cell"></td>
              </tr>
              <tr><td class="cellbuoi">2</td><td class="cell"></td></tr>
              <tr><td class="cellbuoi">3</td><td class="cell">Hệ điều hành<br>Tiết|Period: 3 (Phòng:|Room: A103)</td></tr>
              <tr>
                <td class="cellbuoi">4</td>
                <td class="cell"></td>
                <td class="cell">Lập trình Python<br>Tiết|Period: 456 (Phòng:|Room: A102)</td>
              </tr>
            </table>
            """
        )

        rows = _parse_weekly_grid_table(self.page, "520H0001")

        self.assertIsNotNone(rows)
        assert rows is not None
        self.assertEqual(
            [
                (
                    row["subject_name"],
                    row["room"],
                    row["day_of_week"],
                    row["session_date"],
                    row["start_period"],
                    row["end_period"],
                )
                for row in rows
            ],
            [
                ("Giải tích", "", "Monday", "2025-12-29", 1, 3),
                ("Hệ điều hành", "A103", "Sunday", "2026-01-04", 3, 3),
                ("Lập trình Python", "A102", "Sunday", "2026-01-04", 4, 6),
            ],
        )

    def test_parse_weekly_grid_table_with_morning_afternoon_rows(self) -> None:
        self.page.set_content(
            """
            <html><body>
              <table>
                <tr>
                  <th></th>
                  <th>Thứ 2 | Monday 04/08/2026</th>
                  <th>Thứ 3 | Tuesday 05/08/2026</th>
                </tr>
                <tr>
                  <td>Morning</td>
                  <td class="cell">
                    Kinh tế chính trị Mác-Lênin<br>
                    Tiết|Period: 123 (Phòng:|Room: A101)
                  </td>
                  <td></td>
                </tr>
                <tr>
                  <td>Afternoon</td>
                  <td></td>
                  <td class="cell">
                    Cấu trúc dữ liệu và giải thuật<br>
                    Tiết|Period: 789 (Phòng:|Room: B202)
                  </td>
                </tr>
              </table>
            </body></html>
            """
        )

        rows = _parse_weekly_grid_table(self.page, "520H0001")
        self.assertIsNotNone(rows)
        assert rows is not None
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["subject_name"], "Kinh tế chính trị Mác-Lênin")
        self.assertEqual(rows[0]["day_of_week"], "Monday")
        self.assertEqual(rows[0]["session_date"], "2026-08-04")
        self.assertEqual(rows[0]["start_period"], 1)
        self.assertEqual(rows[0]["end_period"], 3)
        self.assertEqual(rows[1]["subject_name"], "Cấu trúc dữ liệu và giải thuật")
        self.assertEqual(rows[1]["day_of_week"], "Tuesday")
        self.assertEqual(rows[1]["session_date"], "2026-08-05")
        self.assertEqual(rows[1]["start_period"], 7)
        self.assertEqual(rows[1]["end_period"], 9)


if __name__ == "__main__":
    unittest.main()
