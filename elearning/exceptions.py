"""Typed exception hierarchy for TDTU eLearning API client."""


class ElearningError(Exception):
    """Base exception for all eLearning errors."""


class ElearningAuthError(ElearningError):
    """Raised when HTTP login or sesskey extraction fails."""


class ElearningApiError(ElearningError):
    """Raised when Moodle AJAX service returns an HTTP error or error payload."""


class ElearningPaginationError(ElearningError):
    """Raised when pagination invariants are violated (missing cursor, stall, loop)."""


class ElearningResponseError(ElearningError):
    """Raised when API payload is malformed or missing required schema fields."""
