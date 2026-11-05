"""联网搜索：先用 DeepSeek 的 Search 接口（见 dstools.py），没搜到或者出错了再用 Tavily（要 .env 里的 TAVILY_API_KEY）。

搜到的几条（标题、网站、日期、摘要）整理成一段文字交给模型自己读，来源附在后面。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .dstools import DeepSeekTools, Hit, ToolError, good_url
from .models import SearchResult
from .tavily import Tavily, TavilyError
from .textutil import clean_text, one_line

log = logging.getLogger(__name__)

MAX_SOURCES = 5
SEARCH_COUNT = 8  # 向 DeepSeek 要几条（价格和条数无关；字数放不下的就不放）
_HEADING = re.compile(r"^\s*#{1,6}\s[^\n]*\n?")  # 摘要开头那行“# 标题”：标题已经单独写了


class SearchError(Exception):
    def __init__(self, message: str, *, paid: int = 0):
        super().__init__(message)
        self.paid = paid  # 没搜成，但 DeepSeek 已经算了钱的次数（返回了结果，这边却一条都用不了）


def local_date(date: str | None, tz: str) -> str:
    """RFC 3339 的时间 → 本地日期（2026-11-05）；看不懂就是空的。"""
    if not date:
        return ""
    try:
        when = datetime.fromisoformat(date.strip().replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(ZoneInfo(tz)).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def format_hits(hits: list[Hit], max_chars: int, tz: str) -> tuple[str, list[Hit]]:
    """几条搜索结果 → 给模型读的一段文字，和放进去了哪几条（字数放不下的不放，第一条一定放）。"""
    per = max(200, max_chars // 3)  # 每条的摘要最多多少字：至少放得下三条
    blocks: list[str] = []
    used: list[Hit] = []
    total = 0
    for hit in hits:
        about = "，".join(x for x in (hit.site, local_date(hit.date, tz)) if x)
        head = f"{len(used) + 1}. {one_line(hit.title or hit.site or hit.url, 80)}" + (f"（{about}）" if about else "")
        gist = one_line(_HEADING.sub("", hit.summary.strip(), count=1), per)
        block = head + ("\n" + gist if gist else "")
        if used and total + len(block) + 1 > max_chars:
            break
        blocks.append(block)
        used.append(hit)
        total += len(block) + 1
    return "\n".join(blocks), used


def parse_tavily_response(data: Any) -> SearchResult:
    if not isinstance(data, dict):
        raise SearchError("响应格式不对")
    lines: list[str] = []
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    answer = data.get("answer")
    if isinstance(answer, str) and answer.strip():
        lines.append(answer.strip())
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        content = " ".join(str(item.get("content") or "").split())[:400]
        if title or content:
            lines.append(f"- {title}：{content}" if title else f"- {content}")
        url = item.get("url")
        if good_url(url) and url not in seen:
            seen.add(url)
            sources.append((clean_text(title or url)[:80], clean_text(url)))
    if not lines:
        raise SearchError("什么都没搜到")
    return SearchResult(summary=clean_text("\n".join(lines)), sources=sources[:MAX_SOURCES], provider="Tavily")


class WebSearch:
    """先用 DeepSeek 的 Search 接口，没搜到、出错了再用 Tavily。两个都没有就搜不了。"""

    def __init__(self, tools: DeepSeekTools | None, tavily: Tavily | None = None, *, tz: str = "UTC"):
        self.tools = tools
        self.tavily = tavily
        self.tz = tz

    async def search(self, query: str, *, max_chars: int) -> SearchResult:
        errors: list[str] = []
        paid = 0  # DeepSeek 算了钱的次数：后面改用 Tavily 了也要记账
        if self.tools is not None:
            try:
                hits, billed = await self.tools.search(query, count=SEARCH_COUNT)
            except ToolError as exc:
                errors.append(f"DeepSeek：{exc}")
                log.log(logging.WARNING if exc.ours else logging.INFO, "DeepSeek 搜索失败：%s", exc)  # key、余额要你处理
            except Exception as exc:  # 意料之外的错误：也改用备用的
                errors.append(f"DeepSeek：{type(exc).__name__}")
                log.warning("DeepSeek 搜索出错：%r", exc)
            else:
                paid += int(billed)
                if hits:
                    summary, used = format_hits(hits, max_chars, self.tz)
                    sources = [(one_line(h.title or h.site or h.url, 80), h.url) for h in used[:MAX_SOURCES]]
                    return SearchResult(summary=summary, sources=sources, provider="DeepSeek", paid=paid)
                errors.append("DeepSeek：什么都没搜到")
        if self.tavily is not None:
            try:
                result = parse_tavily_response(await self.tavily.search(query, max_results=MAX_SOURCES))
            except (TavilyError, SearchError) as exc:
                errors.append(f"Tavily：{exc}")
                log.warning("Tavily 搜索失败：%s", exc)
            except Exception as exc:
                errors.append(f"Tavily：{type(exc).__name__}")
                log.warning("Tavily 搜索出错：%r", exc)
            else:
                result.paid = paid
                return result
        raise SearchError("；".join(errors) or "没有能用的搜索", paid=paid)
