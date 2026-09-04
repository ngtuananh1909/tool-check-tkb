"""
Custom exceptions for the TDTU student portal HTTP client and parsers.
"""


class TDTUError(Exception):
    """Base exception for all TDTU portal crawler errors."""

    pass


class TDTUAuthenticationError(TDTUError):
    """Raised when authentication fails (e.g. invalid credentials, unexpected response)."""

    pass


class TDTUProtocolError(TDTUError):
    """Raised when WebForms or HTTP redirection protocol fails unexpectedly."""

    pass


class TDTUParsingError(TDTUError):
    """Raised when HTML DOM structure cannot be parsed or expected elements are missing."""

    pass
