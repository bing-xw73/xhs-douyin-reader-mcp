"""Bounded temporary video processing; only JPEGs and transcript remain in RAM."""
import collections
import copy
import json
import math
import os
import pathlib
import subprocess
import tempfile
import threading
import time

import httpx
from douyin_reader import MAX_DURATION, MAX_VIDEO_BYTES, post_url
from xhs_images import jpeg_tiles, MAX_RESULT_BYTES, MAX_RESULT_PARTS
from xhs_reader import ReaderError, error_result

ASR_URL = 'https://api.siliconflow.cn/v1/audio/transcriptions'
ASR_MODEL = 'FunAudioLLM/SenseVoiceSmall'
MAX_CACHE_BYTES = 12 * 1024 * 1024


def options(count, transcribe):
    if type(count) is not int or not 1 <= count <= 8:
        raise ReaderError('INVALID_COUNT', 'count 必须是 1 至 8 的整数，默认 4。')
    if type(transcribe) is not bool:
        raise ReaderError('INVALID_TRANSCRIBE', 'transcribe 必须是 true 或 false，默认 false。')


def remaining(deadline, maximum):
    value = min(maximum, deadline - time.monotonic())
    if value <= 0:
        raise ReaderError('PROCESSING_TIMEOUT', '视频处理超时，请减少帧数后重试。')
    return value


def run_media(command, deadline, timeout=25):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=remaining(deadline, timeout),
                                check=False, env={k:v for k,v in os.environ.items() if k not in ('SILICONFLOW_API_KEY','XHS_MCP_SECRET')})
    except subprocess.TimeoutExpired:
        raise ReaderError('PROCESSING_TIMEOUT', 'ffmpeg/ffprobe 超时，临时文件已安排清理。')
    except OSError:
        raise ReaderError('FFMPEG_UNAVAILABLE', 'ffmpeg 或 ffprobe 无法运行。')
    if result.returncode != 0:
        raise ReaderError('MEDIA_PROCESSING_FAILED', '视频损坏、不受支持或处理资源不足。')
    return result.stdout


def probe_file(path, deadline):
    data = json.loads(run_media(['ffprobe','-v','error','-threads','1','-protocol_whitelist','file,pipe',
                                '-show_entries','format=duration:stream=codec_type,width,height,duration',
                                '-of','json',str(path)],deadline,15))
    streams = data.get('streams') or []
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    if not video:
        raise ReaderError('NO_VIDEO_STREAM', '文件中没有视频流。')
    duration = float((data.get('format') or {}).get('duration') or video.get('duration') or 0)
    durations = [duration] + [float(s.get('duration') or 0) for s in streams]
    if any(not math.isfinite(d) or d > MAX_DURATION for d in durations) or duration <= 0:
        raise ReaderError('VIDEO_DURATION_LIMIT', '只处理时长不超过 5 分钟且时长可验证的视频。')
    width, height = int(video.get('width') or 0), int(video.get('height') or 0)
    if min(width,height) <= 0 or width * height > 1920 * 1080:
        raise ReaderError('VIDEO_RESOLUTION_LIMIT', '当前服务内存限制下仅处理不超过 1080p 像素量的视频。')
    return duration, any(s.get('codec_type') == 'audio' for s in streams)


def transcribe_audio(path, deadline):
    key = os.environ.get('SILICONFLOW_API_KEY')
    if not key:
        raise ReaderError('ASR_NOT_CONFIGURED', '语音识别 API key 尚未配置。')
    try:
        budget = remaining(deadline, 65)
        with path.open('rb') as audio, httpx.Client(timeout=httpx.Timeout(budget,connect=min(10,budget)),
                                                  follow_redirects=False,trust_env=False) as client:
            with client.stream('POST', ASR_URL, headers={'Authorization':'Bearer '+key},
                               data={'model':ASR_MODEL}, files={'file':('audio.wav',audio,'audio/wav')}) as response:
                if response.status_code != 200:
                    raise ReaderError('ASR_HTTP_ERROR', '硅基流动语音识别返回 HTTP %s。' % response.status_code)
                content = bytearray()
                for chunk in response.iter_bytes():
                    remaining(deadline,65)
                    content.extend(chunk)
                    if len(content) > 1024 * 1024:
                        raise ReaderError('ASR_RESPONSE_TOO_LARGE', '语音识别响应过大。')
                value = json.loads(content)
                text = value.get('text')
                if not isinstance(text,str):
                    raise ReaderError('ASR_INVALID_RESPONSE', '语音识别未返回有效文本。')
                if len(text) > 64000:
                    raise ReaderError('ASR_RESPONSE_TOO_LARGE', '转写文本超过 64000 字符限制。')
                return text.strip()
    except ReaderError:
        raise
    except httpx.TimeoutException:
        raise ReaderError('ASR_TIMEOUT', '硅基流动语音识别请求超时，请稍后重试。')
    except Exception as error:
        # Only an exception class name is exposed; never exception text, headers or key.
        raise ReaderError('ASR_REQUEST_FAILED', '语音识别请求失败（%s）；未返回上游错误内容以保护凭据。' % type(error).__name__)


class DouyinFrames:
    def __init__(self, reader):
        self.reader = reader
        self.lock = threading.Lock()
        self.cache = collections.OrderedDict()
        self.transcripts = collections.OrderedDict()
        self.cache_bytes = 0

    def download(self, metadata, path, deadline):
        duration = metadata.get('duration_seconds')
        if duration is None or not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
            raise ReaderError('VIDEO_DURATION_LIMIT', '只下载时长可验证且不超过 5 分钟的视频。')
        size = metadata.get('size_bytes')
        if size is not None and size > MAX_VIDEO_BYTES:
            raise ReaderError('VIDEO_SIZE_LIMIT', '视频文件超过 100 MB 限制。')
        urls = metadata.get('_play_urls') or []
        for url in urls[:2]:
            conn = None
            try:
                conn, res = self.reader.open_media(url, timeout=remaining(deadline,12))
                if res.status != 200:
                    continue
                length = res.getheader('Content-Length') or ''
                expected = int(length) if length.isdigit() else None
                if expected is not None and expected > MAX_VIDEO_BYTES:
                    raise ReaderError('VIDEO_SIZE_LIMIT', '视频文件超过 100 MB 限制。')
                total = 0
                prefix = bytearray()
                with path.open('wb') as output:
                    while True:
                        timeout = remaining(deadline,12)
                        if conn.sock:
                            conn.sock.settimeout(timeout)
                        chunk = res.read1(min(65536, MAX_VIDEO_BYTES + 1 - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_VIDEO_BYTES:
                            raise ReaderError('VIDEO_SIZE_LIMIT', '视频下载超过 100 MB，已中止。')
                        if len(prefix) < 12:
                            prefix.extend(chunk[:12-len(prefix)])
                        if len(prefix) >= 12 and prefix[4:8] != b'ftyp':
                            raise ReaderError('INVALID_VIDEO_FILE', '下载结果不是 MP4 文件。')
                        output.write(chunk)
                if total < 12 or (expected is not None and total != expected):
                    raise ReaderError('INCOMPLETE_DOWNLOAD', '视频下载不完整。')
                return total
            except ReaderError:
                raise
            except Exception:
                continue
            finally:
                if conn:
                    conn.close()
        raise ReaderError('VIDEO_DOWNLOAD_FAILED', 'VPS 无法下载视频，可能是播放地址过期或 CDN 拒绝访问。')

    def process(self, metadata, folder, count, transcribe, deadline):
        if metadata.get('type') == 'images':
            raise ReaderError('IMAGE_POST_USE_IMAGES', '这是图文帖，请调用 read_douyin_images；不会下载或转写背景音乐。')
        video = folder / 'video.mp4'
        size = self.download(metadata, video, min(deadline, time.monotonic()+45))
        duration, has_audio = probe_file(video, deadline)
        parts, timestamps, total = [], [], 0
        for index in range(count):
            timestamp = duration * (index + 0.5) / count
            timestamps.append(round(timestamp,3))
            target = folder / 'frame.png'
            run_media(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y',
                       '-threads','1','-filter_threads','1','-protocol_whitelist','file,pipe',
                       '-ss',str(timestamp),'-i',str(video),'-map','0:v:0','-frames:v','1',
                       '-an','-sn','-dn','-threads','1',str(target)],deadline)
            if target.stat().st_size > 10*1024*1024:
                raise ReaderError('FRAME_TOO_LARGE','视频帧超过图片大小限制。')
            tiles = jpeg_tiles(target.read_bytes())
            target.unlink()
            for number, tile in enumerate(tiles,1):
                total += len(tile['jpeg'])
                parts.append(dict(tile,frame=index+1,timestamp_seconds=round(timestamp,3),part=number,parts=len(tiles)))
                if total > MAX_RESULT_BYTES or len(parts) > MAX_RESULT_PARTS:
                    raise ReaderError('FRAME_RESULT_LIMIT','返回图片过多或超过 6 MiB，请减少 count。')
        summary = {'ok':True,'title':metadata['title'],'author':metadata['author'],
                   'video_id':metadata['video_id'],'duration_seconds':duration,'downloaded_bytes':size,
                   'requested_count':count,'frame_count':count,'image_parts':len(parts),
                   'timestamps_seconds':timestamps,'transcribe_requested':transcribe,
                   'transcript':None,'transcription_status':'not_requested','cached':False,
                   'temporary_files_deleted':False,'partial':False}
        if transcribe:
            try:
                if not has_audio:
                    raise ReaderError('NO_AUDIO_STREAM','视频没有音轨。')
                transcript_key = metadata['video_id']
                cached = self.transcripts.get(transcript_key)
                if cached and cached[0] > time.monotonic():
                    text = cached[1]
                    summary['transcription_cached'] = True
                else:
                    audio = folder / 'audio.wav'
                    run_media(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y',
                               '-threads','1','-protocol_whitelist','file,pipe','-i',str(video),
                               '-map','0:a:0','-vn','-sn','-dn','-ac','1','-ar','16000',
                               '-c:a','pcm_s16le','-threads','1',str(audio)],deadline)
                    if audio.stat().st_size > 10_000_000:
                        raise ReaderError('AUDIO_SIZE_LIMIT','提取的音频超过大小限制。')
                    text = transcribe_audio(audio,deadline)
                    audio.unlink()
                    self.transcripts[transcript_key] = (time.monotonic()+600,text)
                    while len(self.transcripts)>32:
                        self.transcripts.popitem(last=False)
                summary.update(transcript=text,transcription_status='complete',transcription_model=ASR_MODEL)
            except ReaderError as error:
                summary.update(partial=True,transcription_status='failed',transcription_error=error_result(error)['error'])
        return summary, parts

    def read(self, url, count=4, transcribe=False):
        acquired = False
        try:
            self.reader.take_call()
            options(count,transcribe)
            url = post_url(url)
            acquired = self.lock.acquire(blocking=False)
            if not acquired:
                raise ReaderError('VIDEO_PROCESSOR_BUSY','已有视频正在处理，请稍后再试。')
            now = time.monotonic()
            for key in list(self.cache):
                if self.cache[key][0] <= now:
                    self.cache_bytes -= self.cache.pop(key)[3]
            cache_key = (url,count,transcribe)
            if cache_key in self.cache:
                _, summary, parts, _ = self.cache[cache_key]
                summary = copy.deepcopy(summary)
                summary['cached'] = True
                return summary, parts
            deadline = time.monotonic()+105
            metadata = self.reader.metadata(url)
            if metadata.get('type') == 'images':
                raise ReaderError('IMAGE_POST_USE_IMAGES', '这是图文帖，请调用 read_douyin_images；不会下载或转写背景音乐。')
            for attempt in range(2):
                try:
                    with tempfile.TemporaryDirectory(prefix='douyin-mcp-') as tmp:
                        summary, parts = self.process(metadata,pathlib.Path(tmp),count,transcribe,deadline)
                    summary['temporary_files_deleted'] = True
                    break
                except ReaderError as error:
                    if attempt or error.code not in ('VIDEO_DOWNLOAD_FAILED','INCOMPLETE_DOWNLOAD'):
                        raise
                    metadata = self.reader.metadata(url,force=True)
            if not summary['partial']:
                size = sum(len(p['jpeg']) for p in parts)
                self.cache[cache_key] = (time.monotonic()+600,copy.deepcopy(summary),parts,size)
                self.cache_bytes += size
                while self.cache_bytes > MAX_CACHE_BYTES:
                    self.cache_bytes -= self.cache.popitem(last=False)[1][3]
            return summary, parts
        except ReaderError as error:
            return error_result(error), []
        except Exception:
            return error_result(ReaderError('VIDEO_PROCESSING_FAILED','视频处理失败，临时文件已清理。')), []
        finally:
            if acquired:
                self.lock.release()
