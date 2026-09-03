"""
Generic ASP.NET WebForms state management and postback helper.
"""

from typing import Any
import requests
from bs4 import BeautifulSoup

from tdtu.exceptions import TDTUProtocolError


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
        """
        payload = self.hidden_fields()
        payload["__EVENTTARGET"] = event_target
        payload["__EVENTARGUMENT"] = event_argument

        if extra:
            payload.update(extra)

        target_url = action_url or self.url

        try:
            resp = self.session.post(
                target_url,
                data=payload,
                allow_redirects=True,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise TDTUProtocolError(f"Postback request to {target_url} failed: {exc}") from exc

        self.url = resp.url
        self.html = resp.text
        return resp
