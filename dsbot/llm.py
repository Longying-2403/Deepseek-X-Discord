"""DeepSeek Chat Completions 客户端。

直接走 HTTP（aiohttp 是 discord.py 自带的依赖），不额外装 openai SDK，
这样思考开关、用量里的缓存字段都按 DeepSeek 文档原样处理。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .models import Completion, Usage
from .render import content_tokens

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
DEFAULT_MAX_TOKENS = 4096  # 没写 max_tokens 时按这么多估（引擎的调用都写了）
CONNECT_TIMEOUT = 15.0  # 连上 DeepSeek（含代理、TLS 握手）最多等这么久：连都没连上的，请求没发出去，不算钱，可以重试


class _CutOff(Exception):
    """响应读到一半连接断了：请求已经被处理（多半也算了钱），不重试。"""


class _NotSent(Exception):
    """连接阶段就超时了（连不上、代理卡住）：请求没发出去，对方没算钱，可以重试。"""


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None, *, timeout: bool = False, usage: Usage | None = None):
        super().__init__(message)
        self.status = status
        self.timeout = timeout  # 等太久没等到结果（对方可能已经算钱了）
        self.usage = usage  # 超时、传到一半断了：估出来的用量（对方多半照样算了钱，调用方要记账）；别的错误是 None


def build_payload(
    model: str,
    messages: list[dict[str, Any]],
    *,
    thinking: str = "off",
    json_mode: bool = False,
    max_tokens: int | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if thinking == "off":
        # 思考模式默认开启，不需要时必须显式关掉
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = thinking  # low / high
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if user_id:
        payload["user_id"] = user_id  # 按服务器隔离 KV 缓存
    return payload


def estimate_usage(messages: list[dict[str, Any]], max_tokens: int | None) -> Usage:
    """超时、响应传到一半断了：拿不到用量，但对方多半照样跑完并计费，估一笔记进每日预算。
    输入全算：最后两条（上一次的回复、这次新加的内容）按没命中缓存算；前面的是上一次请求发过的前缀，按命中算
    （只有两条的，比如整理记忆、压缩摘要，就全按没命中）。输出不知道写到哪了，按上限的一半算。"""
    tokens = [content_tokens(m.get("content")) for m in messages]
    hit, miss = sum(tokens[:-2]), sum(tokens[-2:])
    return Usage(prompt_tokens=hit + miss, hit=hit, miss=miss, output=(max_tokens or DEFAULT_MAX_TOKENS) // 2)


def parse_usage(usage: dict[str, Any]) -> Usage:
    prompt = int(usage.get("prompt_tokens") or 0)
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(0, prompt - int(hit))
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    return Usage(
        prompt_tokens=prompt,
        hit=int(hit),
        miss=int(miss),
        output=int(usage.get("completion_tokens") or 0),
        reasoning=int(reasoning),
    )


def parse_completion(data: dict[str, Any]) -> Completion:
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise TypeError("content")
        return Completion(
            content=content,
            reasoning=message.get("reasoning_content"),
            usage=parse_usage(data.get("usage") or {}),
            finish_reason=choice.get("finish_reason"),
        )
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise LLMError(f"响应格式不对：{str(data)[:200]}", status=200) from None


def _billed_usage(data: Any, messages: list[dict[str, Any]], max_tokens: int | None) -> Usage:
    """HTTP 200 但结果用不了：对方已经跑完、算了钱。响应里带了用量就照记，没有就估一笔。"""
    if isinstance(data, dict) and isinstance(data.get("usage"), dict):
        try:
            return parse_usage(data["usage"])
        except (TypeError, ValueError, AttributeError):
            pass
    return estimate_usage(messages, max_tokens)


def _error_message(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return str(data)[:300]


class DeepSeekClient:
    def __init__(self, *, api_key: str, base_url: str, model: str, timeout: float = 90.0, max_retries: int = 2):
        self.api_key = api_key
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
        import aiohttp  # 延迟导入：核心逻辑测试时不需要

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=True,  # 支持 HTTPS_PROXY 环境变量
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        # 连接阶段单独限时：连都没连上就超时的，请求没发出去（3.10 以前的 aiohttp 没有单独的 ConnectionTimeoutError）
        not_connected = getattr(aiohttp, "ConnectionTimeoutError", aiohttp.ServerTimeoutError)
        # 连接的时限要比总时限早一截（aiohttp 会把时限凑成整秒，靠得太近就总是总时限先到，分不出来）
        limits = aiohttp.ClientTimeout(total=timeout, connect=min(CONNECT_TIMEOUT, timeout / 2))
        try:
            async with self._session.post(self.url, json=payload, timeout=limits) as resp:
                try:
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError:
                        data = await resp.text(errors="replace")  # 不是 UTF-8 的也照样当成“拿到了响应”
                except aiohttp.ClientPayloadError as exc:
                    if resp.status != 200:
                        return resp.status, ""  # 错误响应（429、503……）传到一半断了：对方没跑这次请求，照常按状态码处理
                    raise _CutOff(repr(exc)) from None
                except asyncio.TimeoutError:
                    if resp.status != 200:
                        return resp.status, ""  # 同上：读错误响应的时候超时
                    raise
                return resp.status, data
        except not_connected as exc:
            raise _NotSent(repr(exc)) from None

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: str = "off",
        json_mode: bool = False,
        max_tokens: int | None = None,
        user_id: str | None = None,
        timeout_scale: float = 1.0,
    ) -> Completion:
        """timeout_scale：这次最多等几倍的 timeout（后台整理记忆没人在等，输出又可能比较长）。"""
        payload = build_payload(
            self.model, messages, thinking=thinking, json_mode=json_mode, max_tokens=max_tokens, user_id=user_id
        )
        timeout = self.timeout * max(1.0, timeout_scale)
        last_error: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                await asyncio.sleep(min(2.0**attempt, 10.0))
            try:
                status, data = await self._post(payload, timeout)
            except _NotSent as exc:
                last_error = LLMError(f"连不上 DeepSeek：{exc}")  # 请求没发出去：不算钱，可以重试
                continue
            except asyncio.TimeoutError:
                # 超时不重试：请求可能已经在对方那边跑完并计费了，再发一次只会更慢、更贵；估一笔账让调用方记上
                raise LLMError(f"请求超时（{timeout:g} 秒）", timeout=True, usage=estimate_usage(messages, max_tokens)) from None
            except _CutOff as exc:
                # 同理：结果传到一半断了，对方已经算过钱，不再发一次（也估一笔账）
                raise LLMError(f"响应传到一半连接断了：{exc}", usage=estimate_usage(messages, max_tokens)) from None
            except Exception as exc:  # aiohttp.ClientError 等网络错误
                last_error = LLMError(f"网络错误：{exc!r}")
                continue
            if status == 200 and isinstance(data, dict):
                try:
                    return parse_completion(data)
                except LLMError as exc:
                    exc.usage = _billed_usage(data, messages, max_tokens)  # 200：对方跑完了，照样算了钱
                    raise
            last_error = LLMError(f"HTTP {status}：{_error_message(data)}", status=status)
            if status == 200:
                last_error.usage = _billed_usage(data, messages, max_tokens)  # 200 却不是 JSON：对方照样算了钱
            if status not in RETRY_STATUS:
                break
            log.warning("DeepSeek 返回 %s，准备重试（第 %d 次）", status, attempt + 1)
        assert last_error is not None
        raise last_error
