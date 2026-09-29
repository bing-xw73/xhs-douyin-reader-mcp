"""On-demand Douyin galleries. Reuse the existing JPEG tiling and RAM cache."""
import http.client
import time
import urllib.parse

from douyin_reader import IMAGE_DOMAINS, safe_url
from xhs_images import (ImageReader, validate_indexes, MAX_DOWNLOAD, MAX_RESULT_PARTS,
                        MAX_RESULT_BYTES, MAX_EDGE, JPEG_QUALITY)
from xhs_reader import ReaderError, error_result


def download_image(url, reader):
    current = safe_url(url, IMAGE_DOMAINS)
    deadline = time.monotonic() + 25
    for _ in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReaderError('IMAGE_TIMEOUT', '抖音图片下载超时。')
        conn, res = reader.connection(current, IMAGE_DOMAINS,
                                      {'Accept':'image/jpeg,image/png,image/webp'}, min(10,remaining))
        try:
            location = res.getheader('Location')
            if res.status in (301,302,303,307,308) and location:
                current = safe_url(urllib.parse.urljoin(current,location), IMAGE_DOMAINS)
                continue
            if res.status in (401,403,429):
                raise ReaderError('IMAGE_ACCESS_BLOCKED', '抖音图片 CDN 拒绝访问（HTTP %s）。' % res.status)
            if res.status != 200:
                raise ReaderError('IMAGE_HTTP_ERROR', '抖音图片返回 HTTP %s。' % res.status)
            content_type = (res.getheader('Content-Type') or '').split(';')[0].lower()
            if content_type not in ('image/jpeg','image/png','image/webp'):
                raise ReaderError('INVALID_IMAGE_TYPE', '抖音 CDN 未返回 JPEG、PNG 或 WebP 图片。')
            length = res.getheader('Content-Length') or ''
            if length.isdigit() and int(length)>MAX_DOWNLOAD:
                raise ReaderError('IMAGE_TOO_LARGE', '图片超过 10 MiB 限制。')
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReaderError('IMAGE_TIMEOUT','抖音图片下载超时。')
                if conn.sock:
                    conn.sock.settimeout(min(10,remaining))
                chunk = res.read1(min(65536,MAX_DOWNLOAD+1-size))
                if not chunk:
                    return b''.join(chunks)
                size += len(chunk)
                if size>MAX_DOWNLOAD:
                    raise ReaderError('IMAGE_TOO_LARGE', '图片下载超过 10 MiB 限制。')
                chunks.append(chunk)
        finally:
            conn.close()
    raise ReaderError('IMAGE_REDIRECT_LIMIT','抖音图片重定向次数过多。')


class DouyinImages(ImageReader):
    def __init__(self, reader, processing_lock=None):
        super().__init__(reader, download=lambda url, _:download_image(url,reader))
        # Share the video processor lock to avoid image decoding alongside ffmpeg.
        self.processing_lock = processing_lock

    def read(self, url, indexes=None):
        acquired = False
        try:
            self.reader.take_call()
            indexes = validate_indexes(indexes)
            if self.processing_lock is not None:
                acquired = self.processing_lock.acquire(blocking=False)
                if not acquired:
                    raise ReaderError('VIDEO_PROCESSOR_BUSY', '已有抖音图片或视频正在处理，请稍后重试。')
            post = self.reader.metadata(url)
            if post.get('type') != 'images':
                raise ReaderError('VIDEO_POST_USE_FRAMES', '这是视频帖，请调用 read_douyin_frames。')
            sources = post.get('_image_sources') or []
            if not sources:
                raise ReaderError('NO_IMAGES','这篇图文帖没有可读取的图片。')
            selected = indexes if indexes is not None else list(range(1,min(2,len(sources))+1))
            if any(i>len(sources) for i in selected):
                raise ReaderError('INDEX_OUT_OF_RANGE','这篇图文帖共有 %s 张图片。' % len(sources))
            parts, summaries, errors = [], [], []
            total = 0
            with self.lock:
                for index in selected:
                    try:
                        candidates = sources[index-1]
                        if not candidates:
                            raise ReaderError('UNSAFE_IMAGE_URL','该图片没有许可 CDN 上的有效地址。')
                        last_error = None
                        for candidate in candidates[:2]:
                            try:
                                candidate = safe_url(candidate, IMAGE_DOMAINS)
                                tiles, cached = self.cached_tiles(candidate)
                                break
                            except ReaderError as error:
                                if error.code not in ('IMAGE_HTTP_ERROR','IMAGE_ACCESS_BLOCKED'):
                                    raise
                                last_error = error
                            except (OSError,http.client.HTTPException):
                                last_error = ReaderError('IMAGE_NETWORK_ERROR','抖音图片下载失败，请稍后重试。')
                        else:
                            raise last_error or ReaderError('NO_IMAGES','图片不可用。')
                        size = sum(len(tile['jpeg']) for tile in tiles)
                        if len(parts)+len(tiles)>MAX_RESULT_PARTS or total+size>MAX_RESULT_BYTES:
                            raise ReaderError('RESPONSE_LIMIT','图片返回超过 24 段或 6 MiB，请减少图片数量。')
                        total += size
                        for number,tile in enumerate(tiles,1):
                            parts.append(dict(tile,index=index,part=number,parts=len(tiles)))
                        summaries.append({'index':index,'parts':len(tiles),'cached':cached,
                                          'original_width':tiles[0]['source_size'][0],
                                          'original_height':tiles[0]['source_size'][1]})
                    except ReaderError as error:
                        errors.append(dict(error_result(error)['error'],index=index))
                    except Exception:
                        errors.append({'index':index,'code':'IMAGE_READ_FAILED','message':'图片处理失败。'})
            result = {'ok':bool(parts),'partial':bool(parts and errors),'type':'images','title':post['title'],
                      'author':post['author'],'image_count':len(sources),'requested_indexes':selected,
                      'returned_indexes':[s['index'] for s in summaries],'returned_parts':len(parts),
                      'images':summaries,'errors':errors,'format':'image/jpeg','max_edge':MAX_EDGE,
                      'jpeg_quality':JPEG_QUALITY,'transcription_status':'not_applicable_image_post'}
            if not parts:
                result['error'] = errors[0] if errors else {'code':'NO_IMAGES','message':'没有可返回的图片。'}
            return result,parts
        except ReaderError as error:
            return error_result(error),[]
        except Exception:
            return error_result(ReaderError('IMAGE_READ_FAILED','抖音图片读取失败，请稍后重试。')),[]
        finally:
            if acquired:
                self.processing_lock.release()
