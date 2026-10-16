"""Tavily：打开网页（extract）、上网找图（search 带图片）。都要 .env 里的 TAVILY_API_KEY。

额度（免费每月 1000）：打开网页每 5 个算 1 次，找一次图算 1 次。xAI 自带的搜索用不了时，
联网搜索也会改用 Tavily（那部分在 search.py 里）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .net import FetchError, HttpClient
from .textutil import clean_text, one_line

log = logging.getLogger(__name__)

TAVILY_API = "https://api.tavily.com"
MAX_IMAGE_RESULTS = 12  # 一次找图最多拿回多少个候选网址
_SAFE_SEARCH = re.compile(r"safe[\s_-]?search", re.I)  # 报错里提到 safe_search / safe search / SafeSearch


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
    """extract 的结果 → 网页正文（markdown）。打不开时抛 TavilyError。"""
    if not isinstance(data, dict):
        raise TavilyError("Tavily 返回的格式不对", transient=True)
    for item in data.get("results") or []:
        if isinstance(item, dict) and isinstance(item.get("raw_content"), str) and item["raw_content"].strip():
            return item["raw_content"]
    for item in data.get("failed_results") or []:
        if isinstance(item, dict) and item.get("error"):
            raise TavilyError(f"打不开（{one_line(str(item['error']), 120)}）")
    raise TavilyError("打不开，没拿到内容")


def parse_images(data: Any) -> list[tuple[str, str]]:
    """search 带图片的结果 → [(图片网址, 说明)]。图片可能是字符串，也可能是 {url, description}。"""
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
    for result in data.get("results") or []:  # 每个网页里的图排在后面
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
            raise TavilyError(f"连不上 Tavily（{exc}）", transient=True) from None
        if status == 200:
            return data
        if status in (401, 403):
            raise TavilyError("Tavily 的 key 不对（检查 .env 里的 TAVILY_API_KEY）", status=status)
        if status in (432, 433):
            raise TavilyError("Tavily 这个月的额度用完了", status=status)
        transient = status == 429 or status >= 500
        raise TavilyError(f"Tavily 出错（HTTP {status}：{_detail(data)}）", transient=transient, status=status)

    async def extract(self, url: str) -> str:
        payload = {"urls": [url], "extract_depth": "basic", "format": "markdown", "timeout": 15}
        return parse_extract(await self._post("/extract", payload, timeout=30), url)

    async def images(self, query: str, *, max_results: int = 5) -> list[tuple[str, str]]:
        payload: dict[str, Any] = {
            "query": query,
            "search_depth": "basic",  # 1 次额度（auto_parameters 可能自动升到 advanced，那样算 2 次）
            "max_results": max_results,
            "include_images": True,
            "include_image_descriptions": True,
            "include_answer": False,
        }
        if self._safe_search:
            payload["safe_search"] = True  # 过滤成人内容
        try:
            data = await self._post("/search", payload, timeout=30)
        except TavilyError as exc:
            if exc.status not in (400, 422) or "safe_search" not in payload or not _SAFE_SEARCH.search(str(exc)):
                raise
            # 有的套餐不支持 safe_search：去掉再试一次，以后都不带（挑图时模型还会再看一遍）
            log.warning("Tavily 不接受 safe_search（%s），改成不带它", exc)
            self._safe_search = False
            payload.pop("safe_search")
            data = await self._post("/search", payload, timeout=30)
        return parse_images(data)
