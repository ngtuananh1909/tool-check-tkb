import json
import os
from pathlib import Path

from crawler import fetch_elearning_deadlines


def _load_dotenv_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


if __name__ == "__main__":
    _load_dotenv_file()
    rows = fetch_elearning_deadlines()
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"Nearest incomplete deadlines: {len(rows)}")
