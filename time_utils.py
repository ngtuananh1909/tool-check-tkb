"""Utilities for timezone-aware current date/time handling."""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"


def _resolve_timezone() -> ZoneInfo:
    timezone_name = os.environ.get("APP_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_now() -> dt.datetime:
    """Return current datetime in application timezone."""
    return dt.datetime.now(_resolve_timezone())


def local_today() -> dt.date:
    """Return current local date in application timezone."""
    return local_now().date()
