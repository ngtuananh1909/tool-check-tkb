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
    "sso.tdt.edu.vn",
    "stdportal.tdtu.edu.vn",
    "stdportal.tdt.edu.vn",
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


def safe_request(
    session: requests.Session,
    method: str,
    url: str,
    data: Any = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_redirects: int = 5,
) -> requests.Response:
    """
    Execute HTTP request with strict step-by-step redirect validation.
    Ensures every redirect destination uses HTTPS and stays within ALLOWED_HOSTS (Bug 21).
    """
    current_url = url
    current_method = method.upper()
    current_data = data

    for _ in range(max_redirects + 1):
        parsed = urlparse(current_url)
        if parsed.scheme != "https":
            raise TDTUAuthenticationError(f"Insecure non-HTTPS scheme: {parsed.scheme}")
        if (parsed.hostname or "") not in ALLOWED_HOSTS:
            raise TDTUAuthenticationError(f"Untrusted host in request/redirect: {parsed.hostname}")

        resp = session.request(
            method=current_method,
            url=current_url,
            data=current_data,
            headers=headers,
            allow_redirects=False,
            timeout=timeout,
        )

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                return resp

            base_parsed = urlparse(current_url)
            loc_parsed = urlparse(location)

            query = loc_parsed.query
            if not query and base_parsed.query:
                query = base_parsed.query

            if location.startswith("/"):
                path_part = loc_parsed.path
            elif location.startswith("./"):
                base_path = base_parsed.path.rsplit("/", 1)[0]
                path_part = f"{base_path}/{loc_parsed.path[2:]}"
            elif not location.startswith("http"):
                path_part = f"/{loc_parsed.path.lstrip('/')}"
            else:
                path_part = None

            if path_part is not None:
                current_url = f"https://{base_parsed.netloc}{path_part}"
                if query:
                    current_url += f"?{query}"
            else:
                current_url = location

            if resp.status_code in (301, 302, 303):
                current_method = "GET"
                current_data = None
            continue

        return resp

    raise TDTUProtocolError("Too many redirects during authenticated navigation")


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
        self.token: str = ""
        self.request_id: str = ""
        self.authenticated_schedule_url: str = ""
        self.is_logged_in: bool = False

    def __enter__(self) -> "TDTUClient":
        self.login()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying requests Session."""
        self.session.close()

    def login(self) -> None:
        """
        Authenticate with TDTU student portal.
        Performs 2-step login: GET /Login/ -> POST /Login/SignIn -> Follow redirect URL.
        Captures Token and RequestId parameters for subsequent API postbacks.
        """
        try:
            # 1. Open login page to acquire initial cookies / session
            r1 = safe_request(self.session, "GET", PORTAL_LOGIN_URL, timeout=self.timeout)
            r1.raise_for_status()

            # 2. Submit credentials to SignIn API endpoint
            payload = {
                "user": self.student_id,
                "pass": self.password,
            }
            r2 = self.session.post(
                PORTAL_SIGNIN_URL,
                data=payload,
                headers={"Referer": PORTAL_LOGIN_URL},
                timeout=self.timeout,
            )
            r2.raise_for_status()

            # 3. Parse JSON response
            try:
                data = r2.json()
            except ValueError as exc:
                raise TDTUAuthenticationError(
                    f"SignIn response is not valid JSON: {r2.text[:100]}"
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

            # 5. Follow redirect step-by-step with domain validation
            logger.debug("[tdtu.auth] Following authenticated redirect to %s", sanitize_url(redirect_url))
            r3 = safe_request(self.session, "GET", redirect_url, timeout=self.timeout)
            r3.raise_for_status()

            # Verify final destination host
            final_parsed = urlparse(r3.url)
            if final_parsed.hostname not in ALLOWED_HOSTS:
                raise TDTUAuthenticationError(
                    f"Final redirect landed on untrusted host: {final_parsed.hostname}"
                )

            # Extract Token and RequestId from final URL or redirect URL
            params = parse_qs(final_parsed.query)
            for k, v in parse_qs(parsed_redirect.query).items():
                if k not in params:
                    params[k] = v

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
        if self.token:
            url = f"https://sso.tdtu.edu.vn/Authenticate.aspx?ReturnUrl={SCHEDULE_BASE_URL}&Token={self.token}"

        logger.debug("[tdtu.schedule] Fetching schedule page: %s", sanitize_url(url))
        try:
            resp = safe_request(self.session, "GET", url, timeout=self.timeout)
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
        if self.token:
            url = f"https://sso.tdtu.edu.vn/Authenticate.aspx?ReturnUrl={EXAM_BASE_URL}&Token={self.token}"

        logger.debug("[tdtu.exams] Fetching exam page: %s", sanitize_url(url))
        try:
            resp = safe_request(self.session, "GET", url, timeout=self.timeout)
            resp.raise_for_status()

            if "login" in resp.url.lower() or "đăng nhập" in resp.text[:500].lower():
                raise TDTUAuthenticationError("Exam page redirected to login (session expired)")

            if "__VIEWSTATE" not in resp.text:
                raise TDTUProtocolError("Exam page missing ASP.NET __VIEWSTATE")

            return WebFormsPage(session=self.session, url=resp.url, html=resp.text, timeout=self.timeout)

        except requests.RequestException as exc:
            raise TDTUProtocolError(f"Failed to fetch exam page: {sanitize_url(exc)}") from exc
