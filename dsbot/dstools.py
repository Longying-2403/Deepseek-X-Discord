"""DeepSeek 的 Search / Fetch 接口（2026-10-17 上线）：上网搜、打开网页、按歌名找歌都先用它，不行了再用 Tavily。

- Search：POST {base_url}/v1/tools/search，一次拿回几条结果（标题、网址、摘要、日期），通常不到 1 秒。
  query 里可以写 site:、-site:、"整句"、after: 这些搜索语法。
- Fetch：POST {base_url}/v1/tools/fetch，把一个网页转成 markdown。网页由 DeepSeek 的服务器去读，NAS 自己不打开，
  家里的内网和 IP 都不会暴露给群友发的链接（和交给 Tavily 一样）。PDF、要登录才能看的网站多半读不了。
- 用的是 .env 里同一个 DEEPSEEK_API_KEY。按次计费：搜到结果、读到内容才算钱，失败、没结果、超时都不算。
  每次多少钱见 config.toml 的 [pricing]。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .net import FetchError, HttpClient
from .textutil import clean_text, one_line

log = logging.getLogger(__name__)

MAX_QUERY = 128  # Search 的 query 最多 128 个字
SEARCH_TIMEOUT = 15.0  # 平时不到 1 秒
FETCH_TIMEOUT = 25.0  # 大网页要好几秒
_RETRY = (408, 429, 500, 502, 503, 504)  # 官方建议等一下再试的
_OURS = (401, 402)  # key 不对、余额不够：是这边的问题，要你处理


class ToolError(Exception):
    """没搜成 / 没读成。status：HTTP 状态码（连不上、超时是 None）；transient：暂时的，过一会儿可以再试；
    ours：这边的问题（key 不对、余额不够、接口地址不对），不是对方网站的问题，修好了就能用（日志要提醒你处理）。"""

    def __init__(self, message: str, *, status: int | None = None, transient: bool = False, ours: bool | None = None):
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.ours = status in _OURS if ours is None else ours


@dataclass
class Hit:
    """搜到的一条。"""

    title: str
    url: str
    summary: str  # 给模型读的摘要（没有就用 snippet）
    site: str = ""
    date: str | None = None  # RFC 3339（UTC）


def good_url(url: Any) -> bool:
    """正常的 http(s) 网址：没有空白和控制字符（有的话可能是想伪造聊天记录的换行）。"""
    if not (
        isinstance(url, str)
        and url.startswith(("http://", "https://"))
        and len(url) <= 2000
        and not any(c.isspace() or ord(c) < 32 for c in url)
    ):
        return False
    try:
        url.encode("utf-8")  # 不成对的代理字符（\ud800 之类）：存数据库、发消息时都会出错
    except UnicodeEncodeError:
        return False
    return True


def _error(status: int, data: Any) -> ToolError:
    detail = ""
    kind = ""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        detail = one_line(str(data["error"].get("message") or ""), 160)
        kind = str(data["error"].get("type") or "")
    elif isinstance(data, str):
        detail = one_line(data, 160)
    if status == 401:
        return ToolError("DeepSeek 的 key 不对（检查 .env 里的 DEEPSEEK_API_KEY）", status=status)
    if status == 402:
        return ToolError("DeepSeek 的余额不够了", status=status)
    if status == 403:
        return ToolError("被 DeepSeek 的安全策略拒绝了", status=status)
    if status == 404 and kind != "not_found_error":
        # DeepSeek 读不到网页时 type 是 not_found_error；别的 404 多半是接口地址不对（比如 base_url 填了不带这两个接口的中转）
        return ToolError("找不到 DeepSeek 的 Search / Fetch 接口（HTTP 404），检查 config.toml 的 base_url", status=status, ours=True)
    if status == 404:
        return ToolError("读不到内容（网站打不开、要登录、不让机器人看，或者不是网页）", status=status)
    if status == 429:
        return ToolError("太频繁了，被限流", status=status, transient=True)
    if status in _RETRY:
        return ToolError(f"DeepSeek 那边出错了（HTTP {status}）", status=status, transient=True)
    return ToolError(f"HTTP {status}" + (f"：{detail}" if detail else ""), status=status)


def parse_hits(data: Any) -> tuple[list[Hit], bool]:
    """Search 的结果 → (能用的几条, 这次算不算钱)。只要返回了结果就算钱，哪怕这边一条都用不了。"""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ToolError("返回的格式不对", status=200, transient=True)
    hits: list[Hit] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not good_url(url) or url in seen:
            continue
        seen.add(url)
        summary = item.get("summary") or item.get("snippet") or ""
        site = item.get("site_name")
        date = item.get("date")
        hits.append(
            Hit(
                title=clean_text(" ".join(str(item.get("title") or "").split()))[:200],
                url=url,
                summary=clean_text(str(summary)),
                site=clean_text(one_line(site, 60)) if isinstance(site, str) else "",
                date=date if isinstance(date, str) else None,
            )
        )
    return hits, bool(results)


def parse_page(data: Any) -> tuple[str, str]:
    """Fetch 的结果 → (标题, 正文 markdown)。没有正文的不算读到（也不算钱）。"""
    if not isinstance(data, dict):
        raise ToolError("返回的格式不对", status=200, transient=True)
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ToolError("读到的网页是空的", status=200)
    title = data.get("title")
    return (one_line(title, 200) if isinstance(title, str) else ""), content


def tools_base(base_url: str) -> str:
    """config.toml 的 base_url → 接口地址的前半段（有人写成 …/v1，那是 OpenAI 的写法，去掉）。"""
    base = base_url.strip().rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


class DeepSeekTools:
    def __init__(self, http: HttpClient, api_key: str, base_url: str = "https://api.deepseek.com"):
        self.http = http
        self.api_key = api_key
        self.base = tools_base(base_url)

    @property
    def search_url(self) -> str:
        return self.base + "/v1/tools/search"

    @property
    def fetch_url(self) -> str:
        return self.base + "/v1/tools/fetch"

    async def _post(self, url: str, payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            return await self.http.api("POST", url, payload=payload, headers=headers, timeout=timeout)
        except FetchError as exc:  # 连不上、超时：没拿到结果，不算钱
            raise ToolError(f"连不上 DeepSeek（{exc}）", status=exc.status, transient=True) from None

    async def search(self, query: str, *, count: int = 8) -> tuple[list[Hit], bool]:
        """搜一次 → (搜到的几条, 这次算不算钱)。没搜到返回空的；出错抛 ToolError（不算钱）。"""
        query = " ".join(query.split())[:MAX_QUERY].strip()
        if not query:
            raise ToolError("没有要搜的内容")
        payload = {"query": query, "count": max(1, min(50, int(count)))}
        status, data = await self._post(self.search_url, payload, SEARCH_TIMEOUT)
        if status in _RETRY:  # 限流、对方出错：等一下再试一次（搜索很快，不怕多等这一下）
            await asyncio.sleep(1)
            status, data = await self._post(self.search_url, payload, SEARCH_TIMEOUT)
        if status != 200:
            raise _error(status, data)
        return parse_hits(data)

    async def fetch(self, url: str) -> tuple[str, str]:
        """读一个网页 → (标题, 正文 markdown)。读不到抛 ToolError（不算钱）；读到了算一次钱。"""
        status, data = await self._post(self.fetch_url, {"url": url}, FETCH_TIMEOUT)
        if status != 200:
            raise _error(status, data)
        return parse_page(data)
