"""
Generic ASP.NET WebForms state management and postback helper.
"""

import logging
from typing import Any
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

from tdtu.exceptions import TDTUAuthenticationError, TDTUProtocolError

logger = logging.getLogger(__name__)


def sanitize_url(url: Any) -> str:
    """Import sanitize_url helper from tdtu.client."""
    from tdtu.client import sanitize_url as _san
    return _san(url)


def extract_hidden_fields(html: str) -> dict[str, str]:
    """
    Extract all ASP.NET hidden form fields from HTML string.
    Includes __VIEWSTATE, __VIEWSTATEGENERATOR, __EVENTVALIDATION, etc.
    """
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    for node in soup.select('input[type="hidden"][name]'):
        name = node.get("name")
        if name:
            fields[name] = node.get("value", "")
    return fields


class WebFormsPage:
    """
    Wrapper around an active WebForms page that tracks state across postbacks.
    Every postback uses the state returned by the PREVIOUS response.
    """

    def __init__(
        self,
        session: requests.Session,
        url: str,
        html: str,
        timeout: int = 15,
    ) -> None:
        self.session = session
        self.url = url
        self.html = html
        self.timeout = timeout

    def hidden_fields(self) -> dict[str, str]:
        """Extract hidden fields from the current response HTML."""
        return extract_hidden_fields(self.html)

    def postback(
        self,
        event_target: str = "",
        event_argument: str = "",
        extra: dict[str, Any] | None = None,
        action_url: str | None = None,
    ) -> requests.Response:
        """
        Execute an ASP.NET postback with current WebForms state.
        Updates self.html and self.url with the new response content.
        Validates postback response semantically and structurally.
        """
        from tdtu.client import ALLOWED_HOSTS, safe_request

        payload = self.hidden_fields()
        payload["__EVENTTARGET"] = event_target
        payload["__EVENTARGUMENT"] = event_argument

        if extra:
            payload.update(extra)

        target_url = action_url or self.url

        # Validate target_url before posting (Bug 22)
        parsed_target = urlparse(target_url)
        if parsed_target.scheme != "https":
            raise TDTUProtocolError(f"Insecure non-HTTPS target URL for postback: {target_url}")
        if (parsed_target.hostname or "") not in ALLOWED_HOSTS:
            raise TDTUProtocolError(f"Untrusted host in postback target URL: {parsed_target.hostname}")

        try:
            resp = safe_request(
                self.session,
                "POST",
                target_url,
                data=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise TDTUProtocolError(
                f"Postback request to {sanitize_url(target_url)} failed: {sanitize_url(exc)}"
            ) from exc

        # Semantic validation of postback response
        if "login" in resp.url.lower() or "đăng nhập" in resp.text[:500].lower():
            raise TDTUAuthenticationError("Postback response redirected to login (session expired)")

        parsed_url = urlparse(resp.url)
        if (parsed_url.hostname or "") not in ALLOWED_HOSTS:
            raise TDTUProtocolError(f"Postback redirected to unexpected host: {parsed_url.hostname}")

        if "__VIEWSTATE" not in resp.text:
            raise TDTUProtocolError("Postback response missing ASP.NET __VIEWSTATE")

        self.url = resp.url
        self.html = resp.text
        return resp
