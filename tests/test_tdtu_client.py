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
        
        r2 = MagicMock()
        r2.status_code = 200
        r2.json.return_value = {
            "result": "https://lichhoc-lichthi.tdtu.edu.vn/tkb2.aspx?Token=MOCK_TOK&RequestId=MOCK_REQ"
        }

        r3 = MagicMock()
        r3.status_code = 200
        r3.url = "https://lichhoc-lichthi.tdtu.edu.vn/tkb2.aspx?Token=MOCK_TOK&RequestId=MOCK_REQ"
        r3.text = '<html><input type="hidden" name="__VIEWSTATE" value="VS"/></html>'

        mock_sess.get.side_effect = [r1, r3]
        mock_sess.post.return_value = r2

        client = TDTUClient("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET", session=mock_sess)
        client.login()

        self.assertTrue(client.is_logged_in)
        self.assertEqual(client.token, "MOCK_TOK")
        self.assertEqual(client.request_id, "MOCK_REQ")

    @patch("requests.Session")
    def test_login_failed_code(self, mock_session_cls: MagicMock) -> None:
        mock_sess = MagicMock()

        r1 = MagicMock()
        r1.status_code = 200
        
        r2 = MagicMock()
        r2.status_code = 200
        r2.json.return_value = {"result": "fail"}

        mock_sess.get.return_value = r1
        mock_sess.post.return_value = r2

        client = TDTUClient("TEST_STUDENT_001", "TEST_PASSWORD_WRONG", session=mock_sess)
        with self.assertRaises(TDTUAuthenticationError):
            client.login()

    @patch("requests.Session")
    def test_untrusted_domain_redirect(self, mock_session_cls: MagicMock) -> None:
        mock_sess = MagicMock()

        r1 = MagicMock()
        r1.status_code = 200
        
        r2 = MagicMock()
        r2.status_code = 200
        r2.json.return_value = {"result": "https://evil-site.com/steal-creds"}

        mock_sess.get.return_value = r1
        mock_sess.post.return_value = r2

        client = TDTUClient("TEST_STUDENT_001", "TEST_PASSWORD_NOT_A_SECRET", session=mock_sess)
        with self.assertRaises(TDTUAuthenticationError) as ctx:
            client.login()
        self.assertIn("Untrusted redirect domain", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
