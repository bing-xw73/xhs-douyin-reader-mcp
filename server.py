"""Streamable HTTP MCP server for public Xiaohongshu and Douyin posts."""
import base64
import json
import os
import re
from typing import List, Optional

import anyio
import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import StrictBool, StrictInt

from douyin_frames import DouyinFrames
from douyin_images import DouyinImages
from douyin_reader import DouyinReader
from xhs_images import ImageReader
from xhs_reader import XhsReader

load_dotenv()


def _csv_env(name, default):
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


secret = os.getenv("MCP_SECRET", "")
if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", secret):
    raise RuntimeError("MCP_SECRET must be 43-128 URL-safe characters")

bind_host = os.getenv("MCP_BIND_HOST", "localhost")
bind_port = int(os.getenv("MCP_PORT", "18120"))
public_host = os.getenv("MCP_PUBLIC_HOST", "reader.example.com")
allowed_hosts = _csv_env("MCP_ALLOWED_HOSTS", public_host + ",localhost:%s" % bind_port)
allowed_origins = _csv_env(
    "MCP_ALLOWED_ORIGINS",
    "https://claude.ai,https://chatgpt.com,https://" + public_host,
)

xhs_reader = XhsReader()
xhs_images = ImageReader(xhs_reader)
douyin_reader = DouyinReader()
douyin_frames = DouyinFrames(douyin_reader)
douyin_images = DouyinImages(douyin_reader, processing_lock=douyin_frames.lock)

mcp = FastMCP(
    "XHS and Douyin Reader",
    host=bind_host,
    port=bind_port,
    streamable_http_path="/" + secret + "/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    ),
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
async def read_xhs_post(url: str) -> dict:
    """读取公开小红书帖子元数据和正文，不下载图片。"""
    return await anyio.to_thread.run_sync(xhs_reader.read, url)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
    structured_output=False,
)
async def read_xhs_images(url: str, indexes: Optional[List[StrictInt]] = None) -> CallToolResult:
    """按需读取小红书图片。序号从 1 开始，默认前 2 张，最多 4 张。"""
    summary, parts = await anyio.to_thread.run_sync(xhs_images.read, url, indexes)
    content = [TextContent(type="text", text=json.dumps(summary, ensure_ascii=False))]
    for part in parts:
        content.append(
            TextContent(
                type="text",
                text="原帖第 %s 张图片，第 %s/%s 段" % (part["index"], part["part"], part["parts"]),
            )
        )
        content.append(
            ImageContent(
                type="image",
                mimeType="image/jpeg",
                data=base64.b64encode(part["jpeg"]).decode("ascii"),
            )
        )
    return CallToolResult(content=content, structuredContent=summary, isError=not summary["ok"])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
async def read_douyin_video(url: str) -> dict:
    """读取公开抖音帖子元数据，并识别视频或图文类型。"""
    return await anyio.to_thread.run_sync(douyin_reader.read, url)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
    structured_output=False,
)
async def read_douyin_frames(
    url: str, count: StrictInt = 4, transcribe: StrictBool = False
) -> CallToolResult:
    """均匀抽取抖音视频帧；可选使用 SenseVoiceSmall 转写音频。"""
    summary, parts = await anyio.to_thread.run_sync(douyin_frames.read, url, count, transcribe)
    content = [TextContent(type="text", text=json.dumps(summary, ensure_ascii=False))]
    for part in parts:
        content.append(
            TextContent(
                type="text",
                text="第 %s 帧，%.3f 秒，第 %s/%s 段"
                % (part["frame"], part["timestamp_seconds"], part["part"], part["parts"]),
            )
        )
        content.append(
            ImageContent(
                type="image",
                mimeType="image/jpeg",
                data=base64.b64encode(part["jpeg"]).decode("ascii"),
            )
        )
    return CallToolResult(content=content, structuredContent=summary, isError=not summary["ok"])


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
    structured_output=False,
)
async def read_douyin_images(url: str, indexes: Optional[List[StrictInt]] = None) -> CallToolResult:
    """按需读取抖音图文帖图片。默认前 2 张，最多 4 张。"""
    summary, parts = await anyio.to_thread.run_sync(douyin_images.read, url, indexes)
    content = [TextContent(type="text", text=json.dumps(summary, ensure_ascii=False))]
    for part in parts:
        content.append(
            TextContent(
                type="text",
                text="原帖第 %s 张图片，第 %s/%s 段" % (part["index"], part["part"], part["parts"]),
            )
        )
        content.append(
            ImageContent(
                type="image",
                mimeType="image/jpeg",
                data=base64.b64encode(part["jpeg"]).decode("ascii"),
            )
        )
    return CallToolResult(content=content, structuredContent=summary, isError=not summary["ok"])


if __name__ == "__main__":
    uvicorn.run(
        mcp.streamable_http_app(),
        host=bind_host,
        port=bind_port,
        workers=1,
        access_log=False,
        log_level=os.getenv("LOG_LEVEL", "warning"),
        proxy_headers=False,
        limit_concurrency=20,
    )
