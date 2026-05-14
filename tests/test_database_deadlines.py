import unittest
from unittest.mock import patch

import database


class _FakeExecute:
    def __init__(self, data=None):
        self.data = data

    def execute(self):
        return self


class _FakeSelectExecute:
    def __init__(self, data):
        self.data = data

    def eq(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def execute(self):
        return self


class _FakeTable:
    def __init__(self, select_rows=None):
        self.payload_rows = None
        self.on_conflict = None
        self.select_rows = select_rows or []

    def upsert(self, payload_rows, on_conflict=None):
        self.payload_rows = payload_rows
        self.on_conflict = on_conflict
        return _FakeExecute()

    def select(self, *args, **kwargs):
        return _FakeSelectExecute(self.select_rows)


class _FakeClient:
    def __init__(self, deadline_rows=None):
        self.deadlines_table = _FakeTable(deadline_rows)

    def table(self, table_name):
        self.table_name = table_name
        return self.deadlines_table


class ElearningDeadlineDatabaseTests(unittest.TestCase):
    def test_upsert_elearning_deadlines_keeps_multiple_activities_for_same_course(self) -> None:
        client = _FakeClient()
        rows = [
            {
                "course_id": "47728",
                "course_name": "Operating Systems",
                "activity_name": "Final Report",
                "due_date": "2026-05-20T00:00:00+07:00",
                "activity_url": "https://example.test/mod/assign/view.php?id=1",
                "source_signature": "deadline-1",
            },
            {
                "course_id": "47728",
                "course_name": "Operating Systems",
                "activity_name": "Quiz 4",
                "due_date": "2026-05-21T00:00:00+07:00",
                "activity_url": "https://example.test/mod/quiz/view.php?id=2",
                "source_signature": "deadline-2",
            },
        ]

        with patch.object(database, "_get_client", return_value=client):
            count = database.upsert_elearning_deadlines(rows, student_id="52500028")

        self.assertEqual(count, 2)
        self.assertEqual(client.table_name, database.ELEARNING_DEADLINES_TABLE)
        self.assertEqual(client.deadlines_table.on_conflict, "student_id,source_signature")
        self.assertEqual(len(client.deadlines_table.payload_rows), 2)
        self.assertEqual(
            [row["activity_name"] for row in client.deadlines_table.payload_rows],
            ["Final Report", "Quiz 4"],
        )

    def test_get_nearest_elearning_deadlines_merges_progress_by_course(self) -> None:
        client = _FakeClient(
            [
                {
                    "id": 1,
                    "course_id": "501032",
                    "course_name": "Đại số tuyến tính",
                    "activity_name": "Bài tập cuối kỳ",
                    "due_date": "2026-05-20T00:00:00+07:00",
                    "activity_url": None,
                    "completion_status": "incomplete",
                }
            ]
        )

        with patch.object(database, "_get_client", return_value=client), patch.object(
            database,
            "get_latest_elearning_progress",
            return_value=[
                {
                    "course_id": "501032",
                    "progress_percent": 75,
                    "lessons_completed": 15,
                    "lessons_total": 20,
                }
            ],
        ):
            rows = database.get_nearest_elearning_deadlines(student_id="52500028")

        self.assertEqual(rows[0]["progress_percent"], 75)
        self.assertEqual(rows[0]["lessons_completed"], 15)
        self.assertEqual(rows[0]["lessons_total"], 20)


if __name__ == "__main__":
    unittest.main()
