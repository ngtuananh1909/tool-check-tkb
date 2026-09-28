"""Guard against committing local configuration or recognizable credentials."""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIGS = {".mcp.json", ".opencode.json"}
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "Google API key": re.compile(rb"AIza[0-9A-Za-z_-]{35}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "Telegram bot token": re.compile(rb"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
}


def tracked_paths() -> list[Path]:
    result = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [ROOT / raw.decode() for raw in result.split(b"\0") if raw]


class RepositoryHygieneTests(unittest.TestCase):
    def test_local_config_and_secret_files_are_not_tracked(self) -> None:
        bad = []
        for path in tracked_paths():
            relative = path.relative_to(ROOT).as_posix()
            if path.name in LOCAL_CONFIGS or (
                path.name.startswith(".env") and path.name != ".env.example"
            ):
                bad.append(relative)
            if path.suffix.lower() in {".pem", ".p12", ".pfx", ".key"}:
                bad.append(relative)
            if path.suffix == ".json" and (
                "service-account" in path.name or path.name.startswith("credentials")
            ):
                bad.append(relative)
        self.assertEqual(bad, [], f"Local configuration or secret files tracked: {bad}")

    def test_tracked_files_have_no_recognizable_credentials(self) -> None:
        matches = []
        for path in tracked_paths():
            data = path.read_bytes()
            if b"\0" in data:
                continue
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(data):
                    matches.append((path.relative_to(ROOT).as_posix(), label))
        self.assertEqual(matches, [], f"Possible credentials in tracked files: {matches}")


if __name__ == "__main__":
    unittest.main()
