import os
import tempfile
import unittest
from pathlib import Path

import test_deadline_crawler


class DeadlineSampleTests(unittest.TestCase):
    def test_load_dotenv_file_populates_missing_credentials(self) -> None:
        old_student = os.environ.pop("STUDENT_ID", None)
        old_password = os.environ.pop("PASSWORD", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                env_file = Path(tmpdir) / ".env"
                env_file.write_text("STUDENT_ID=demo\nPASSWORD=secret\n", encoding="utf-8")

                test_deadline_crawler._load_dotenv_file(env_file)

                self.assertEqual(os.environ.get("STUDENT_ID"), "demo")
                self.assertEqual(os.environ.get("PASSWORD"), "secret")
        finally:
            os.environ.pop("STUDENT_ID", None)
            os.environ.pop("PASSWORD", None)
            if old_student is not None:
                os.environ["STUDENT_ID"] = old_student
            if old_password is not None:
                os.environ["PASSWORD"] = old_password


if __name__ == "__main__":
    unittest.main()
