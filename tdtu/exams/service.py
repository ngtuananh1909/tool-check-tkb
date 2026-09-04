"""
Exam service for orchestrating HTTP exam schedule retrieval across tab postbacks.
"""

import logging
import os
import re
from typing import Any

from tdtu.client import TDTUClient
from tdtu.exceptions import TDTUProtocolError
from tdtu.exams.parser import deduplicate_exam_rows, parse_exam_html

logger = logging.getLogger(__name__)


def _desired_exam_types() -> list[tuple[str, str]]:
    """
    Read desired exam types from TARGET_EXAM_TYPES environment variable.
    Returns list of tuples: (tab_argument, label).
    """
    raw = (os.environ.get("TARGET_EXAM_TYPES") or "midterm,final").strip()
    tabs: list[tuple[str, str]] = []
    
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    for token in tokens:
        if token in {"mid", "midterm", "giua", "giuaky", "giua_ky", "gk"}:
            if ("0", "Giữa kỳ") not in tabs:
                tabs.append(("0", "Giữa kỳ"))
        elif token in {"final", "cuoi", "cuoiky", "cuoi_ky", "ck"}:
            if ("1", "Cuối kỳ") not in tabs:
                tabs.append(("1", "Cuối kỳ"))
        elif token in {"final2", "cuoiky2", "cuoi_ky_2", "ck2"}:
            if ("2", "Cuối kỳ lần 2") not in tabs:
                tabs.append(("2", "Cuối kỳ lần 2"))

    return tabs or [("0", "Giữa kỳ"), ("1", "Cuối kỳ")]


def fetch_exam_schedule_http(
    client: TDTUClient,
    selected_semester: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch exam schedule entries for configured exam types.
    Optionally switches semester via LichThi1$cboHocKy.
    Executes tab postbacks sequentially using updated WebForms state.
    Raises TDTUProtocolError if a postback fails (Blocker 10).
    """
    page = client.open_exam_page()

    # 1. Semester selection if requested
    target_sem = selected_semester or os.environ.get("TARGET_SEMESTER")
    if target_sem:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(page.html, "html.parser")
        select = soup.find("select", id=re.compile(r".*cboHocKy.*", re.IGNORECASE))
        if select:
            opts = select.find_all("option")
            target_val = None
            for opt in opts:
                txt = opt.get_text().strip()
                val = opt.get("value", "").strip()
                if target_sem.lower() in txt.lower() or val == target_sem:
                    target_val = val
                    break
            if target_val:
                logger.info("[tdtu.exams] Switching exam semester to value=%s", target_val)
                page.postback(
                    event_target="LichThi1$cboHocKy",
                    extra={"LichThi1$cboHocKy": target_val},
                )
            else:
                raise TDTUProtocolError(f"Requested exam semester '{target_sem}' not found in dropdown")

    # 2. Iterate requested exam tabs
    desired_tabs = _desired_exam_types()
    all_exams: list[dict[str, Any]] = []

    for arg, label in desired_tabs:
        if "LichThi1$Menu1" not in page.html:
            logger.warning("[tdtu.exams] Menu control missing on exam page")
            break

        logger.debug("[tdtu.exams] Executing postback for exam tab '%s' (arg=%s)...", label, arg)
        try:
            page.postback(
                event_target="LichThi1$Menu1",
                event_argument=arg,
            )
            tab_exams = parse_exam_html(page.html, default_exam_type=label)
            all_exams.extend(tab_exams)
        except Exception as exc:
            # Do NOT swallow postback errors! Raise TDTUProtocolError so operations fail cleanly (Blocker 10)
            logger.error("[tdtu.exams] Tab postback failed for '%s': %s", label, exc)
            raise TDTUProtocolError(f"Exam tab postback failed for '{label}': {exc}") from exc

    deduped = deduplicate_exam_rows(all_exams)
    logger.info("[tdtu.exams] Fetched %d exam entries via HTTP", len(deduped))
    return deduped
