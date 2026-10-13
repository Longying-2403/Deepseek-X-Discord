"""Tavily: opening web pages (extract) and finding pictures online (search with images). Both need TAVILY_API_KEY in .env.

Credits (1,000 a month for free): every 5 web pages opened count as 1, and each image search counts as 1. When DeepSeek's built-in search
isn't available, web search also switches to Tavily (that part lives in search.py).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .net import FetchError, HttpClient
from .textutil import clean_text, one_line

log = logging.getLogger(__name__)

TAVILY_API = "https://api.tavily.com"
MAX_IMAGE_RESULTS = 12  # max candidate URLs taken from one image search
_SAFE_SEARCH = re.compile(r"safe[\s_-]?search", re.I)  # the error mentions safe_search / safe search / SafeSearch


class TavilyError(Exception):
    def __init__(self, message: str, *, transient: bool = False, status: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.status = status


def _detail(data: Any) -> str:
    if isinstance(data, dict):
        detail = data.get("detail", data.get("error", data.get("message")))
        if isinstance(detail, dict):
            detail = detail.get("error") or detail.get("message") or detail
        if detail:
            return one_line(str(detail), 200)
    return one_line(str(data), 200)


def parse_extract(data: Any, url: str) -> str:
    """An extract result → the page's text (markdown). Raises TavilyError when it couldn't be opened."""
    if not isinstance(data, dict):
        raise TavilyError("Tavily returned an unexpected format", transient=True)
    for item in data.get("results") or []:
        if isinstance(item, dict) and isinstance(item.get("raw_content"), str) and item["raw_content"].strip():
            return item["raw_content"]
    for item in data.get("failed_results") or []:
        if isinstance(item, dict) and item.get("error"):
            raise TavilyError(f"couldn't open it ({one_line(str(item['error']), 120)})")
    raise TavilyError("couldn't open it; no content came back")


def parse_images(data: Any) -> list[tuple[str, str]]:
    """A search-with-images result → [(image URL, description)]. An image may be a string or {url, description}."""
    if not isinstance(data, dict):
        return []
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(item: Any) -> None:
        if isinstance(item, str):
            url, desc = item, ""
        elif isinstance(item, dict):
            url, desc = item.get("url"), item.get("description") or ""
        else:
            return
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or url in seen:
            return
        if any(c.isspace() or ord(c) < 32 for c in url) or url.lower().split("?")[0].endswith(".svg"):
            return
        seen.add(url)
        found.append((url, clean_text(one_line(str(desc), 160))))

    for item in data.get("images") or []:
        add(item)
    for result in data.get("results") or []:  # the images from each page come after
        if isinstance(result, dict):
            for item in result.get("images") or []:
                add(item)
    return found[:MAX_IMAGE_RESULTS]


class Tavily:
    def __init__(self, http: HttpClient, api_key: str):
        self.http = http
        self.api_key = api_key
        self._safe_search = True

    async def _post(self, path: str, payload: dict[str, Any], timeout: float) -> Any:
        try:
            status, data = await self.http.api(
                "POST", TAVILY_API + path, payload=payload, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=timeout
            )
        except FetchError as exc:
            raise TavilyError(f"can't reach Tavily ({exc})", transient=True) from None
        if status == 200:
            return data
        if status in (401, 403):
            raise TavilyError("Tavily's key is wrong (check TAVILY_API_KEY in .env)", status=status)
        if status in (432, 433):
            raise TavilyError("Tavily's credits for this month are used up", status=status)
        transient = status == 429 or status >= 500
        raise TavilyError(f"Tavily error (HTTP {status}: {_detail(data)})", transient=transient, status=status)

    async def extract(self, url: str) -> str:
        payload = {"urls": [url], "extract_depth": "basic", "format": "markdown", "timeout": 15}
        return parse_extract(await self._post("/extract", payload, timeout=30), url)

    async def images(self, query: str, *, max_results: int = 5) -> list[tuple[str, str]]:
        payload: dict[str, Any] = {
            "query": query,
            "search_depth": "basic",  # 1 credit (auto_parameters might bump it up to advanced, which counts as 2)
            "max_results": max_results,
            "include_images": True,
            "include_image_descriptions": True,
            "include_answer": False,
        }
        if self._safe_search:
            payload["safe_search"] = True  # filter out adult content
        try:
            data = await self._post("/search", payload, timeout=30)
        except TavilyError as exc:
            if exc.status not in (400, 422) or "safe_search" not in payload or not _SAFE_SEARCH.search(str(exc)):
                raise
            # some plans don't support safe_search: drop it, retry, and leave it off from then on (the model still looks at the pictures when picking)
            log.warning("Tavily doesn't accept safe_search (%s); continuing without it", exc)
            self._safe_search = False
            payload.pop("safe_search")
            data = await self._post("/search", payload, timeout=30)
        return parse_images(data)
