import json
import time
import unittest
from unittest.mock import patch
from douyin_reader import DouyinReader, cookie_value, parse_item, post_url, safe_url, MEDIA_DOMAINS
from xhs_reader import ReaderError

URL = 'https://v.douyin.com/ipohcwlD9Xs/'
FINAL = 'https://www.iesdouyin.com/share/video/7687627430039012529/'
ITEM = {'aweme_id': '7687627430039012529', 'desc': '测试', 'author': {'nickname':'作者'},
        'video': {'duration':61100, 'play_addr': {'url_list':['https://aweme.snssdk.com/aweme/v1/playwm/?video_id=test']}}}
PAGE = '<script>window._ROUTER_DATA = ' + json.dumps({'loaderData': {'video_(id)/page': {'videoInfoRes': {'item_list':[ITEM]}}}}) + '</script>'

class ReaderTests(unittest.TestCase):
    def test_url_allowlist(self):
        self.assertEqual(post_url(URL), URL)
        for url in ['https://evil.com/video/1234567890', 'https://douyin.com.evil.com/video/1234567890',
                    'https://user@v.douyin.com/abcdef/', 'https://douyin.com:8443/video/1234567890',
                    'https://v.douyin.com/abc\\def/', 'https://douyin.com/admin']:
            with self.subTest(url=url), self.assertRaises(ReaderError):
                post_url(url)

    def test_cdn_allowlist(self):
        self.assertEqual(safe_url('http://v.zjcdn.com/x', MEDIA_DOMAINS), 'https://v.zjcdn.com/x')
        with self.assertRaises(ReaderError):
            safe_url('https://' + '.'.join(('127', '0', '0', '1')) + '/a', MEDIA_DOMAINS)

    def test_cookie_expiry_and_no_logging_value(self):
        self.assertIsNone(cookie_value([('Set-Cookie','ttwid=abc; Max-Age=0')]))
        token, expiry = cookie_value([('Set-Cookie','ttwid=abc%7Cdef; Max-Age=10')])
        self.assertEqual(token, 'abc%7Cdef')
        self.assertLessEqual(expiry - time.monotonic(), 10)

    def test_real_data_required(self):
        self.assertEqual(parse_item(PAGE, FINAL)['desc'], '测试')
        for html in ['请登录', '安全验证', '抱歉出错了', '<script>window._ROUTER_DATA={"loaderData":{}}</script>']:
            with self.assertRaises(ReaderError):
                parse_item(html, FINAL)
        with self.assertRaises(ReaderError):
            parse_item(PAGE, FINAL.replace('7687627430039012529', '1111111111111111111'))

    def test_cookie_two_pass(self):
        reader = DouyinReader()
        cookie = ('anonymous', time.monotonic()+600)
        with patch.object(reader, 'page', side_effect=[('抱歉出错了', FINAL, cookie), (PAGE, FINAL, None)]) as fetch:
            self.assertEqual(reader.item(URL), ITEM)
            self.assertEqual(fetch.call_args_list[1].args, (FINAL, 'anonymous'))

    def test_expired_cookie_and_refresh_after_failure(self):
        reader = DouyinReader()
        reader.cookie = ('expired', 0)
        fresh = ('fresh', time.monotonic()+600)
        with patch.object(reader, 'page', side_effect=[('抱歉出错了', FINAL, None),
                                                     ('抱歉出错了', FINAL, fresh), (PAGE, FINAL, None)]) as fetch:
            self.assertEqual(reader.item(URL), ITEM)
            self.assertIsNone(fetch.call_args_list[0].args[1])

    def test_failures_bounded(self):
        reader = DouyinReader()
        with patch.object(reader, 'page', side_effect=OSError('secret must not appear')) as fetch:
            result = reader.read(URL)
            self.assertFalse(result['ok'])
            self.assertNotIn('secret', json.dumps(result))
            self.assertEqual(fetch.call_count, 2)

    def test_cache_and_limit(self):
        reader = DouyinReader()
        with patch.object(reader, 'item', return_value=ITEM) as get_item, patch.object(reader, 'probe', return_value={'downloadable': True, 'download_check':'mp4_range_probe','size_bytes':100,'download_error':None}):
            first = reader.read(URL)
            self.assertEqual(first['duration_seconds'], 61.1)
            self.assertNotIn('_play_urls', first)
            for _ in range(4):
                self.assertTrue(reader.read(URL)['cached'])
            self.assertEqual(get_item.call_count, 1)
            self.assertEqual(reader.read(URL)['error']['code'], 'RATE_LIMITED')

if __name__ == '__main__':
    unittest.main()
