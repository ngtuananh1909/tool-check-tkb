"""TDTU eLearning API package."""

from elearning.client import DeadlineCrawlResult, ElearningClient
from elearning.exceptions import (
    ElearningApiError,
    ElearningAuthError,
    ElearningError,
    ElearningPaginationError,
    ElearningResponseError,
)

__all__ = [
    "DeadlineCrawlResult",
    "ElearningClient",
    "ElearningError",
    "ElearningAuthError",
    "ElearningApiError",
    "ElearningPaginationError",
    "ElearningResponseError",
]
