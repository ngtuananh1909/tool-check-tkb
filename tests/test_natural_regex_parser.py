"""Unit tests for Vietnamese natural Regex parser."""

from __future__ import annotations

import datetime as dt
import unittest

from natural_regex_parser import (
    DEFAULT_DURATION_MINUTES,
    DEFAULT_REMINDER_MINUTES,
    _add_minutes_to_time,
    _extract_date,
    _extract_location,
    _extract_reminder,
    _extract_time_and_duration,
    _time_diff_minutes,
    parse_quick_add,
)


class TestNaturalRegexParser(unittest.TestCase):
    def setUp(self) -> None:
        # Base reference date: Wednesday, 2026-05-13
        self.ref_date = dt.date(2026, 5, 13)

    def test_relative_dates(self) -> None:
        d1, _ = _extract_date("mai 9h họp", self.ref_date)
        self.assertEqual(d1, dt.date(2026, 5, 14))

        d2, _ = _extract_date("ngày mốt 14h phỏng vấn", self.ref_date)
        self.assertEqual(d2, dt.date(2026, 5, 15))

        d3, _ = _extract_date("hôm nay 18h đá bóng", self.ref_date)
        self.assertEqual(d3, dt.date(2026, 5, 13))

    def test_weekday_dates(self) -> None:
        # ref_date is Wednesday (weekday 2).
        # "thứ 5" should be Thursday 2026-05-14 (1 day ahead)
        d_thu5, _ = _extract_date("thứ 5 9h họp", self.ref_date)
        self.assertEqual(d_thu5, dt.date(2026, 5, 14))

        # "thứ 6" should be Friday 2026-05-15 (2 days ahead)
        d_thu6, _ = _extract_date("thứ 6 15h seminar", self.ref_date)
        self.assertEqual(d_thu6, dt.date(2026, 5, 15))

        # "thứ 2 tuần sau" should be Monday 2026-05-18 (5 days ahead)
        d_t2_next, _ = _extract_date("thứ 2 tuần sau 8h chào cờ", self.ref_date)
        self.assertEqual(d_t2_next, dt.date(2026, 5, 18))

        # "chủ nhật" should be Sunday 2026-05-17 (4 days ahead)
        d_cn, _ = _extract_date("chủ nhật 9h cafe", self.ref_date)
        self.assertEqual(d_cn, dt.date(2026, 5, 17))

    def test_numeric_dates(self) -> None:
        d_dm, _ = _extract_date("16/5 9h họp", self.ref_date)
        self.assertEqual(d_dm, dt.date(2026, 5, 16))

        d_iso, _ = _extract_date("2026-06-20 09:00 họp", self.ref_date)
        self.assertEqual(d_iso, dt.date(2026, 6, 20))

        d_textual, _ = _extract_date("ngày 25 tháng 5 đi du lịch", self.ref_date)
        self.assertEqual(d_textual, dt.date(2026, 5, 25))

    def test_time_ranges(self) -> None:
        st, et, dur, _ = _extract_time_and_duration("họp 9h - 11h")
        self.assertEqual(st, "09:00")
        self.assertEqual(et, "11:00")
        self.assertEqual(dur, 120)

        st2, et2, dur2, _ = _extract_time_and_duration("9h đến 11h30 họp")
        self.assertEqual(st2, "09:00")
        self.assertEqual(et2, "11:30")
        self.assertEqual(dur2, 150)

        st3, et3, dur3, _ = _extract_time_and_duration("từ 14h tới 16h30 hội thảo")
        self.assertEqual(st3, "14:00")
        self.assertEqual(et3, "16:30")
        self.assertEqual(dur3, 150)

    def test_time_with_duration(self) -> None:
        st, et, dur, _ = _extract_time_and_duration("9h trong 2 tiếng bảo vệ")
        self.assertEqual(st, "09:00")
        self.assertEqual(et, "11:00")
        self.assertEqual(dur, 120)

        st2, et2, dur2, _ = _extract_time_and_duration("14h kéo dài 1h30p seminar")
        self.assertEqual(st2, "14:00")
        self.assertEqual(et2, "15:30")
        self.assertEqual(dur2, 90)

        st3, et3, dur3, _ = _extract_time_and_duration("8h trong 45 phút kiểm tra")
        self.assertEqual(st3, "08:00")
        self.assertEqual(et3, "08:45")
        self.assertEqual(dur3, 45)

    def test_single_time_defaults_to_1h(self) -> None:
        st, et, dur, _ = _extract_time_and_duration("9h họp nhóm")
        self.assertEqual(st, "09:00")
        self.assertEqual(et, "10:00")
        self.assertEqual(dur, 60)

        st2, et2, dur2, _ = _extract_time_and_duration("2h chiều đi cafe")
        self.assertEqual(st2, "14:00")
        self.assertEqual(et2, "15:00")
        self.assertEqual(dur2, 60)

        st3, et3, dur3, _ = _extract_time_and_duration("8h tối xem phim")
        self.assertEqual(st3, "20:00")
        self.assertEqual(et3, "21:00")
        self.assertEqual(dur3, 60)

    def test_reminder_extraction(self) -> None:
        rem1, _ = _extract_reminder("mai 9h họp báo trước 1h")
        self.assertEqual(rem1, 60)

        rem2, _ = _extract_reminder("mai 9h họp nhắc trước 30p")
        self.assertEqual(rem2, 30)

        rem3, _ = _extract_reminder("mai 9h họp remind 2 tiếng")
        self.assertEqual(rem3, 120)

        rem_default, _ = _extract_reminder("mai 9h họp")
        self.assertEqual(rem_default, DEFAULT_REMINDER_MINUTES)

    def test_location_extraction(self) -> None:
        loc1, _ = _extract_location("họp nhóm ở B402")
        self.assertEqual(loc1, "B402")

        loc2, _ = _extract_location("gặp đối tác tại Highland Coffee")
        self.assertEqual(loc2, "Highland Coffee")

        loc3, _ = _extract_location("học nhóm @Thư viện")
        self.assertEqual(loc3, "Thư viện")

        loc4, _ = _extract_location("sinh hoạt lớp phòng A101")
        self.assertEqual(loc4, "Phòng A101")

    def test_full_parse_quick_add(self) -> None:
        text = "/add Mai 9h đến 11h họp nhóm đồ án ở B402 báo trước 1h"
        res = parse_quick_add(text, reference_date=self.ref_date)
        self.assertTrue(res.is_complete)
        self.assertEqual(res.appointment_date, dt.date(2026, 5, 14))
        self.assertEqual(res.start_time, "09:00")
        self.assertEqual(res.end_time, "11:00")
        self.assertEqual(res.duration_minutes, 120)
        self.assertEqual(res.title, "họp nhóm đồ án")
        self.assertEqual(res.location, "B402")
        self.assertEqual(res.reminder_minutes, 60)
        self.assertEqual(res.missing_fields, ())

    def test_duration_syntax(self) -> None:
        text = "/add Thứ 6 14h trong 2 tiếng bảo vệ môn học tại Lab 3"
        res = parse_quick_add(text, reference_date=self.ref_date)
        self.assertTrue(res.is_complete)
        self.assertEqual(res.appointment_date, dt.date(2026, 5, 15))
        self.assertEqual(res.start_time, "14:00")
        self.assertEqual(res.end_time, "16:00")
        self.assertEqual(res.duration_minutes, 120)
        self.assertEqual(res.title, "bảo vệ môn học")
        self.assertEqual(res.location, "Lab 3")
        self.assertEqual(res.reminder_minutes, 60)

    def test_missing_fields_detection(self) -> None:
        # Missing date and time
        res1 = parse_quick_add("/add Họp nhóm ở B402", reference_date=self.ref_date)
        self.assertFalse(res1.is_complete)
        self.assertIn("date", res1.missing_fields)
        self.assertIn("start_time", res1.missing_fields)
        self.assertEqual(res1.title, "Họp nhóm")
        self.assertEqual(res1.location, "B402")

        # Missing time
        res2 = parse_quick_add("/add mai đi phỏng vấn tại FPT", reference_date=self.ref_date)
        self.assertFalse(res2.is_complete)
        self.assertEqual(res2.missing_fields, ("start_time",))
        self.assertEqual(res2.appointment_date, dt.date(2026, 5, 14))
        self.assertEqual(res2.title, "đi phỏng vấn")
        self.assertEqual(res2.location, "FPT")

        # Missing title
        res3 = parse_quick_add("/add mai 9h", reference_date=self.ref_date)
        self.assertFalse(res3.is_complete)
        self.assertEqual(res3.missing_fields, ("job",))
        self.assertEqual(res3.appointment_date, dt.date(2026, 5, 14))
        self.assertEqual(res3.start_time, "09:00")


if __name__ == "__main__":
    unittest.main()
