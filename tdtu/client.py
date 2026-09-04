"""
Authenticated TDTU Student Portal HTTP Client.
Handles login lifecycle, cookie management, URL sanitization, and navigation.
"""

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from tdtu.exceptions import TDTUAuthenticationError, TDTUProtocolError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
PORTAL_LOGIN_URL = "https://old-stdportal.tdtu.edu.vn/Login/"
PORTAL_SIGNIN_URL = "https://old-stdportal.tdtu.edu.vn/Login/SignIn"
SCHEDULE_BASE_URL = "https://lichhoc-lichthi.tdtu.edu.vn/tkb2.aspx"
EXAM_BASE_URL = "https://lichhoc-lichthi.tdtu.edu.vn/xemlichthi.aspx"
ALLOWED_HOSTS = {
    "old-stdportal.tdtu.edu.vn",
    "lichhoc-lichthi.tdtu.edu.vn",
    "sso.tdtu.edu.vn",
    "stdportal.tdtu.edu.vn",
}


def sanitize_url(url: Any) -> str:
    """Redact sensitive query parameters, tokens, and cookies from URLs and messages."""
    url_str = str(url or "")
    # Redact Token, RequestId, SessionId
    sanitized = re.sub(
        r"([?&](?:token|requestid|sessionid|asp.net_sessionid)=)[^&]+",
        r"\1[REDACTED]",
        url_str,
        flags=re.IGNORECASE,
    )
    # Redact raw passwords or credentials if present in string
    sanitized = re.sub(r'(pass(?:word)?=["\']?)[^"\'&\s]+', r'\1[REDACTED]', sanitized, flags=re.IGNORECASE)
    return sanitized


class TDTUClient:
    """
    Authenticated session manager for TDTU student portal.
    Uses a single requests.Session for the entire lifecycle.
    """

    def __init__(
        self,
        student_id: str,
        password: str,
        timeout: int = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.student_id = student_id
        self.password = password
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }
        )
        self.is_logged_in = False
        self.token: str | None = None
        self.request_id: str | None = None
        self.authenticated_schedule_url: str | None = None

    def __enter__(self) -> "TDTUClient":
        self.login()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    def login(self) -> None:
        """
        Authenticate against TDTU student portal.
        Follows the verified protocol:
        1. GET login page for initial cookies.
        2. POST credentials to /Login/SignIn.
        3. Extract redirect URL from JSON result.
        4. Validate domain and GET redirect URL to obtain Token and RequestId.
        """
        if self.is_logged_in:
            return

        logger.info("[tdtu.auth] Starting login for student ID: %s", self.student_id)

        try:
            # 1. Establish initial cookies
            r1 = self.session.get(PORTAL_LOGIN_URL, timeout=self.timeout)
            r1.raise_for_status()

            # 2. POST login credentials
            r2 = self.session.post(
                PORTAL_SIGNIN_URL,
                data={"user": self.student_id, "pass": self.password},
                allow_redirects=False,
                timeout=self.timeout,
            )
            r2.raise_for_status()

            # 3. Parse JSON response
            try:
                data = r2.json()
            except Exception as exc:
                raise TDTUAuthenticationError(
                    f"Login endpoint returned non-JSON response: {r2.text[:100]}"
                ) from exc

            if not isinstance(data, dict):
                raise TDTUAuthenticationError("Login JSON response is not a dict")

            result_val = str(data.get("result", "")).strip()
            url_val = str(data.get("url", "")).strip()

            if result_val in ("fail", "T", "*", "chualamcamketgiaothong", "chualamcamketmatuy", "chuathuchienshcd"):
                raise TDTUAuthenticationError(f"Login failed with code: {result_val}")

            redirect_url = url_val or result_val
            if not redirect_url or redirect_url.lower() in ("success", "true", "1", "ok"):
                redirect_url = r2.headers.get("Location") or "https://old-stdportal.tdtu.edu.vn/StdPortalMain"

            # Format relative redirect URLs if returned as relative path
            if redirect_url.startswith("/"):
                redirect_url = f"https://old-stdportal.tdtu.edu.vn{redirect_url}"
            elif redirect_url.startswith("./"):
                redirect_url = f"https://old-stdportal.tdtu.edu.vn/Login/{redirect_url[2:]}"
            elif not redirect_url.startswith("http"):
                redirect_url = f"https://old-stdportal.tdtu.edu.vn/{redirect_url.lstrip('/')}"

            # 4. Strict Domain and HTTPS Security Check
            parsed_redirect = urlparse(redirect_url)
            if parsed_redirect.scheme != "https":
                raise TDTUAuthenticationError(f"Insecure non-HTTPS redirect scheme: {parsed_redirect.scheme}")

            hostname = parsed_redirect.hostname or ""
            if hostname not in ALLOWED_HOSTS:
                raise TDTUAuthenticationError(
                    f"Untrusted redirect domain in login response: {hostname}"
                )

            # 5. Follow redirect to establish session on lichhoc-lichthi domain
            logger.debug("[tdtu.auth] Following authenticated redirect to %s", sanitize_url(redirect_url))
            r3 = self.session.get(redirect_url, allow_redirects=True, timeout=self.timeout)
            r3.raise_for_status()

            # Verify final destination host
            final_parsed = urlparse(r3.url)
            if final_parsed.hostname not in ALLOWED_HOSTS:
                raise TDTUAuthenticationError(
                    f"Final redirect landed on untrusted host: {final_parsed.hostname}"
                )

            # Extract Token and RequestId from final URL or history
            params = parse_qs(parsed_redirect.query)
            if not params:
                params = parse_qs(final_parsed.query)

            self.token = (params.get("Token") or params.get("token") or [""])[0]
            self.request_id = (params.get("RequestId") or params.get("requestid") or [""])[0]

            self.authenticated_schedule_url = r3.url
            self.is_logged_in = True
            logger.info("[tdtu.auth] Authenticated successfully.")

        except requests.RequestException as exc:
            raise TDTUAuthenticationError(f"Network error during authentication: {sanitize_url(exc)}") from exc

    def open_schedule_page(self) -> Any:
        """Fetch schedule page (tkb2.aspx) and return a WebFormsPage instance."""
        from tdtu.webforms import WebFormsPage

        if not self.is_logged_in:
            self.login()

        url = SCHEDULE_BASE_URL
        if self.token and self.request_id:
            url = f"{SCHEDULE_BASE_URL}?Token={self.token}&RequestId={self.request_id}"

        logger.debug("[tdtu.schedule] Fetching schedule page: %s", sanitize_url(url))
        try:
            resp = self.session.get(url, allow_redirects=True, timeout=self.timeout)
            resp.raise_for_status()

            # Semantic validation: check for login redirect or missing elements
            if "login" in resp.url.lower() or "đăng nhập" in resp.text[:500].lower():
                raise TDTUAuthenticationError("Schedule page redirected to login (session expired)")

            if "__VIEWSTATE" not in resp.text:
                raise TDTUProtocolError("Schedule page missing ASP.NET __VIEWSTATE")

            return WebFormsPage(session=self.session, url=resp.url, html=resp.text, timeout=self.timeout)

        except requests.RequestException as exc:
            raise TDTUProtocolError(f"Failed to fetch schedule page: {sanitize_url(exc)}") from exc

    def open_exam_page(self) -> Any:
        """Fetch exam page (xemlichthi.aspx) and return a WebFormsPage instance."""
        from tdtu.webforms import WebFormsPage

        if not self.is_logged_in:
            self.login()

        url = EXAM_BASE_URL
        if self.token and self.request_id:
            url = f"{EXAM_BASE_URL}?Token={self.token}&RequestId={self.request_id}"

        logger.debug("[tdtu.exams] Fetching exam page: %s", sanitize_url(url))
        try:
            resp = self.session.get(url, allow_redirects=True, timeout=self.timeout)
            resp.raise_for_status()

            if "login" in resp.url.lower() or "đăng nhập" in resp.text[:500].lower():
                raise TDTUAuthenticationError("Exam page redirected to login (session expired)")

            if "__VIEWSTATE" not in resp.text:
                raise TDTUProtocolError("Exam page missing ASP.NET __VIEWSTATE")

            return WebFormsPage(session=self.session, url=resp.url, html=resp.text, timeout=self.timeout)

        except requests.RequestException as exc:
            raise TDTUProtocolError(f"Failed to fetch exam page: {sanitize_url(exc)}") from exc
