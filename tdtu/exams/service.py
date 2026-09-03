"""
Exam service for orchestrating HTTP exam schedule retrieval across tab postbacks.
"""

import logging
from typing import Any

from tdtu.client import TDTUClient
from tdtu.exams.parser import deduplicate_exam_rows, parse_exam_html

logger = logging.getLogger(__name__)


def fetch_exam_schedule_http(
    client: TDTUClient,
    selected_semester: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch exam schedule entries across Midterm, Final, and Final 2nd attempt tabs.
    Executes sequential postbacks using updated WebForms state after each request.
    """
    page = client.open_exam_page()
    all_exams: list[dict[str, Any]] = []

    # 1. Parse initial page content (usually Final term default tab)
    initial_exams = parse_exam_html(page.html, default_exam_type="Cuối kỳ")
    all_exams.extend(initial_exams)

    # Tab configurations: (argument, label)
    tabs = [
        ("0", "Giữa kỳ"),
        ("1", "Cuối kỳ"),
        ("2", "Cuối kỳ lần 2"),
    ]

    for arg, label in tabs:
        if "LichThi1$Menu1" not in page.html:
            logger.debug("[tdtu.exams] Menu control not present on exam page")
            break

        logger.debug("[tdtu.exams] Executing postback for tab '%s' (arg=%s)...", label, arg)
        try:
            page.postback(
                event_target="LichThi1$Menu1",
                event_argument=arg,
            )
            tab_exams = parse_exam_html(page.html, default_exam_type=label)
            all_exams.extend(tab_exams)
        except Exception as exc:
            logger.warning("[tdtu.exams] Failed postback for exam tab '%s': %s", label, exc)

    deduped = deduplicate_exam_rows(all_exams)
    logger.info("[tdtu.exams] Fetched %d exam entries via HTTP", len(deduped))
    return deduped
