"""Finding pictures online: search for images with Tavily, download a few candidates (public addresses only, see net.py), shrink them and let the model pick.

The model only sends one after looking at the candidates and finding one that fits; if none fit, nothing is sent. What gets sent is usually the
original (uploaded as an attachment); transparent pictures get a white background and oversized ones are shrunk (matching what the model saw,
and staying under Discord's upload limit).
Animated pictures found online are skipped: the model only sees the first frame and can't vet the rest (animated memes can go in the meme library).
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
WEB_MAX_PIXELS = 16_000_000  # max pixels for pictures from the web (4000×4000): decoding anything bigger takes hundreds of MB of memory


class PictureFinder:
    def __init__(self, http: HttpClient, tavily: Tavily, *, keep: int = 4, max_bytes: int = 8_000_000, view_side: int = 512):
        self.http = http
        self.tavily = tavily
        self.keep = max(1, keep)
        self.max_bytes = max_bytes
        self.view_side = view_side
        self._decoding = asyncio.Semaphore(2)  # downloads can run in parallel, but at most two are decoded at once (to save memory)

    async def find(self, query: str) -> list[Picture]:
        """Return at most keep usable candidates (in search-result order). Raises TavilyError when Tavily fails."""
        items = await self.tavily.images(query)
        tried = items[: self.keep * 2]  # some pictures can't be downloaded (hotlink protection, too big, wrong format), so try a few extra
        loaded = await asyncio.gather(*(self._load(url, desc) for url, desc in tried))
        out: list[Picture] = []
        seen: set[str] = set()
        for picture in loaded:
            if picture is None:
                continue
            digest = hashlib.sha1(picture.data).hexdigest()
            if digest in seen:  # the same picture reposted on different sites
                continue
            seen.add(digest)
            out.append(picture)
            if len(out) >= self.keep:
                break
        log.info('Image search "%s": %d URLs found, %d usable', one_line(query, 40), len(items), len(out))
        return out

    async def _load(self, url: str, description: str) -> Picture | None:
        try:
            raw, _ = await self.http.fetch(url, max_bytes=self.max_bytes)
        except FetchError as exc:
            log.debug("Downloading a candidate from %s failed: %s", host_of(url), exc)
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
        """(picture to send, type, downsized version for the model). None for animated, oversized or broken pictures."""
        pixels = pixel_count(raw)
        if pixels is None or pixels > WEB_MAX_PIXELS:  # check the size first (header only), and don't decode anything too big
            return None
        if is_animated(raw):
            return None
        # the downsized version for the model gets a white background where it's transparent; when it's actually sent (the model picked it), it gets the same white background, see images.upload_ready
        uri = to_data_uri(raw, self.view_side, max_pixels=WEB_MAX_PIXELS)
        if uri is None:
            return None
        return raw, mime, uri
