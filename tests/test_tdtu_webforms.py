"""
Unit tests for WebForms hidden field extraction and state tracking across postbacks.
"""

import unittest
from unittest.mock import MagicMock

import requests

from tdtu.webforms import WebFormsPage, extract_hidden_fields


class TestTDTUWebForms(unittest.TestCase):

    def test_extract_hidden_fields(self) -> None:
        html = """
        <html>
            <input type="hidden" name="__VIEWSTATE" value="STATE1" />
            <input type="hidden" name="__VIEWSTATEGENERATOR" value="GEN1" />
            <input type="hidden" name="__EVENTVALIDATION" value="VAL1" />
            <input type="hidden" name="custom_field" value="123" />
            <input type="text" name="visible" value="ignore" />
        </html>
        """
        fields = extract_hidden_fields(html)
        self.assertEqual(fields["__VIEWSTATE"], "STATE1")
        self.assertEqual(fields["__VIEWSTATEGENERATOR"], "GEN1")
        self.assertEqual(fields["__EVENTVALIDATION"], "VAL1")
        self.assertEqual(fields["custom_field"], "123")
        self.assertNotIn("visible", fields)

    def test_postback_state_sequence(self) -> None:
        mock_sess = MagicMock()

        html_initial = '<html><input type="hidden" name="__VIEWSTATE" value="STATE_A" /></html>'
        html_resp_1 = '<html><input type="hidden" name="__VIEWSTATE" value="STATE_B" /></html>'
        html_resp_2 = '<html><input type="hidden" name="__VIEWSTATE" value="STATE_C" /></html>'

        r1 = MagicMock()
        r1.status_code = 200
        r1.url = "http://example.com/page"
        r1.text = html_resp_1

        r2 = MagicMock()
        r2.status_code = 200
        r2.url = "http://example.com/page"
        r2.text = html_resp_2

        mock_sess.post.side_effect = [r1, r2]

        page = WebFormsPage(session=mock_sess, url="http://example.com/page", html=html_initial)

        # POST #1: Should use STATE_A
        page.postback(event_target="Btn1")
        post_data_1 = mock_sess.post.call_args_list[0][1]["data"]
        self.assertEqual(post_data_1["__VIEWSTATE"], "STATE_A")

        # Page state should now be updated to STATE_B
        self.assertEqual(page.hidden_fields()["__VIEWSTATE"], "STATE_B")

        # POST #2: Must use STATE_B (returned by POST #1), NOT stale STATE_A
        page.postback(event_target="Btn2")
        post_data_2 = mock_sess.post.call_args_list[1][1]["data"]
        self.assertEqual(post_data_2["__VIEWSTATE"], "STATE_B")


if __name__ == "__main__":
    unittest.main()
