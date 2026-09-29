"""On-demand XHS CDN images; shared post reader, rate limits and 600 s cache."""
import collections
import http.client
import io
import re
import threading
import time
import urllib.parse
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from xhs_reader import (MOBILE_UA, PinnedHTTPSConnection, ReaderError,
                             error_result, public_addresses)

MAX_DOWNLOAD = 10 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_EDGE = 1568
JPEG_QUALITY = 85
MAX_PARTS = 16
MAX_RESULT_PARTS = 24
MAX_RESULT_BYTES = 6 * 1024 * 1024
MAX_CACHE_BYTES = 12 * 1024 * 1024
CDN_DOMAINS = ('xhscdn.com',)


def validate_cdn_url(url):
    if not isinstance(url, str) or len(url) > 8192 or re.search(r'[\s\\\x00-\x1f\x7f]', url):
        raise ReaderError('UNSAFE_IMAGE_URL', '图片地址无效。')
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ''
        if (p.scheme not in ('http', 'https') or p.username is not None or p.password is not None or
                not host.isascii() or p.port not in (None, 443 if p.scheme == 'https' else 80) or
                not any(host == d or host.endswith('.' + d) for d in CDN_DOMAINS)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ReaderError('UNSAFE_IMAGE_URL', '只允许下载帖子提供的 xhscdn.com 小红书 CDN 图片。')
    return urllib.parse.urlunsplit(('https', host, p.path or '/', p.query, ''))


def download_image(url, limiter):
    current = validate_cdn_url(url)
    deadline = time.monotonic() + 25
    for _ in range(3):
        if time.monotonic() >= deadline:
            raise ReaderError('IMAGE_TIMEOUT', '图片下载超时。')
        p = urllib.parse.urlsplit(current)
        addresses = public_addresses(p.hostname)
        limiter.take()  # Shared with post pages; every redirect consumes one request.
        connection = PinnedHTTPSConnection(p.hostname, addresses[0])
        connection.timeout = min(10, max(0.1, deadline - time.monotonic()))
        try:
            path = urllib.parse.urlunsplit(('', '', p.path, p.query, ''))
            connection.request('GET', path, headers={
                'User-Agent': MOBILE_UA, 'Accept': 'image/jpeg,image/png,image/webp',
                'Referer': 'https://www.xiaohongshu.com/',
                'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = connection.getresponse()
            location = response.getheader('Location')
            if response.status in (301, 302, 303, 307, 308) and location:
                current = validate_cdn_url(urllib.parse.urljoin(current, location))
                continue
            if response.status in (401, 403, 429):
                raise ReaderError('IMAGE_ACCESS_BLOCKED', '图片 CDN 拒绝访问（HTTP %s），链接可能已过期或触发风控。' % response.status)
            if response.status != 200:
                raise ReaderError('IMAGE_HTTP_ERROR', '图片 CDN 返回 HTTP %s。' % response.status)
            content_type = (response.getheader('Content-Type') or '').split(';')[0].lower()
            if content_type not in ('image/jpeg', 'image/png', 'image/webp'):
                raise ReaderError('INVALID_IMAGE_TYPE', 'CDN 未返回受支持的 JPEG、PNG 或 WebP 图片。')
            length = response.getheader('Content-Length')
            if length and length.isdigit() and int(length) > MAX_DOWNLOAD:
                raise ReaderError('IMAGE_TOO_LARGE', '原图文件超过 10 MiB 限制。')
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReaderError('IMAGE_TIMEOUT', '图片下载超时。')
                if connection.sock:
                    connection.sock.settimeout(min(10, remaining))
                chunk = response.read1(min(65536, MAX_DOWNLOAD + 1 - size))
                if not chunk:
                    return b''.join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise ReaderError('IMAGE_TOO_LARGE', '原图文件超过 10 MiB 限制。')
        finally:
            connection.close()
    raise ReaderError('IMAGE_REDIRECT_LIMIT', '图片重定向次数过多。')


def crop_boxes(width, height):
    if height <= width * 3:
        return [(0, 0, width, height)]
    # Crop before resizing: small text in a tall image never shrinks as one page.
    tile_height = max(width, min(MAX_EDGE, round(width * 1.5)))
    overlap = max(1, min(tile_height // 8, round(64 * max(1, width / MAX_EDGE))))
    boxes, top = [], 0
    while top < height:
        bottom = min(height, top + tile_height)
        boxes.append((0, top, width, bottom))
        if len(boxes) > MAX_PARTS:
            raise ReaderError('IMAGE_TOO_LONG', '这张长图切分后超过 16 段，请改用较短的图片。')
        if bottom == height:
            break
        top = bottom - overlap
    return boxes


def jpeg_tiles(data):
    """Return immutable JPEG bytes plus dimensions and original crop coordinates."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=('JPEG', 'PNG', 'WEBP')) as source:
                if source.width * source.height > MAX_PIXELS:
                    raise ReaderError('IMAGE_PIXEL_LIMIT', '图片超过 1600 万像素，已停止处理以保护服务内存。')
                if getattr(source, 'n_frames', 1) != 1:
                    raise ReaderError('ANIMATED_IMAGE_UNSUPPORTED', '暂不读取动画图片，请使用静态图片。')
                source.load()
                ImageOps.exif_transpose(source, in_place=True)
                original_size = source.size
                boxes = crop_boxes(*original_size)
                tiles, total = [], 0
                for box in boxes:
                    tile = source.crop(box)
                    try:
                        tile.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                        if tile.mode != 'RGB':
                            if tile.mode in ('RGBA', 'LA') or 'transparency' in tile.info:
                                rgba = tile.convert('RGBA')
                                rgb = Image.new('RGB', tile.size, 'white')
                                rgb.paste(rgba, mask=rgba.getchannel('A'))
                                rgba.close()
                            else:
                                rgb = tile.convert('RGB')
                            tile.close()
                            tile = rgb
                        output = io.BytesIO()
                        # Do not carry EXIF/GPS/metadata into tool results.
                        tile.save(output, format='JPEG', quality=JPEG_QUALITY, subsampling=0)
                        encoded = output.getvalue()
                        total += len(encoded)
                        if total > MAX_RESULT_BYTES:
                            raise ReaderError('IMAGE_RESULT_TOO_LARGE', '图片压缩后仍过大，无法一次返回。')
                        tiles.append({'jpeg': encoded, 'width': tile.width, 'height': tile.height,
                                      'source_size': original_size, 'crop': box})
                    finally:
                        tile.close()
                return tuple(tiles)
    except ReaderError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ReaderError('INVALID_IMAGE', '图片损坏、格式不支持或像素数量异常。')


def validate_indexes(indexes):
    if indexes is None:
        return None
    if (not isinstance(indexes, list) or not 1 <= len(indexes) <= 4 or
            any(type(index) is not int or index < 1 for index in indexes) or
            len(set(indexes)) != len(indexes)):
        raise ReaderError('INVALID_INDEXES', 'indexes 必须包含 1 至 4 个不重复的正整数，图片从第 1 张开始；省略时读取前 2 张。')
    return indexes


class ImageReader:
    def __init__(self, reader, download=download_image, clock=time.monotonic):
        self.reader, self.download, self.clock = reader, download, clock
        self.cache = collections.OrderedDict()
        self.cache_bytes = 0
        self.lock = threading.Lock()

    def cached_tiles(self, url):
        now = self.clock()
        for key in list(self.cache):
            until, _, size = self.cache[key]
            if until <= now:
                self.cache_bytes -= size
                del self.cache[key]
        if url in self.cache:
            self.cache.move_to_end(url)
            return self.cache[url][1], True
        tiles = jpeg_tiles(self.download(url, self.reader.outbound))
        size = sum(len(tile['jpeg']) for tile in tiles)
        if size > MAX_CACHE_BYTES:
            return tiles, False
        while self.cache and self.cache_bytes + size > MAX_CACHE_BYTES:
            _, (_, _, old_size) = self.cache.popitem(last=False)
            self.cache_bytes -= old_size
        self.cache[url] = (self.clock() + 600, tiles, size)
        self.cache_bytes += size
        return tiles, False

    def read(self, url, indexes=None):
        try:
            try:
                indexes = validate_indexes(indexes)
            except ReaderError:
                self.reader.calls.take()
                raise
            # Reuse the unchanged reader: exactly one shared tool-call charge,
            # the same post cache, and the same outbound budget.
            post = self.reader.read(url)
            if not post.get('ok'):
                return post, []
            images = post['images']
            if not images:
                raise ReaderError('NO_IMAGES', '这篇帖子没有可读取的静态图片。')
            selected = indexes if indexes is not None else list(range(1, min(2, len(images)) + 1))
            if any(index > len(images) for index in selected):
                raise ReaderError('INDEX_OUT_OF_RANGE', '这篇帖子共有 %s 张图片，请提供范围内的序号。' % len(images))
            parts, summaries, errors = [], [], []
            total = 0
            with self.lock:
                for index in selected:
                    try:
                        cdn_url = validate_cdn_url(images[index - 1])
                        tiles, cached = self.cached_tiles(cdn_url)
                        size = sum(len(tile['jpeg']) for tile in tiles)
                        if len(parts) + len(tiles) > MAX_RESULT_PARTS or total + size > MAX_RESULT_BYTES:
                            raise ReaderError('RESPONSE_LIMIT', '本次返回的切片数量或图片总大小达到上限，请单独读取这张图片。')
                        total += size
                        for number, tile in enumerate(tiles, 1):
                            parts.append(dict(tile, index=index, part=number, parts=len(tiles)))
                        summaries.append({'index': index, 'parts': len(tiles), 'cached': cached,
                                          'original_width': tiles[0]['source_size'][0],
                                          'original_height': tiles[0]['source_size'][1]})
                    except ReaderError as error:
                        errors.append(dict(error_result(error)['error'], index=index))
                    except (OSError, http.client.HTTPException):
                        errors.append({'index': index, 'code': 'IMAGE_NETWORK_ERROR', 'message': '图片下载失败，请稍后重试。'})
                    except Exception:
                        errors.append({'index': index, 'code': 'IMAGE_READ_FAILED', 'message': '图片处理失败。'})
            result = {'ok': bool(parts), 'partial': bool(parts and errors), 'title': post['title'],
                      'image_count': len(images), 'requested_indexes': selected,
                      'returned_indexes': [item['index'] for item in summaries],
                      'returned_parts': len(parts), 'images': summaries, 'errors': errors,
                      'format': 'image/jpeg', 'max_edge': MAX_EDGE, 'jpeg_quality': JPEG_QUALITY}
            if not parts:
                result['error'] = errors[0] if errors else {'code': 'NO_IMAGES', 'message': '没有可返回的图片。'}
            return result, parts
        except ReaderError as error:
            return error_result(error), []
        except Exception:
            return error_result(ReaderError('IMAGE_READ_FAILED', '图片读取失败，请稍后重试。')), []
