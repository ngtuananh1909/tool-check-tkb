"""TDTU eLearning crawler package."""

from elearning.crawler import (
    DeadlineCrawlResult,
    PlaywrightElearningCrawler,
    compute_crawl_window,
)
from elearning.exceptions import (
    ElearningAuthError,
    ElearningCrawlError,
    ElearningError,
)

__all__ = [
    "DeadlineCrawlResult",
    "ElearningAuthError",
    "ElearningCrawlError",
    "ElearningError",
    "PlaywrightElearningCrawler",
    "compute_crawl_window",
]
