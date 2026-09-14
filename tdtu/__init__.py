"""
TDTU Student Portal Package.
Provides authenticated HTTP crawling, WebForms state management, and HTML parsing.
"""

from tdtu.client import TDTUClient, sanitize_url
from tdtu.exams.service import fetch_exam_schedule_http
from tdtu.exceptions import (
    TDTUAuthenticationError,
    TDTUError,
    TDTUParsingError,
    TDTUProtocolError,
)
from tdtu.schedule.service import fetch_schedule_http, get_current_semester_http
from tdtu.snapshot import FetchResult, PortalSnapshot, fetch_portal_snapshot

__all__ = [
    "FetchResult",
    "PortalSnapshot",
    "TDTUAuthenticationError",
    "TDTUClient",
    "TDTUError",
    "TDTUParsingError",
    "TDTUProtocolError",
    "fetch_exam_schedule_http",
    "fetch_portal_snapshot",
    "fetch_schedule_http",
    "get_current_semester_http",
    "sanitize_url",
]
