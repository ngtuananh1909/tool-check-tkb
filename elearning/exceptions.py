"""Typed exception hierarchy for TDTU eLearning Playwright crawler."""


class ElearningError(Exception):
    """Base exception for all eLearning errors."""


class ElearningAuthError(ElearningError):
    """Raised when HTTP login fails or session expires."""


class ElearningCrawlError(ElearningError):
    """Raised when crawling fails (e.g. malformed DOM, timeout, unsupported activity kind)."""
