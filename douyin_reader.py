"""Public Douyin metadata. Anonymous ttwid and cached results live only in RAM."""
import collections
import copy
import datetime
import email.utils
import http.cookies
import json
import re
import threading
import time
import urllib.parse

from xhs_reader import (MOBILE_UA, PinnedHTTPSConnection, ReaderError,
                             SlidingLimit, error_result, public_addresses)

PAGE_DOMAINS = ('douyin.com', 'iesdouyin.com')
MEDIA_DOMAINS = ('snssdk.com', 'douyinvod.com', 'zjcdn.com')
IMAGE_DOMAINS = ('douyinpic.com',)
MAX_VIDEO_BYTES = 100_000_000
MAX_DURATION = 300


def safe_url(url, domains):
    try:
        if not isinstance(url, str) or len(url) > 8192 or re.search(r'[\s\\\x00-\x1f\x7f]', url):
            raise ValueError()
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ''
        if (p.scheme not in ('http', 'https') or p.username is not None or p.password is not None or
                p.port not in (None, 443 if p.scheme == 'https' else 80) or not host.isascii() or
                not any(host == d or host.endswith('.' + d) for d in domains)):
            raise ValueError()
        return urllib.parse.urlunsplit(('https', host, p.path or '/', p.query, ''))
    except (TypeError, ValueError):
        raise ReaderError('UNSAFE_URL', '只接受抖音分享链接及页面提供的许可 CDN 地址。')


def post_url(url):
    normalized = safe_url(url, PAGE_DOMAINS)
    p = urllib.parse.urlsplit(normalized)
    if p.hostname == 'v.douyin.com' and re.fullmatch(r'/[A-Za-z0-9_-]{5,80}/?', p.path):
        return normalized
    if re.fullmatch(r'/(?:share/)?(?:video|note|slides)/\d{10,30}/?', p.path):
        return normalized
    raise ReaderError('INVALID_VIDEO_URL', '请提供抖音分享链接或 /video/、/note/、/slides/数字ID 链接。')


def page_url(url):
    """Notes and slides have a public mobile note route, even on desktop links."""
    url = safe_url(url, PAGE_DOMAINS)
    p = urllib.parse.urlsplit(url)
    match = re.fullmatch(r'/(?:share/)?(?:note|slides)/(\d{10,30})/?', p.path)
    if match:
        return urllib.parse.urlunsplit(('https', 'm.douyin.com', '/share/note/' + match.group(1), p.query, ''))
    return url


def is_image_post(item):
    # The real image fixture also contains a video/play_addr object. Images win.
    return bool(isinstance(item.get('images'), list) and item['images']) or item.get('aweme_type') in (2, 68)


def image_sources(item):
    images = item.get('images') or []
    if not isinstance(images, list) or len(images) > 100:
        raise ReaderError('IMAGE_DATA_INVALID', '图片列表格式异常或超过 100 张。')
    result = []
    for entry in images:
        urls = []
        if isinstance(entry, dict):
            candidates = entry.get('url_list') or []
            for candidate in (candidates[:4] if isinstance(candidates, list) else []):
                try:
                    urls.append(safe_url(candidate, IMAGE_DOMAINS))
                except ReaderError:
                    continue
        # Preserve original indexes even if one image has no allowed URL.
        result.append(urls)
    return result


def cookie_value(headers):
    for key, value in headers:
        if key.lower() != 'set-cookie':
            continue
        try:
            cookies = http.cookies.SimpleCookie()
            cookies.load(value)
            if 'ttwid' not in cookies:
                continue
            item = cookies['ttwid']
            # Use no longer than one hour, or the server's shorter expiry.
            ttl = 3600.0
            if item['max-age']:
                ttl = min(ttl, float(item['max-age']))
            elif item['expires']:
                ttl = min(ttl, email.utils.parsedate_to_datetime(item['expires']).timestamp() - time.time())
            token = item.value
            if ttl > 0 and re.fullmatch(r'[A-Za-z0-9_%|.+/=~-]{1,4096}', token):
                return token, time.monotonic() + ttl
        except (ValueError, TypeError, OverflowError, http.cookies.CookieError):
            continue
    return None


def parse_item(page, final_url):
    match = re.search(r'(?:window\.)?_ROUTER_DATA\s*=\s*', page)
    if match:
        try:
            state, _ = json.JSONDecoder().raw_decode(page[match.end():])
            loaders = state['loaderData']
            for route in ('note_(id)/page', 'slides_(id)/page', 'video_(id)/page'):
                payload = loaders.get(route) or {}
                items = (payload.get('videoInfoRes') or {}).get('item_list') or []
                item = items[0] if items else None
                if isinstance(item, dict) and (is_image_post(item) or
                        (isinstance(item.get('video'), dict) and item['video'].get('play_addr'))):
                    expected = re.search(r'/(?:video|note|slides)/(\d+)', urllib.parse.urlsplit(final_url).path)
                    if expected and str(item.get('aweme_id')) != expected.group(1):
                        raise ReaderError('VIDEO_ID_MISMATCH', '分享页返回的帖子 ID 不匹配。')
                    return item
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            pass
    visible = re.sub(r'<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>', '', page, flags=re.S | re.I)
    visible = re.sub(r'<[^>]+>', ' ', visible)
    if any(s in visible for s in ('验证码', '安全验证', '访问频繁', '访问异常')):
        raise ReaderError('CAPTCHA_OR_RISK_CONTROL', '抖音返回验证或风控页面。')
    if any(s in visible for s in ('请登录', '登录后观看', '扫码登录')):
        raise ReaderError('LOGIN_REQUIRED', '此视频要求登录。')
    raise ReaderError('VIDEO_DATA_UNAVAILABLE', '分享页没有可用视频数据，可能要求在 App 内观看或暂时不可用。')


class DouyinReader:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = SlidingLimit(5)
        self.outbound = SlidingLimit(20)
        self.cache = collections.OrderedDict()
        self.cookie = None

    def take_call(self):
        with self.lock:
            self.calls.take()

    def connection(self, url, domains, headers=None, timeout=12):
        url = safe_url(url, domains)
        p = urllib.parse.urlsplit(url)
        addresses = public_addresses(p.hostname)
        with self.lock:
            self.outbound.take()
        conn = PinnedHTTPSConnection(p.hostname, addresses[0])
        conn.timeout = timeout
        request_headers = {'User-Agent': MOBILE_UA, 'Accept-Encoding': 'identity', 'Connection': 'close'}
        request_headers.update(headers or {})
        try:
            conn.request('GET', urllib.parse.urlunsplit(('', '', p.path, p.query, '')), headers=request_headers)
            return conn, conn.getresponse()
        except BaseException:
            conn.close()
            raise

    def page(self, url, token):
        acquired = None
        for _ in range(4):
            url = page_url(url)
            headers = {'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'zh-CN,zh;q=0.9'}
            if token:
                headers['Cookie'] = 'ttwid=' + token
            conn, res = self.connection(url, PAGE_DOMAINS, headers)
            try:
                acquired = cookie_value(res.getheaders()) or acquired
                location = res.getheader('Location')
                if res.status in (301, 302, 303, 307, 308) and location:
                    url = safe_url(urllib.parse.urljoin(url, location), PAGE_DOMAINS)
                    continue
                if res.status in (401, 403, 429):
                    raise ReaderError('ACCESS_BLOCKED', '抖音拒绝访问（HTTP %s）。' % res.status)
                if res.status != 200:
                    raise ReaderError('PAGE_HTTP_ERROR', '分享页返回 HTTP %s。' % res.status)
                body = res.read(3 * 1024 * 1024 + 1)
                if len(body) > 3 * 1024 * 1024:
                    raise ReaderError('PAGE_TOO_LARGE', '分享页超过大小限制。')
                return body.decode('utf-8', errors='replace'), url, acquired
            finally:
                conn.close()
        raise ReaderError('REDIRECT_LIMIT', '分享页重定向次数过多。')

    def item(self, url):
        if self.cookie and self.cookie[1] <= time.monotonic():
            self.cookie = None
        final = url
        last_error = None
        # At most two acquisition cycles; no unbounded retries or challenge execution.
        for _ in range(2):
            token = self.cookie[0] if self.cookie else None
            try:
                page, final, fresh = self.page(final, token)
                if fresh:
                    self.cookie = fresh
                try:
                    return parse_item(page, final)
                except ReaderError as error:
                    if error.code != 'VIDEO_DATA_UNAVAILABLE':
                        raise
                    if not token and fresh:
                        page, final, updated = self.page(final, fresh[0])
                        self.cookie = updated or fresh
                        try:
                            return parse_item(page, final)
                        except ReaderError as retry_error:
                            video_id = re.search(r'/(video|note|slides)/(\d+)', urllib.parse.urlsplit(final).path)
                            if retry_error.code != 'VIDEO_DATA_UNAVAILABLE' or not video_id:
                                raise
                            # The already tested mobile share endpoint is a bounded fallback.
                            kind = 'video' if video_id.group(1) == 'video' else 'note'
                            alternate = 'https://m.douyin.com/share/' + kind + '/' + video_id.group(2)
                            page, alternate, updated = self.page(alternate, self.cookie[0])
                            self.cookie = updated or self.cookie
                            return parse_item(page, alternate)
                    raise
            except ReaderError as error:
                self.cookie = None
                if error.code not in ('VIDEO_DATA_UNAVAILABLE', 'PAGE_HTTP_ERROR'):
                    raise
                last_error = error
            except OSError:
                self.cookie = None
                last_error = ReaderError('NETWORK_ERROR', '分享页请求失败，请稍后重试。')
        raise last_error or ReaderError('VIDEO_DATA_UNAVAILABLE', '没有取得视频信息。')

    def open_media(self, url, headers=None, timeout=12):
        for _ in range(4):
            conn, res = self.connection(url, MEDIA_DOMAINS, headers, timeout)
            location = res.getheader('Location')
            if res.status in (301, 302, 303, 307, 308) and location:
                conn.close()
                url = safe_url(urllib.parse.urljoin(url, location), MEDIA_DOMAINS)
                continue
            return conn, res
        raise ReaderError('MEDIA_REDIRECT_LIMIT', '视频地址重定向次数过多。')

    def probe(self, urls):
        for url in urls[:2]:
            conn = None
            try:
                conn, res = self.open_media(url, {'Range': 'bytes=0-65535'})
                if res.status not in (200, 206):
                    continue
                prefix = res.read(65536)
                if len(prefix) < 12 or prefix[4:8] != b'ftyp':
                    continue
                content_range = res.getheader('Content-Range') or ''
                size = re.search(r'/(\d+)$', content_range)
                total = int(size.group(1)) if size else None
                if total is None and res.status == 200 and (res.getheader('Content-Length') or '').isdigit():
                    total = int(res.getheader('Content-Length'))
                return {'downloadable': True, 'download_check': 'mp4_range_probe',
                        'size_bytes': total, 'download_error': None}
            except ReaderError as error:
                if error.code == 'RATE_LIMITED':
                    raise
            except Exception:
                pass
            finally:
                if conn:
                    conn.close()
        return {'downloadable': False, 'download_check': 'failed', 'size_bytes': None,
                'download_error': 'VPS 未能读取有效 MP4 数据；地址可能过期或 CDN 拒绝访问。'}

    def metadata(self, url, force=False):
        url = page_url(post_url(url))
        with self.lock:
            now = time.monotonic()
            for key in list(self.cache):
                if self.cache[key][0] <= now:
                    del self.cache[key]
            if not force and url in self.cache:
                value = copy.deepcopy(self.cache[url][1])
                value['cached'] = True
                return value
            item = self.item(url)
            if is_image_post(item):
                sources = image_sources(item)
                author, stats = item.get('author') or {}, item.get('statistics') or {}
                result = {'ok': True, 'type': 'images', 'video_id': str(item.get('aweme_id', '')),
                          'post_id': str(item.get('aweme_id', '')), 'title': item.get('desc') or '',
                          'body': item.get('desc') or '', 'author': author.get('nickname') or '',
                          'author_id': author.get('unique_id') or author.get('short_id'),
                          'interactions': {name: stats.get(key) for name, key in
                              (('likes','digg_count'), ('comments','comment_count'), ('shares','share_count'), ('favorites','collect_count'), ('plays','play_count'))},
                          'image_count': len(sources), 'images': [s[0] if s else None for s in sources],
                          '_image_sources': sources, 'duration_seconds': None,
                          'cover': sources[0][0] if sources and sources[0] else None,
                          'downloadable': False, 'download_check': 'not_applicable_image_post',
                          'download_error': None, 'size_bytes': None, 'within_processing_limits': False,
                          'background_music_present': bool(item.get('music')), 'transcription_status': 'not_applicable_image_post',
                          'cached': False, 'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
                self.cache[url] = (time.monotonic() + 600, copy.deepcopy(result))
                while len(self.cache) > 64:
                    self.cache.popitem(last=False)
                return result
            video, author = item['video'], item.get('author') or {}
            urls = [safe_url(v, MEDIA_DOMAINS) for v in (video.get('play_addr') or {}).get('url_list', [])][:2]
            duration = video.get('duration')
            duration = float(duration) / 1000 if isinstance(duration, (float, int)) and duration > 0 else None
            covers = []
            for value in (video.get('cover') or {}).get('url_list', [])[:4]:
                try:
                    covers.append(safe_url(value, ('douyinpic.com',)))
                except ReaderError:
                    continue
            stats = item.get('statistics') or {}
            result = {'ok': True, 'type': 'video', 'image_count': 0, 'video_id': str(item.get('aweme_id', '')), 'title': item.get('desc') or '',
                      'author': author.get('nickname') or '', 'author_id': author.get('unique_id') or author.get('short_id'),
                      'interactions': {name: stats.get(key) for name, key in
                                       (('likes','digg_count'), ('comments','comment_count'), ('shares','share_count'), ('favorites','collect_count'), ('plays','play_count'))},
                      'duration_seconds': duration, 'cover': covers[0] if covers else None, 'covers': covers,
                      'cached': False, 'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      '_play_urls': urls}
            result.update(self.probe(urls))
            result['within_processing_limits'] = bool(duration is not None and duration <= MAX_DURATION and
                                                       (result['size_bytes'] is None or result['size_bytes'] <= MAX_VIDEO_BYTES))
            self.cache[url] = (time.monotonic() + (600 if result['downloadable'] else 30), copy.deepcopy(result))
            while len(self.cache) > 64:
                self.cache.popitem(last=False)
            return result

    def read(self, url):
        try:
            self.take_call()
            result = self.metadata(url)
            return {k: v for k, v in result.items() if not k.startswith('_')}
        except ReaderError as error:
            return error_result(error)
        except Exception:
            return error_result(ReaderError('READ_FAILED', '抖音读取失败：网络或数据结构异常，请稍后重试。'))
