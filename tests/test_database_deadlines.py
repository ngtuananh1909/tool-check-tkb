import unittest
from unittest.mock import patch

import database


class _FakeExecute:
    def execute(self):
        return None


class _FakeTable:
    def __init__(self):
        self.payload_rows = None
        self.on_conflict = None

    def upsert(self, payload_rows, on_conflict=None):
        self.payload_rows = payload_rows
        self.on_conflict = on_conflict
        return _FakeExecute()


class _FakeClient:
    def __init__(self):
        self.deadlines_table = _FakeTable()

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


if __name__ == "__main__":
    unittest.main()
