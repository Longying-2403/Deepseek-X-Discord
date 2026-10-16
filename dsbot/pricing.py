"""按 usage 算花费。xAI 在响应里直接给出这次的实际花费（cost_in_usd_ticks）时就用它，
没有时按这里的价格估。价格单位：美元 / 百万 token。xAI 没有高峰、非高峰之分。"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Usage


@dataclass(frozen=True)
class Pricing:
    # grok-4.3 的价格（2026-10 xAI 官方价格页）。换模型时改这里，见 config.toml 的 [pricing]
    cache_hit: float = 0.20
    cache_miss: float = 1.25
    output: float = 2.50  # 推理 token 也按这个价
    # 一次请求的输入达到这么多 token，整次请求都按 long_context_factor 倍计费
    long_context_tokens: int = 200_000
    long_context_factor: float = 2.0
    web_search_per_1k: float = 5.0  # 联网搜索：xAI 每搜 1000 次 5 美元（另外加 token 费）

    def cost(self, usage: Usage, ts: float = 0.0) -> float:
        """ts 没用到（DeepSeek 版按时段打折要用），留着让调用的地方两个版本一样。"""
        if usage.cost_usd is not None:
            return usage.cost_usd
        factor = self.long_context_factor if 0 < self.long_context_tokens <= usage.prompt_tokens else 1.0
        tokens = (usage.hit * self.cache_hit + usage.miss * self.cache_miss + usage.output * self.output) / 1_000_000
        return tokens * factor + usage.web_searches * self.web_search_per_1k / 1000
