import base64
import json
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import httpx

from douyin_watch import DouyinWatch, JsonMediaBody, OmniClient, TextCache, description
from douyin_frames import DouyinFrames, transcribe_video
from douyin_reader import DouyinReader, MAX_DURATION, MAX_VIDEO_BYTES
from xhs_reader import ReaderError

URL = 'https://www.douyin.com/video/1234567890123456789'
ANSWER = json.dumps({'画面': '画面字幕', '声音': '旁白', '台词': '你好'}, ensure_ascii=False)


class WatchTests(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(MAX_DURATION, 390)
        self.assertEqual(MAX_VIDEO_BYTES, 150_000_000)

    def test_pinned_public_address_failover_preserves_limit(self):
        reader = DouyinReader()
        first, second = Mock(), Mock()
        first.request.side_effect = TimeoutError('private network details')
        second.getresponse.return_value = 'response'
        with patch('douyin_reader.public_addresses', return_value=['first-public', 'second-public']), patch('douyin_reader.PinnedHTTPSConnection', side_effect=[first,second]) as connect:
            connection, response = reader.connection('https://p3.douyinpic.com/picture', ('douyinpic.com',))
        self.assertIs(connection, second)
        self.assertEqual(response, 'response')
        first.close.assert_called_once()
        self.assertEqual(connect.call_count, 2)

    def test_streaming_base64_boundaries_and_content_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            for size in (1, 2, 3, 49151, 49152, 49153, 99000):
                path = pathlib.Path(tmp) / 'video'
                data = bytes(range(251)) * (size // 251) + bytes(range(size % 251))
                path.write_bytes(data)
                payload = {'media': 'data:video/mp4;base64,__LOCAL_MEDIA_0__', 'text': '中文'}
                body = JsonMediaBody(payload, [path])
                encoded = b''.join(body)
                self.assertEqual(len(encoded), body.length)
                self.assertEqual(base64.b64decode(json.loads(encoded)['media'].split(',', 1)[1]), data)
            with patch('douyin_watch.BASE64_LIMIT', 10):
                with self.assertRaises(ReaderError):
                    JsonMediaBody(payload, [path])

    def test_description_nonempty_and_length(self):
        self.assertIn('画面：', description(ANSWER, 300))
        huge = json.dumps({'画面': '字' * 3000, '声音': '声' * 1500, '台词': '话' * 2500})
        for limit in (300, 500):
            text = description(huge, limit)
            self.assertLessEqual(len(text), limit)
            self.assertEqual(len(text.splitlines()), 3)
        for raw in ('', '{}', 'not json', '{"画面":"x","声音":"","台词":"x"}'):
            with self.assertRaises(ReaderError):
                description(raw, 300)

    def test_cache_ask_namespace_expiry_and_short_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = TextCache(tmp, 'model-one')
            cached = {'description': 'text-only', 'title': 'title'}
            cache.put('123', 'question', 'https://v.douyin.com/abcdef/', cached)
            self.assertEqual(cache.resolve('https://v.douyin.com/abcdef/'), '123')
            self.assertEqual(cache.get('123', 'question'), cached)
            self.assertIsNone(cache.get('123', 'different'))
            self.assertIsNone(TextCache(tmp, 'model-two').get('123', 'question'))
            with cache.connect() as conn:
                conn.execute('UPDATE descriptions SET expires=0')
                conn.execute('UPDATE aliases SET expires=0')
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')
            self.assertIsNone(cache.get('123', 'question'))
            self.assertIsNone(cache.resolve('https://v.douyin.com/abcdef/'))
            self.assertEqual(sorted(p.name for p in pathlib.Path(tmp).iterdir()), ['text.sqlite3'])

    def test_sse_join_and_upstream_error_redaction(self):
        client = OmniClient()
        client.key = 'test-key-never-returned'
        def request(status, body):
            transport = httpx.MockTransport(lambda req: httpx.Response(status, text=body))
            real_client = httpx.Client
            with patch('douyin_watch.httpx.Client', side_effect=lambda **kw: real_client(transport=transport, **kw)):
                return client.request([{'type': 'text', 'text': 'test'}], [], time.monotonic() + 10)
        data = '\n'.join(['data: ' + json.dumps({'choices': [{'delta': {'content': text}}]})
                          for text in ('中文', '结果')]) + '\ndata: [DONE]\n'
        self.assertEqual(request(200, data), '中文结果')
        for status, raw in ((401, client.key), (403, client.key), (200, 'data: {"error":"'+client.key+'"}\n'),
                            (200, 'data: {"choices":[]}\n')):
            with self.assertRaises(ReaderError) as error:
                request(status, raw)
            self.assertNotIn(client.key, str(error.exception))

    def test_complete_timeline_fps_merge_and_cleanup(self):
        reader = Mock()
        frames = Mock()
        frames.download.return_value = 33716436
        client = Mock(model='qwen3-omni-flash', base_url='https://example.com', key='placeholder')
        client.request.return_value = ANSWER
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            worker = DouyinWatch(reader, frames, client=client, cache=Mock())
            calls = []
            def encode(video, target, start, length, fps, deadline):
                calls.append((start, length, fps))
                target.write_bytes(b'mp4')
            with patch('douyin_watch.probe_file', return_value=(300.034, True)), patch('douyin_watch.encode_segment', side_effect=encode):
                text, details = worker.video({}, folder, '', time.monotonic()+10)
            self.assertEqual(calls, [(0.0, 120.0, 1), (120.0, 120.0, 1), (240.0, 60.03399999999999, 1)])
            self.assertEqual(details['coverage'], 'full')
            self.assertEqual(details['segments'][-1]['end_seconds'], 300.034)
            self.assertEqual(client.request.call_count, 4)
            self.assertTrue(details['audio_included'])
            self.assertLessEqual(len(text), 500)
            self.assertFalse((folder/'segment.mp4').exists())

    def test_oversized_segment_resplits_without_gaps(self):
        client = Mock(model='model', base_url='https://example.com', key='placeholder')
        client.request.return_value = ANSWER
        worker = DouyinWatch(Mock(), Mock(), client=client, cache=Mock())
        with tempfile.TemporaryDirectory() as tmp:
            def encode(video, target, start, length, fps, deadline):
                target.write_bytes(b'x' * (11 if length > 30 else 1))
            with patch('douyin_watch.probe_file', return_value=(60, False)), patch('douyin_watch.encode_segment', side_effect=encode), patch('douyin_watch.RAW_VIDEO_LIMIT', 10):
                _, detail = worker.video({}, pathlib.Path(tmp), '', time.monotonic()+10)
            self.assertEqual(detail['segments'], [{'start_seconds': 0, 'end_seconds': 30}, {'start_seconds': 30, 'end_seconds': 60}])
            self.assertEqual(detail['fps'], 2)

    def test_failed_watch_deletes_media_and_releases_lock(self):
        reader = Mock()
        reader.metadata.return_value = {'video_id': '123', 'type': 'video'}
        frames = DouyinFrames(reader)
        client = Mock(model='model', base_url='https://example.com', key='placeholder')
        cache = Mock()
        cache.resolve.return_value = None
        cache.get.return_value = None
        worker = DouyinWatch(reader, frames, client=client, cache=cache)
        folders = []
        def fail(metadata, folder, *args):
            folders.append(folder)
            (folder/'video.mp4').write_bytes(b'temporary')
            raise ReaderError('OMNI_HTTP_ERROR', '百炼返回 HTTP 403。')
        with patch.object(worker, 'video', side_effect=fail):
            result = worker.read(URL)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'OMNI_HTTP_ERROR')
        self.assertFalse(frames.lock.locked())
        self.assertTrue(all(not p.exists() for p in folders))
        cache.put.assert_not_called()

    def test_asr_segmentation_mono16k_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            commands = []
            def run(command, *args):
                commands.append(command)
                pathlib.Path(command[-1]).write_bytes(b'audio')
            with patch('douyin_frames.ASR_MAX_SECONDS', 120), patch('douyin_frames.run_media', side_effect=run), patch('douyin_frames.transcribe_audio', side_effect=['one', 'two', 'three']):
                self.assertEqual(transcribe_video(folder/'video.mp4', folder, 300, time.monotonic()+10), 'one\ntwo\nthree')
            self.assertEqual(len(commands), 3)
            self.assertTrue(all(c[c.index('-ar')+1]=='16000' and c[c.index('-ac')+1]=='1' for c in commands))
            self.assertEqual(list(folder.iterdir()), [])

    def test_gallery_network_fallback_and_nine_image_limit(self):
        client = Mock(model='model', base_url='https://example.com', key='placeholder')
        client.request.return_value = ANSWER
        worker = DouyinWatch(Mock(), Mock(), client=client, cache=Mock())
        sources = [['https://a.douyinpic.com/one', 'https://b.douyinpic.com/two']] * 12
        with tempfile.TemporaryDirectory() as tmp:
            def write(data, target): target.write_bytes(b'jpeg')
            with patch('douyin_watch.download_image', side_effect=[OSError('private-network-error'), b'jpeg']+[b'jpeg']*8) as fetch, patch('douyin_watch.image_file', side_effect=write):
                _, detail = worker.gallery({'_image_sources':sources}, pathlib.Path(tmp), '', time.monotonic()+10)
            self.assertEqual(fetch.call_count, 10)
            self.assertEqual(detail['analyzed_image_count'], 9)
            self.assertTrue(detail['image_limit_reached'])
            self.assertFalse(detail['background_music_analyzed'])
            content = client.request.call_args.args[0]
            self.assertEqual(sum(c['type']=='image_url' for c in content), 9)
            self.assertIn('未提供音频', content[-1]['text'])


if __name__ == '__main__':
    unittest.main()
