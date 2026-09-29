"""Safe, dependency-free Xiaohongshu page reader.

The parser in this module was independently implemented for this repository.
It extracts structured state embedded by Xiaohongshu and never evaluates
JavaScript.
"""
import collections
import copy
import datetime
import html.parser
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
import urllib.parse

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
)
PAGE_DOMAINS = ("xhslink.com", "xhslink.cn", "xiaohongshu.com")
MAX_PAGE_BYTES = 5 * 1024 * 1024


class ReaderError(Exception):
    def __init__(self, code, message, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def error_result(error):
    result = {"ok": False, "error": {"code": error.code, "message": str(error)}}
    if error.retry_after is not None:
        result["error"]["retry_after_seconds"] = error.retry_after
    return result


class SlidingLimit:
    def __init__(self, limit=5, period=60, clock=time.monotonic):
        self.limit = limit
        self.period = period
        self.clock = clock
        self.events = collections.deque()
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            now = self.clock()
            while self.events and self.events[0] <= now - self.period:
                self.events.popleft()
            if len(self.events) >= self.limit:
                retry = max(1, math.ceil(self.period - (now - self.events[0])))
                raise ReaderError("RATE_LIMITED", "请求过于频繁，请稍后重试。", retry)
            self.events.append(now)


def _allowed_host(host, domains):
    return bool(host) and host.isascii() and any(
        host == domain or host.endswith("." + domain) for domain in domains
    )


def validate_url(url, domains=PAGE_DOMAINS):
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        raise ReaderError("INVALID_URL", "请提供完整、有效的链接。")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme not in ("http", "https")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 80 if parsed.scheme == "http" else 443)
            or not _allowed_host(host, domains)
        ):
            raise ValueError()
    except (TypeError, ValueError):
        raise ReaderError("INVALID_URL", "链接域名或格式不受支持。")
    return urllib.parse.urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def public_addresses(host):
    records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ReaderError("UNSAFE_ADDRESS", "目标域名解析到非公网地址。")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated IP while keeping hostname TLS verification."""

    def __init__(self, host, address, timeout=12):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def request_page(url, user_agent, limiter):
    current = validate_url(url)
    for _ in range(5):
        parsed = urllib.parse.urlsplit(current)
        if any(value in parsed.path.lower() for value in ("captcha", "/login", "/verify", "/security")):
            raise ReaderError("ACCESS_BLOCKED", "链接跳转到了登录或验证页面。")
        addresses = public_addresses(parsed.hostname)
        limiter.take()
        connection = PinnedHTTPSConnection(parsed.hostname, addresses[0])
        try:
            path = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            location = response.getheader("Location")
            if response.status in (301, 302, 303, 307, 308) and location:
                current = validate_url(urllib.parse.urljoin(current, location))
                continue
            body = response.read(MAX_PAGE_BYTES + 1)
            if len(body) > MAX_PAGE_BYTES:
                raise ReaderError("RESPONSE_TOO_LARGE", "页面超过大小限制。")
            return response.status, current, body.decode("utf-8", errors="replace")
        finally:
            connection.close()
    raise ReaderError("TOO_MANY_REDIRECTS", "链接重定向次数过多。")


class _VisibleText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def _replace_undefined(source):
    """Replace bare JS undefined tokens without touching quoted strings."""
    output = []
    index = 0
    quote = None
    escaped = False
    while index < len(source):
        char = source[index]
        if quote:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
            output.append(char)
            index += 1
            continue
        if source.startswith("undefined", index):
            before = source[index - 1] if index else ""
            after_pos = index + 9
            after = source[after_pos] if after_pos < len(source) else ""
            if not (before.isalnum() or before in "_$" or after.isalnum() or after in "_$"):
                output.append("null")
                index = after_pos
                continue
        output.append(char)
        index += 1
    return "".join(output)


def extract_initial_state(page):
    marker = re.search(r"(?:window\.)?__INITIAL_STATE__\s*=\s*", page)
    if not marker:
        return None
    source = _replace_undefined(page[marker.end() :])
    try:
        value, _ = json.JSONDecoder().raw_decode(source)
        return value if isinstance(value, dict) else None
    except (ValueError, RecursionError):
        return None


def _dictionary(value):
    return value if isinstance(value, dict) else {}


def _expected_note_id(url):
    path = urllib.parse.urlsplit(url).path
    match = re.search(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})(?:/|$)", path)
    return match.group(1).lower() if match else None


def select_note(state, url):
    """Select only known note containers and verify the requested note ID."""
    expected = _expected_note_id(url)
    state = _dictionary(state)

    mobile = _dictionary(_dictionary(state.get("noteData")).get("data")).get("noteData")
    if isinstance(mobile, dict) and (mobile.get("title") or mobile.get("desc")):
        note_id = str(mobile.get("noteId") or "").lower()
        if expected and note_id and note_id != expected:
            return None
        return mobile

    detail_map = _dictionary(_dictionary(state.get("note")).get("noteDetailMap"))
    if expected:
        selected = _dictionary(detail_map.get(expected)).get("note")
        return selected if isinstance(selected, dict) else None
    if len(detail_map) == 1:
        selected = _dictionary(next(iter(detail_map.values()))).get("note")
        return selected if isinstance(selected, dict) else None
    return None


def _time_iso(value):
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        if number > 100_000_000_000:
            number /= 1000
        timezone = datetime.timezone(datetime.timedelta(hours=8))
        return datetime.datetime.fromtimestamp(number, timezone).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _metric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9,]+", value):
        return int(value.replace(",", ""))
    return value if isinstance(value, str) and value else None


def _image_urls(note):
    result = []
    for image in note.get("imageList") or []:
        image = _dictionary(image)
        candidate = image.get("urlDefault") or image.get("url")
        if not candidate:
            for info in image.get("infoList") or []:
                if isinstance(info, dict) and info.get("url"):
                    candidate = info["url"]
                    break
        if isinstance(candidate, str) and candidate.startswith(("https://", "http://")) and candidate not in result:
            result.append(candidate)
    return result


def _clean_body(value):
    body = str(value or "")
    body = re.sub(r"#([^#\r\n]+?)\[话题\]#", r"#\1", body)
    return body.replace("[话题]", "").strip()


def normalize_note(note, url):
    user = _dictionary(note.get("user"))
    stats = _dictionary(note.get("interactInfo"))
    images = _image_urls(note)
    return {
        "ok": True,
        "url": url,
        "title": str(note.get("title") or ""),
        "body": _clean_body(note.get("desc")),
        "author": user.get("nickname") or user.get("nickName") or None,
        "author_id": user.get("userId"),
        "tags": [tag["name"] for tag in note.get("tagList") or [] if isinstance(tag, dict) and tag.get("name")],
        "interactions": {
            name: _metric(stats.get(field))
            for name, field in (
                ("likes", "likedCount"),
                ("comments", "commentCount"),
                ("collects", "collectedCount"),
                ("shares", "shareCount"),
            )
        },
        "images": images,
        "image_count": len(images),
        "is_video": note.get("type") == "video",
        "type": note.get("type"),
        "published_at": _time_iso(note.get("time")),
        "published_at_raw": note.get("time"),
        "updated_at": _time_iso(note.get("lastUpdateTime")),
    }


def parse_xhs_page(page, url, status=200):
    visible_parser = _VisibleText()
    visible_parser.feed(page)
    visible = " ".join(visible_parser.parts)
    if status in (401, 403, 429, 461, 471):
        raise ReaderError("ACCESS_BLOCKED", "小红书拒绝访问，可能需要登录或验证。")
    if status != 200:
        raise ReaderError("UPSTREAM_HTTP_ERROR", "小红书返回 HTTP %s。" % status)
    note = select_note(extract_initial_state(page), url)
    if isinstance(note, dict) and (note.get("title") or note.get("desc")):
        stub = (str(note.get("title") or "") + " " + str(note.get("desc") or "")).strip()
        if len(stub) < 100 and any(text in stub for text in ("请登录", "登录后查看", "安全验证")):
            raise ReaderError("LOGIN_OR_CAPTCHA_REQUIRED", "页面只有登录或验证提示。")
        return normalize_note(note, url)
    if any(text in visible for text in ("验证码", "安全验证", "访问异常", "访问频繁", "异常流量", "Access Denied")):
        raise ReaderError("CAPTCHA_OR_RISK_CONTROL", "小红书返回验证码或风控页面。")
    if any(text in visible for text in ("请登录", "登录后查看", "扫码登录", "登录即可")):
        raise ReaderError("LOGIN_REQUIRED", "小红书要求登录。")
    raise ReaderError("POST_DATA_UNAVAILABLE", "页面没有可用帖子数据。")


class XhsReader:
    def __init__(self, fetch=request_page, clock=time.monotonic, call_limit=5, outbound_limit=5):
        self.fetch = fetch
        self.clock = clock
        self.calls = SlidingLimit(call_limit, clock=clock)
        self.outbound = SlidingLimit(outbound_limit, clock=clock)
        self.cache = collections.OrderedDict()
        self.lock = threading.Lock()

    def read(self, url):
        try:
            self.calls.take()
            normalized = validate_url(url)
            with self.lock:
                now = self.clock()
                for key in list(self.cache):
                    if self.cache[key][0] <= now:
                        del self.cache[key]
                if normalized in self.cache:
                    result = copy.deepcopy(self.cache[normalized][1])
                    result["cached"] = True
                    return result
                network_failed = True
                for user_agent in (MOBILE_UA, DESKTOP_UA):
                    try:
                        status, final_url, page = self.fetch(normalized, user_agent, self.outbound)
                        network_failed = False
                        result = parse_xhs_page(page, final_url, status)
                        result.update(cached=False, fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                        self.cache[normalized] = (self.clock() + 600, copy.deepcopy(result))
                        while len(self.cache) > 128:
                            self.cache.popitem(last=False)
                        return result
                    except (OSError, http.client.HTTPException):
                        continue
                if network_failed:
                    raise ReaderError("NETWORK_ERROR", "所有页面请求均失败。")
                raise ReaderError("POST_DATA_UNAVAILABLE", "没有取得帖子数据。")
        except ReaderError as error:
            return error_result(error)
        except Exception:
            return error_result(ReaderError("READ_FAILED", "读取失败：页面格式异常或服务暂时不可用。"))


# Backward-friendly alias used by the other modules in this repository.
Reader = XhsReader
