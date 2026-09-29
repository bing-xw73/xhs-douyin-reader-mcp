import json
import socket
import unittest
from unittest import mock

import xhs_reader as reader

NOTE_ID = "6a775c840000000024027ffb"
URL = "https://www.xiaohongshu.com/explore/" + NOTE_ID
NOTE = {
    "noteId": NOTE_ID,
    "title": "测试标题",
    "desc": "#英语[话题]# 正文",
    "user": {"nickName": "示例作者"},
    "type": "normal",
    "time": 1786207364000,
    "tagList": [{"name": "英语"}],
    "interactInfo": {"likedCount": "1,234", "commentCount": "1.2万"},
    "imageList": [{"url": "https://example.invalid/image.jpg"}],
}


def page(note=NOTE):
    state = {"noteData": {"data": {"noteData": note}}}
    return "<script>window.__INITIAL_STATE__=" + json.dumps(state, ensure_ascii=False) + ";</script>"


class XhsParserTests(unittest.TestCase):
    def test_mobile_state(self):
        result = reader.parse_xhs_page(page(), URL)
        self.assertEqual(result["author"], "示例作者")
        self.assertEqual(result["body"], "#英语 正文")
        self.assertEqual(result["interactions"]["likes"], 1234)
        self.assertEqual(result["interactions"]["comments"], "1.2万")
        self.assertEqual(result["published_at"], "2026-08-09T00:42:44+08:00")

    def test_desktop_map_and_id_match(self):
        state = {"note": {"noteDetailMap": {NOTE_ID: {"note": NOTE}}}}
        html = "<script>window.__INITIAL_STATE__=" + json.dumps(state) + ";</script>"
        self.assertEqual(reader.parse_xhs_page(html, URL)["title"], "测试标题")
        wrong = URL.replace(NOTE_ID, "a" * 24)
        with self.assertRaises(reader.ReaderError):
            reader.parse_xhs_page(html, wrong)

    def test_bare_undefined_only(self):
        html = '<script>window.__INITIAL_STATE__={"word":"undefined", "missing":undefined};</script>'
        self.assertEqual(reader.extract_initial_state(html), {"word": "undefined", "missing": None})

    def test_login_and_risk_pages(self):
        for html, code in (("请登录后查看", "LOGIN_REQUIRED"), ("请完成安全验证", "CAPTCHA_OR_RISK_CONTROL")):
            with self.subTest(code=code), self.assertRaises(reader.ReaderError) as context:
                reader.parse_xhs_page(html, URL)
            self.assertEqual(context.exception.code, code)

    def test_url_and_ssrf_rejections(self):
        for value in (
            "file:///etc/passwd",
            "http://" + ".".join(("127", "0", "0", "1")) + "/",
            "https://xiaohongshu.com.evil.invalid/",
            "https://xiaohongshu.com@evil.invalid/",
            "https://xiaohongshu.com:8443/",
        ):
            with self.subTest(value=value), self.assertRaises(reader.ReaderError):
                reader.validate_url(value)
        loopback = ".".join(("127", "0", "0", "1"))
        with mock.patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", (loopback, 443))]):
            with self.assertRaises(reader.ReaderError):
                reader.public_addresses("www.xiaohongshu.com")

    def test_all_network_attempts_fail_cleanly(self):
        fetch = mock.Mock(side_effect=TimeoutError("timeout"))
        result = reader.XhsReader(fetch=fetch).read(URL)
        self.assertEqual(result["error"]["code"], "NETWORK_ERROR")
        self.assertEqual(fetch.call_count, 2)

    def test_cache_and_rate_limit(self):
        clock = [0.0]
        fetch = mock.Mock(return_value=(200, URL, page()))
        instance = reader.XhsReader(fetch=fetch, clock=lambda: clock[0])
        for index in range(5):
            value = instance.read(URL)
            self.assertTrue(value["ok"])
            self.assertEqual(value["cached"], index > 0)
        self.assertEqual(instance.read(URL)["error"]["code"], "RATE_LIMITED")


if __name__ == "__main__":
    unittest.main()
