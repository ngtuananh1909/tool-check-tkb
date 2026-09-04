"""
Unit tests for TDTUClient HTTP authentication and session handling.
Uses fake test credentials only.
"""

import unittest
from unittest.mock import MagicMock, patch

import requests

from tdtu.client import TDTUClient, sanitize_url
from tdtu.exceptions import TDTUAuthenticationError


class TestTDTUClient(unittest.TestCase):

    def test_sanitize_url(self) -> None:
        raw_url = "https://lichhoc-lichthi.tdtu.edu.vn/tkb2.aspx?Token=SECRET_TOKEN_123&RequestId=SECRET_REQ_456"
        sanitized = sanitize_url(raw_url)
        self.assertNotIn("SECRET_TOKEN_123", sanitized)
        self.assertNotIn("SECRET_REQ_456", sanitized)
        self.assertIn("Token=[REDACTED]", sanitized)
        self.assertIn("RequestId=[REDACTED]", sanitized)

    @patch("requests.Session")
    def test_login_success(self, mock_session_cls: MagicMock) -> None:
        mock_sess = MagicMock()
        mock_session_cls.return_value = mock_sess

        r1 = MagicMock()
        r1.status_code = 200
        r1.url = "https://old-stdportal.tdtu.edu.vn/Login/"
        
        r2 = MagicMock()
        r2.status_code = 200
        r2.url = "https://old-stdportal.tdtu.edu.vn/Login/SignIn"
        r2.json.return_value = {
            "result": "https://lichhoc-lichthi.tdtu.edu.vn/tkb2.aspx?Token=MOCK_TOK&RequestId=MOCK_REQ"
        }

        r3 = MagicMock()
        r3.status_code = 200
        r3.url = "https://lichhoc-lichthi.tdtu.edu.vn/tkb2.aspx?Token=MOCK_TOK&RequestId=MOCK_REQ"
        r3.text = '<html><input type="hidden" name="__VIEWSTATE" value="VS"/></html>'

        mock_sess.request.side_effect = [r1, r3]
        mock_sess.post.return_value = r2

        client = TDTUClient("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET", session=mock_sess)
        client.login()

        self.assertTrue(client.is_logged_in)
        self.assertEqual(client.token, "MOCK_TOK")
        self.assertEqual(client.request_id, "MOCK_REQ")
        post_kwargs = mock_sess.post.call_args[1]
        self.assertFalse(post_kwargs.get("allow_redirects", True))

    @patch("requests.Session")
    def test_login_failed_code(self, mock_session_cls: MagicMock) -> None:
        mock_sess = MagicMock()

        r1 = MagicMock()
        r1.status_code = 200
        r1.url = "https://old-stdportal.tdtu.edu.vn/Login/"
        
        r2 = MagicMock()
        r2.status_code = 200
        r2.url = "https://old-stdportal.tdtu.edu.vn/Login/SignIn"
        r2.json.return_value = {"result": "fail"}

        mock_sess.request.return_value = r1
        mock_sess.post.return_value = r2

        client = TDTUClient("TEST_STUDENT_001", "TEST_PASSWORD_WRONG", session=mock_sess)
        with self.assertRaises(TDTUAuthenticationError):
            client.login()

    @patch("requests.Session")
    def test_untrusted_domain_redirect(self, mock_session_cls: MagicMock) -> None:
        mock_sess = MagicMock()

        r1 = MagicMock()
        r1.status_code = 200
        r1.url = "https://old-stdportal.tdtu.edu.vn/Login/"
        
        r2 = MagicMock()
        r2.status_code = 200
        r2.url = "https://old-stdportal.tdtu.edu.vn/Login/SignIn"
        r2.json.return_value = {"result": "https://evil-site.com/steal-creds"}

        mock_sess.request.return_value = r1
        mock_sess.post.return_value = r2

        client = TDTUClient("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET", session=mock_sess)
        with self.assertRaises(TDTUAuthenticationError) as ctx:
            client.login()
        self.assertIn("Untrusted redirect domain", str(ctx.exception))

    def test_safe_request_http_downgrade_rejected(self) -> None:
        from tdtu.client import safe_request
        mock_sess = MagicMock()
        with self.assertRaises(TDTUAuthenticationError) as ctx:
            safe_request(mock_sess, "GET", "http://old-stdportal.tdtu.edu.vn/Login/")
        self.assertIn("non-HTTPS scheme", str(ctx.exception))
        mock_sess.request.assert_not_called()

    def test_safe_request_http_redirect_rejected(self) -> None:
        from tdtu.client import safe_request
        mock_sess = MagicMock()
        r1 = MagicMock()
        r1.status_code = 302
        r1.url = "https://old-stdportal.tdtu.edu.vn/Login/"
        r1.headers = {"Location": "http://old-stdportal.tdtu.edu.vn/Login/Unsafe"}
        mock_sess.request.return_value = r1

        with self.assertRaises(TDTUAuthenticationError) as ctx:
            safe_request(mock_sess, "GET", "https://old-stdportal.tdtu.edu.vn/Login/")
        self.assertIn("non-HTTPS scheme", str(ctx.exception))
        # Ensure initial HTTPS request was sent (call count == 1), but NO request was sent to the http:// redirect URL
        self.assertEqual(mock_sess.request.call_count, 1)

    def test_safe_request_untrusted_host_rejected(self) -> None:
        from tdtu.client import safe_request
        mock_sess = MagicMock()
        with self.assertRaises(TDTUAuthenticationError) as ctx:
            safe_request(mock_sess, "GET", "https://malicious.com/phishing")
        self.assertIn("Untrusted host", str(ctx.exception))
        mock_sess.request.assert_not_called()

    def test_safe_request_too_many_redirects(self) -> None:
        from tdtu.client import safe_request
        from tdtu.exceptions import TDTUProtocolError
        mock_sess = MagicMock()
        r = MagicMock()
        r.status_code = 302
        r.url = "https://old-stdportal.tdtu.edu.vn/Login/"
        r.headers = {"Location": "https://old-stdportal.tdtu.edu.vn/Login/"}
        mock_sess.request.return_value = r

        with self.assertRaises(TDTUProtocolError) as ctx:
            safe_request(mock_sess, "GET", "https://old-stdportal.tdtu.edu.vn/Login/", max_redirects=2)
        self.assertIn("Too many redirects", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
