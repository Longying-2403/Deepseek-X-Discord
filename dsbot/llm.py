"""DeepSeek Chat Completions client.

Talks HTTP directly (aiohttp already comes with discord.py) instead of installing the openai SDK,
so the thinking switch and the cache fields in usage are handled exactly as DeepSeek documents them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .models import Completion, Usage

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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
        # thinking mode is on by default, so it has to be turned off explicitly when it isn't wanted
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = thinking  # low / high
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if user_id:
        payload["user_id"] = user_id  # isolate the KV cache per server
    return payload


def parse_completion(data: dict[str, Any]) -> Completion:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"unexpected response format: {str(data)[:200]}") from None
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(0, prompt - int(hit))
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    return Completion(
        content=message.get("content") or "",
        reasoning=message.get("reasoning_content"),
        usage=Usage(
            prompt_tokens=prompt,
            hit=int(hit),
            miss=int(miss),
            output=int(usage.get("completion_tokens") or 0),
            reasoning=int(reasoning),
        ),
        finish_reason=choice.get("finish_reason"),
    )


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

    async def _post(self, payload: dict[str, Any]) -> tuple[int, Any]:
        import aiohttp  # lazy import: not needed when testing the core logic

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=True,  # honor the HTTPS_PROXY environment variable
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        async with self._session.post(self.url, json=payload) as resp:
            try:
                data = await resp.json(content_type=None)
            except ValueError:
                data = await resp.text()
            return resp.status, data

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: str = "off",
        json_mode: bool = False,
        max_tokens: int | None = None,
        user_id: str | None = None,
    ) -> Completion:
        payload = build_payload(
            self.model, messages, thinking=thinking, json_mode=json_mode, max_tokens=max_tokens, user_id=user_id
        )
        last_error: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                await asyncio.sleep(min(2.0**attempt, 10.0))
            try:
                status, data = await self._post(payload)
            except asyncio.TimeoutError:
                # no retry on timeout: the request may already have finished and been billed on their side, so sending it again is only slower and pricier
                raise LLMError(f"request timed out ({self.timeout:g} s)") from None
            except Exception as exc:  # network errors such as aiohttp.ClientError
                last_error = LLMError(f"network error: {exc!r}")
                continue
            if status == 200 and isinstance(data, dict):
                return parse_completion(data)
            last_error = LLMError(f"HTTP {status}: {_error_message(data)}", status=status)
            if status not in RETRY_STATUS:
                break
            log.warning("DeepSeek returned %s, retrying (attempt %d)", status, attempt + 1)
        assert last_error is not None
        raise last_error
