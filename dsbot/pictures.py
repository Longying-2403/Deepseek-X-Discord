"""上网找图：用 Tavily 搜图片，下载几张候选图（只连公网，见 net.py），缩小后交给模型自己挑。

模型看过候选图、觉得合适才发；都不合适就不发。发出去的一般是原图（作为附件上传）；透明的图铺上白底、
太大的缩小（和模型看到的一致，也不超过 Discord 的上传上限）。
网上找到的动图不要：模型只看得到第一帧，后面的画面审核不了（动图表情包可以放进表情包库）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from .images import is_animated, pixel_count, sniff_mime, to_data_uri
from .models import Picture
from .net import FetchError, HttpClient, host_of
from .tavily import Tavily
from .textutil import one_line

log = logging.getLogger(__name__)

SENDABLE = {"image/png", "image/jpeg", "image/gif", "image/webp"}
WEB_MAX_PIXELS = 16_000_000  # 网上找的图最多这么多像素（4000×4000）：再大的解码要用掉好几百 MB 内存


class PictureFinder:
    def __init__(self, http: HttpClient, tavily: Tavily, *, keep: int = 4, max_bytes: int = 8_000_000, view_side: int = 512):
        self.http = http
        self.tavily = tavily
        self.keep = max(1, keep)
        self.max_bytes = max_bytes
        self.view_side = view_side
        self._decoding = asyncio.Semaphore(2)  # 下载可以同时，解码一次最多两张（省内存）

    async def find(self, query: str) -> list[Picture]:
        """返回最多 keep 张能用的候选图（按搜索结果的顺序）。Tavily 出错时抛 TavilyError。"""
        items = await self.tavily.images(query)
        tried = items[: self.keep * 2]  # 有的图下载不了（防盗链、太大、格式不对），多试几张
        loaded = await asyncio.gather(*(self._load(url, desc) for url, desc in tried))
        out: list[Picture] = []
        seen: set[str] = set()
        for picture in loaded:
            if picture is None:
                continue
            digest = hashlib.sha1(picture.data).hexdigest()
            if digest in seen:  # 不同网站转载的同一张图
                continue
            seen.add(digest)
            out.append(picture)
            if len(out) >= self.keep:
                break
        log.info("找图「%s」：搜到 %d 个网址，能用的 %d 张", one_line(query, 40), len(items), len(out))
        return out

    async def _load(self, url: str, description: str) -> Picture | None:
        try:
            raw, _ = await self.http.fetch(url, max_bytes=self.max_bytes)
        except FetchError as exc:
            log.debug("候选图 %s 下载失败：%s", host_of(url), exc)
            return None
        mime = sniff_mime(raw)
        if mime not in SENDABLE:
            return None
        async with self._decoding:
            prepared = await asyncio.to_thread(self._prepare, raw, mime)
        if prepared is None:
            return None
        data, mime, uri = prepared
        return Picture(data, mime, uri, description=one_line(description, 120), host=host_of(url))

    def _prepare(self, raw: bytes, mime: str) -> tuple[bytes, str, str] | None:
        """(发出去的图, 类型, 给模型看的缩小版)。动图、太大、坏掉的图返回 None。"""
        pixels = pixel_count(raw)
        if pixels is None or pixels > WEB_MAX_PIXELS:  # 先看尺寸（只读文件头），太大的不解码
            return None
        if is_animated(raw):
            return None
        # 给模型看的缩小版透明的地方铺白底；真要发的时候（模型挑中了）再照样铺白底，见 images.upload_ready
        uri = to_data_uri(raw, self.view_side, max_pixels=WEB_MAX_PIXELS)
        if uri is None:
            return None
        return raw, mime, uri
