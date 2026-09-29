import concurrent.futures
import io
import json
import unittest
from unittest import mock
from PIL import Image
import xhs_images as images
from xhs_reader import Reader, ReaderError, SlidingLimit

POST_URL = 'https://www.xiaohongshu.com/explore/' + 'a' * 24
CDN = 'https://sns-webpic-qc.xhscdn.com/'


def make_image(size=(320, 240), mode='RGB', color=None, fmt='PNG', **kwargs):
    with Image.new(mode, size, color or ('white' if mode == 'RGB' else (0, 0, 0, 0))) as image:
        stream = io.BytesIO()
        image.save(stream, format=fmt, **kwargs)
        return stream.getvalue()


def post_page(count=6):
    note = {'noteId': 'a' * 24, 'title': 'Image fixture', 'desc': 'Text only',
            'imageList': [{'url': CDN + '%s.jpg' % index} for index in range(1, count + 1)]}
    return '<script>window.__INITIAL_STATE__=' + json.dumps({'noteData': {'data': {'noteData': note}}}) + ';</script>'


class ImageTests(unittest.TestCase):
    def reader(self, count=6, clock=None):
        now = clock or (lambda: 0.0)
        post_fetch = mock.Mock(return_value=(200, POST_URL, post_page(count)))
        post = Reader(fetch=post_fetch, clock=now)
        download = mock.Mock(return_value=make_image())
        result = images.ImageReader(post, download=download, clock=now)
        return result, post_fetch, download

    def test_defaults_first_two(self):
        reader, fetch, download = self.reader()
        summary, parts = reader.read(POST_URL)
        self.assertTrue(summary['ok'])
        self.assertEqual(summary['returned_indexes'], [1, 2])
        self.assertEqual(len(parts), 2)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(fetch.call_count, 1)

    def test_selection_order_and_four_max(self):
        reader, _, download = self.reader()
        summary, parts = reader.read(POST_URL, [4, 1, 6, 2])
        self.assertEqual(summary['returned_indexes'], [4, 1, 6, 2])
        self.assertEqual([part['index'] for part in parts], [4, 1, 6, 2])
        self.assertEqual(download.call_count, 4)

    def test_invalid_selections_do_not_fetch(self):
        for indexes in ([], [1, 2, 3, 4, 5], [0], [-1], [True], [1.0], ['1'], [1, 1], '1'):
            reader, fetch, download = self.reader()
            summary, parts = reader.read(POST_URL, indexes)
            self.assertEqual(summary['error']['code'], 'INVALID_INDEXES', indexes)
            self.assertEqual(parts, [])
            fetch.assert_not_called()
            download.assert_not_called()

    def test_missing_or_out_of_range(self):
        reader, _, download = self.reader(count=1)
        result, parts = reader.read(POST_URL)
        self.assertEqual(result['returned_indexes'], [1])
        result, _ = reader.read(POST_URL, [2])
        self.assertEqual(result['error']['code'], 'INDEX_OUT_OF_RANGE')
        reader, _, download = self.reader(count=0)
        result, _ = reader.read(POST_URL)
        self.assertEqual(result['error']['code'], 'NO_IMAGES')
        download.assert_not_called()

    def test_jpeg_resize_preserves_aspect_ratio(self):
        for original, expected in (((4000, 2000), (1568, 784)), ((500, 1000), (500, 1000)), ((1080, 3000), (564, 1568))):
            with self.subTest(original=original):
                result = images.jpeg_tiles(make_image(original))
                self.assertEqual(len(result), 1)
                with Image.open(io.BytesIO(result[0]['jpeg'])) as image:
                    self.assertEqual(image.format, 'JPEG')
                    self.assertEqual(image.mode, 'RGB')
                    self.assertEqual(image.size, expected)
                    self.assertLessEqual(max(image.size), 1568)

    def test_long_images_crop_before_resize_without_gaps(self):
        for size in ((800, 6000), (150, 1000)):
            parts = images.jpeg_tiles(make_image(size))
            self.assertGreater(len(parts), 1)
            self.assertEqual(parts[0]['crop'][1], 0)
            self.assertEqual(parts[-1]['crop'][3], size[1])
            for previous, current in zip(parts, parts[1:]):
                self.assertLess(current['crop'][1], previous['crop'][3])
                self.assertGreater(current['crop'][1], previous['crop'][1])
            for part in parts:
                self.assertEqual(part['width'], size[0])
                self.assertLessEqual(max(part['width'], part['height']), 1568)

    def test_exif_orientation_and_metadata_removed(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = 'PRIVATE METADATA'
        parts = images.jpeg_tiles(make_image((1000, 100), fmt='JPEG', exif=exif))
        self.assertGreater(len(parts), 1)
        self.assertEqual(parts[0]['source_size'], (100, 1000))
        with Image.open(io.BytesIO(parts[0]['jpeg'])) as image:
            self.assertEqual(len(image.getexif()), 0)

    def test_transparency_is_white(self):
        tile = images.jpeg_tiles(make_image((10, 10), mode='RGBA'))[0]
        with Image.open(io.BytesIO(tile['jpeg'])) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    def test_corrupt_and_pixel_limit(self):
        with self.assertRaises(ReaderError):
            images.jpeg_tiles(b'<html>Please login</html>')
        with mock.patch.object(images, 'MAX_PIXELS', 50), self.assertRaises(ReaderError):
            images.jpeg_tiles(make_image((10, 10)))
        with self.assertRaises(ReaderError):
            images.crop_boxes(100, 100000)

    def test_cdn_allowlist_boundaries(self):
        self.assertEqual(images.validate_cdn_url('http://sns-webpic-qc.xhscdn.com/x?a=1'), CDN + 'x?a=1')
        for url in ('https://xhscdn.com.evil.test/x', 'https://evilxhscdn.com/x',
                    'https://' + '.'.join(('127', '0', '0', '1')) + '/x', 'https://www.xiaohongshu.com/x',
                    'https://evil@xhscdn.com/x', 'https://xhscdn.com:8443/x',
                    'file:///x', 'https://xhscdn.com/\nx', 'https://xhscdn.com\\@evil/x'):
            with self.subTest(url=url), self.assertRaises(ReaderError):
                images.validate_cdn_url(url)

    def test_download_revalidates_redirects_and_rejects_nonimages(self):
        for status, headers, expected in (
            (302, {'Location':'http://' + '.'.join(('169','254','169','254')) + '/latest'}, 'UNSAFE_IMAGE_URL'),
            (200, {'Content-Type':'text/html'}, 'INVALID_IMAGE_TYPE'),
            (200, {'Content-Type':'image/jpeg','Content-Length':str(images.MAX_DOWNLOAD+1)}, 'IMAGE_TOO_LARGE'),
            (403, {}, 'IMAGE_ACCESS_BLOCKED')):
            response = mock.Mock(status=status)
            response.getheader.side_effect = lambda key: headers.get(key)
            conn = mock.Mock()
            conn.getresponse.return_value = response
            with mock.patch.object(images, 'public_addresses', return_value=['public-address']), mock.patch.object(images, 'PinnedHTTPSConnection', return_value=conn):
                with self.assertRaises(ReaderError) as exc:
                    images.download_image(CDN + 'x', SlidingLimit())
                self.assertEqual(exc.exception.code, expected)
                conn.close.assert_called_once()

    def test_private_dns_block_precedes_network(self):
        with mock.patch.object(images, 'public_addresses', side_effect=ReaderError('UNSAFE_ADDRESS', 'private')), mock.patch.object(images, 'PinnedHTTPSConnection') as conn:
            with self.assertRaises(ReaderError):
                images.download_image(CDN + 'x', SlidingLimit())
            conn.assert_not_called()

    def test_cache_expires_after_ten_minutes(self):
        now = [0.0]
        reader, fetch, download = self.reader(clock=lambda: now[0])
        reader.read(POST_URL)
        result, _ = reader.read(POST_URL)
        self.assertTrue(all(i['cached'] for i in result['images']))
        self.assertEqual(download.call_count, 2)
        now[0] = 600
        result, _ = reader.read(POST_URL)
        self.assertFalse(any(i['cached'] for i in result['images']))
        self.assertEqual(download.call_count, 4)
        self.assertEqual(fetch.call_count, 2)

    def test_shared_call_limit_with_text_tool(self):
        reader, _, _ = self.reader()
        self.assertTrue(reader.reader.read(POST_URL)['ok'])
        for _ in range(4):
            result, _ = reader.read(POST_URL, [1])
            self.assertTrue(result['ok'])
        self.assertEqual(reader.reader.read(POST_URL)['error']['code'], 'RATE_LIMITED')
        result, _ = reader.read(POST_URL)
        self.assertEqual(result['error']['code'], 'RATE_LIMITED')

    def test_shared_outbound_budget_and_partial_results(self):
        reader, _, _ = self.reader()
        for _ in range(4):
            reader.reader.outbound.take()
        def fetch(url, limiter):
            limiter.take()
            return make_image()
        reader.download = fetch
        result, parts = reader.read(POST_URL)
        self.assertTrue(result['ok'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['returned_indexes'], [1])
        self.assertEqual(result['errors'][0]['index'], 2)
        self.assertEqual(result['errors'][0]['code'], 'RATE_LIMITED')
        self.assertEqual(len(parts), 1)

    def test_download_failures_and_post_errors(self):
        reader, _, download = self.reader()
        download.side_effect = TimeoutError()
        result, parts = reader.read(POST_URL)
        self.assertFalse(result['ok'])
        self.assertEqual(len(result['errors']), 2)
        self.assertEqual(parts, [])
        reader, fetch, download = self.reader()
        fetch.return_value = (403, POST_URL, 'blocked')
        result, parts = reader.read(POST_URL)
        self.assertFalse(result['ok'])
        download.assert_not_called()

    def test_concurrent_same_images_download_once(self):
        reader, _, download = self.reader()
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            results = list(pool.map(reader.read, [POST_URL] * 3))
        self.assertTrue(all(summary['ok'] for summary, _ in results))
        self.assertEqual(download.call_count, 2)

    def test_cache_memory_and_response_caps(self):
        reader, _, _ = self.reader()
        with mock.patch.object(images, 'MAX_CACHE_BYTES', 2500):
            reader.read(POST_URL, [1, 2, 3, 4])
            self.assertLessEqual(reader.cache_bytes, 2500)
        reader, _, _ = self.reader()
        with mock.patch.object(images, 'MAX_RESULT_PARTS', 1):
            summary, parts = reader.read(POST_URL)
            self.assertTrue(summary['partial'])
            self.assertEqual(len(parts), 1)
            self.assertEqual(summary['errors'][0]['code'], 'RESPONSE_LIMIT')


if __name__ == '__main__':
    unittest.main()
