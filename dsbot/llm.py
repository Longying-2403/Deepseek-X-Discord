"""xAI（Grok）Chat Completions 客户端。

直接走 HTTP（aiohttp 是 discord.py 自带的依赖），不额外装 openai / xai-sdk，
这样推理强度、JSON 模式、缓存路由和用量里的字段都按 xAI 文档原样处理。

和 DeepSeek 版的几处不同：
- 思考强度用 reasoning_effort：off → "none"（grok-4.3 能完全不推理；grok-4.5 及以后的型号关不掉，退到 "low"）。
- 输出上限用 max_completion_tokens（max_tokens 已经弃用），而且只管看得见的输出，不含推理。
- 缓存靠 x-grok-conv-id 这个请求头把同一个服务器的请求送到同一台机器上。
- usage 里 completion_tokens 不含推理 token，两样都按输出价算；xAI 还会直接给出这次的实际花费（cost_in_usd_ticks）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .models import Completion, Usage

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
TICKS_PER_USD = 10_000_000_000  # cost_in_usd_ticks：100 亿 tick = 1 美元

# bot 里的思考强度 → xAI 的 reasoning_effort
EFFORTS = {"off": "none", "low": "low", "medium": "medium", "high": "high"}
# 这些型号推理关不掉（不收 "none"）：off 只能退到最低的 low
ALWAYS_REASONING = ("grok-4.5", "grok-4.6", "grok-4.7")

# 余额用完、到了花费上限这类问题：等充值，不是请求本身的错（普通的限流不算，那个过一会儿就好）
_BILLING = re.compile(r"credit|billing|spending|balance|licen[cs]e|payment", re.I)
_BAD_KEY = re.compile(r"api[ _-]?key|unauthori[sz]ed|authenticat", re.I)
_JSON_PARAM = re.compile(r"response_format|json_object|json mode", re.I)


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None, *, timeout: bool = False):
        super().__init__(message)
        self.status = status
        self.timeout = timeout  # 等太久没等到结果（对方可能已经算钱了）


def effort_for(model: str, thinking: str) -> str:
    effort = EFFORTS.get(str(thinking).lower(), "low")
    if effort == "none" and model.lower().startswith(ALWAYS_REASONING):
        return "low"
    return effort


def build_payload(
    model: str,
    messages: list[dict[str, Any]],
    *,
    thinking: str = "off",
    json_mode: bool = False,
    max_tokens: int | None = None,
    user_id: str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """effort：直接指定 reasoning_effort（模型不认的时候调用方会换一个）；"" = 不发这个参数。"""
    payload: dict[str, Any] = {"model": model, "messages": messages}
    effort = effort_for(model, thinking) if effort is None else effort
    if effort:
        payload["reasoning_effort"] = effort
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if max_tokens:
        payload["max_completion_tokens"] = int(max_tokens)
    if user_id:
        payload["user"] = user_id  # 告诉 xAI 是哪个服务器的请求（出了滥用问题时按服务器算，不连累整个 key）
    return payload


def ticks_to_usd(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value) / TICKS_PER_USD


def billed_output(prompt: int, completion: int, reasoning: int, total: Any) -> int:
    """按输出价计费的 token 数。xAI 的 completion_tokens 不含推理（total = prompt + completion + reasoning），
    两样都按输出价算；万一哪天改成和 OpenAI 一样“completion 已经含推理”，就用 total_tokens 认出来，免得算两遍。"""
    if reasoning and isinstance(total, int) and not isinstance(total, bool) and total == prompt + completion:
        return completion
    return completion + reasoning


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_completion(data: dict[str, Any]) -> Completion:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"响应格式不对：{str(data)[:200]}") from None
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    prompt = _int(usage.get("prompt_tokens"))
    hit = _int((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
    if prompt:
        hit = min(hit, prompt)
    completion = _int(usage.get("completion_tokens"))
    reasoning = _int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    return Completion(
        content=message.get("content") or "",
        reasoning=message.get("reasoning_content"),
        usage=Usage(
            prompt_tokens=prompt,
            hit=hit,
            miss=max(0, prompt - hit),
            output=billed_output(prompt, completion, reasoning, usage.get("total_tokens")),
            reasoning=reasoning,
            cost_usd=ticks_to_usd(usage.get("cost_in_usd_ticks")),
        ),
        finish_reason=choice.get("finish_reason"),
    )


def error_message(data: Any) -> str:
    """xAI 的错误是 {"code": "...", "error": "说明"}；也认 OpenAI 的 {"error": {"message": "..."}}。"""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:500]
        if isinstance(err, str) and err.strip():
            return err.strip()[:500]
        for key in ("message", "detail", "code"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()[:500]
    return str(data)[:300]


def classify_status(status: int, message: str) -> int:
    """把 xAI 的错误归到引擎认识的几类：401 = key 不对，402 = 没钱了（余额用完 / 到了花费上限）。"""
    if status in (402, 403, 429) and _BILLING.search(message):
        return 402
    if status in (400, 403) and _BAD_KEY.search(message) and not _BILLING.search(message):
        return 401
    return status


class XAIClient:
    def __init__(self, *, api_key: str, base_url: str, model: str, timeout: float = 90.0, max_retries: int = 2):
        self.api_key = api_key
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = None
        # 模型不认的参数：记下来，以后的请求不再发（只会因为参数被拒而改，不会因为别的错误改）
        self.no_none = False  # 不收 reasoning_effort="none"：off 退到 low
        self.no_effort = False  # 连 reasoning_effort 都不收（比如不推理的型号）
        self.no_json = False  # 不收 response_format
        self.notes: list[str] = []  # 改过什么，给自检和日志看

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> tuple[int, Any]:
        import aiohttp  # 延迟导入：核心逻辑测试时不需要

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=True,  # 支持 HTTPS_PROXY 环境变量
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        async with self._session.post(
            self.url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except ValueError:
                data = await resp.text()
            return resp.status, data

    def _effort(self, thinking: str) -> str:
        if self.no_effort:
            return ""
        effort = effort_for(self.model, thinking)
        return "low" if effort == "none" and self.no_none else effort

    def _adjust(self, payload: dict[str, Any], message: str) -> bool:
        """请求因为某个参数被拒（400 / 422）：去掉或换掉它再试一次。被拒的请求不算钱。"""
        text = message.lower()
        if "reasoning" in text and "reasoning_effort" in payload:
            if payload["reasoning_effort"] == "none" and not self.no_none:
                self.no_none = True
                payload["reasoning_effort"] = "low"
                self._note(f"模型 {self.model} 不收 reasoning_effort=none（推理关不掉），思考强度 off 改用 low")
            else:
                self.no_effort = True
                payload.pop("reasoning_effort")
                self._note(f"模型 {self.model} 不收 reasoning_effort，以后不发这个参数（用模型默认的推理强度）")
            return True
        if _JSON_PARAM.search(text) and "response_format" in payload:
            self.no_json = True
            payload.pop("response_format")
            self._note(f"模型 {self.model} 不收 response_format，以后不要求 JSON 模式（提示词里照样要它输出 JSON）")
            return True
        return False

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)
            log.warning("%s", text)

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
            self.model,
            messages,
            thinking=thinking,
            json_mode=json_mode and not self.no_json,
            max_tokens=max_tokens,
            user_id=user_id,
            effort=self._effort(thinking),
        )
        # 同一个服务器的请求送到同一台机器上，前缀缓存才命中得了
        headers = {"x-grok-conv-id": user_id} if user_id else {}
        timeout = self.timeout * max(1.0, timeout_scale)
        last_error: LLMError | None = None
        attempt = adjustments = 0
        while attempt <= self.max_retries:
            if attempt:
                await asyncio.sleep(min(2.0**attempt, 10.0))
            try:
                status, data = await self._post(payload, headers, timeout)
            except asyncio.TimeoutError:
                # 超时不重试：请求可能已经在对方那边跑完并计费了，再发一次只会更慢、更贵
                raise LLMError(f"请求超时（{timeout:g} 秒）", timeout=True) from None
            except Exception as exc:  # aiohttp.ClientError 等网络错误
                last_error = LLMError(f"网络错误：{exc!r}")
                attempt += 1
                continue
            if status == 200 and isinstance(data, dict):
                return parse_completion(data)
            message = error_message(data)
            if status in (400, 422) and adjustments < 3 and self._adjust(payload, message):
                adjustments += 1  # 换了参数马上再发，不算重试次数
                continue
            kind = classify_status(status, message)
            last_error = LLMError(f"HTTP {status}：{message}", status=kind)
            if kind not in RETRY_STATUS:
                break
            log.warning("xAI 返回 %s，准备重试（第 %d 次）", status, attempt + 1)
            attempt += 1
        assert last_error is not None
        raise last_error
