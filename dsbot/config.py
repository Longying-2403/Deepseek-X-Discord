"""配置：.env 里的密钥 + config.toml 里的默认值 + 运行时可调参数的定义。"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .pricing import Pricing


# ---------------------------------------------------------------------------
# 可调参数的定义
# level = "admin"：服务器管理员可以用 /ds 改（服务器默认或单个频道）
# level = "owner"：只有你能改（config.toml 或私人服务器里的 /dsowner）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    key: str
    label: str  # 命令里显示的中文名
    kind: type  # bool / int / float / str
    level: str  # "admin" | "owner"
    help: str
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] | None = None


_SPEC_LIST = [
    # —— 管理员 ——
    Spec("proactive", "主动发言", bool, "admin", "没被点名时，是否自己判断要不要说话"),
    Spec("refill_turns", "额度恢复轮数", int, "admin", "别人每说这么多轮，恢复 1 次主动发言额度", 1, 500),
    Spec("quota_capacity", "额度上限", int, "admin", "主动发言额度最多攒几次", 1, 20),
    Spec("min_speak_gap", "最短发言间隔秒", int, "admin", "两次发言之间至少隔多少秒", 0, 86400),
    Spec("quiet_seconds", "冷场秒数", int, "admin", "多久没人说话、也没人在打字，算冷场", 5, 3600),
    Spec("max_wait_seconds", "最长等待秒", int, "admin", "有新消息后最多等多久就判断一次（即使没冷场）", 30, 86400),
    Spec("mention_rate_limit", "每人每分钟点名", int, "admin", "每个人每分钟最多能叫它几次", 1, 60),
    Spec("aliases", "别名", str, "admin", "消息里出现这些名字也算叫它，用逗号分隔，例如：DS,小D"),
    Spec("persona", "人设", str, "admin", "人设描述，留空用默认人设"),
    # —— 主人 ——
    Spec("min_eval_interval", "最短判断间隔秒", int, "owner", "两次主动判断之间至少隔多少秒（主要管成本）", 10, 86400),
    Spec("proactive_thinking", "主动判断思考", str, "owner", "主动判断时的思考强度", choices=("off", "low", "high")),
    Spec("mention_thinking", "点名回复思考", str, "owner", "被叫到时的思考强度", choices=("off", "low", "high")),
    Spec("daily_budget_usd", "每日预算美元", float, "owner", "单个服务器每天最多花多少美元，超了只回点名", 0, 1000),
    Spec("global_daily_budget_usd", "全局每日预算美元", float, "owner", "所有服务器加起来每天最多花多少美元", 0, 10000),
    Spec("context_budget_tokens", "上下文预算", int, "owner", "上下文超过这么多 token 就把旧记录压成摘要", 4000, 200000),
    Spec("vision", "看图", bool, "owner", "是否把群里的图片发给模型"),
]

SPECS: dict[str, Spec] = {s.key: s for s in _SPEC_LIST}
ADMIN_KEYS = [s.key for s in _SPEC_LIST if s.level == "admin"]
OWNER_KEYS = [s.key for s in _SPEC_LIST if s.level == "owner"]

BUILTIN_DEFAULTS: dict[str, Any] = {
    "proactive": False,
    "refill_turns": 15,
    "quota_capacity": 3,
    "min_speak_gap": 120,
    "quiet_seconds": 30,
    "max_wait_seconds": 180,
    "mention_rate_limit": 3,
    "aliases": "",
    "persona": "",
    "min_eval_interval": 60,
    "proactive_thinking": "low",
    "mention_thinking": "off",
    "daily_budget_usd": 1.0,
    "global_daily_budget_usd": 3.0,
    "context_budget_tokens": 30000,
    "vision": True,
}

# 一键档位：只动“话量”相关的三个参数
PRESETS: dict[str, dict[str, int]] = {
    "安静": {"refill_turns": 30, "quota_capacity": 1, "min_speak_gap": 600},
    "普通": {"refill_turns": 15, "quota_capacity": 3, "min_speak_gap": 120},
    "话痨": {"refill_turns": 6, "quota_capacity": 4, "min_speak_gap": 30},
}

_TRUE = {"on", "true", "1", "yes", "y", "开", "是", "启用", "打开"}
_FALSE = {"off", "false", "0", "no", "n", "关", "否", "停用", "关闭"}
RESET_WORDS = {"默认", "default", "reset", "清除"}


def parse_value(spec: Spec, raw: Any) -> Any:
    """把命令里输入的字符串（或 toml 里的值）转成参数值，不合法就抛 ValueError（中文说明）。"""
    if spec.kind is bool:
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ValueError(f"「{spec.label}」只能填 开/关")
    if spec.kind is int:
        try:
            number = float(str(raw).strip())
            if not math.isfinite(number):
                raise ValueError
            value: Any = int(number)
        except (ValueError, OverflowError):
            raise ValueError(f"「{spec.label}」要填整数") from None
    elif spec.kind is float:
        try:
            value = float(str(raw).strip())
        except (ValueError, OverflowError):
            raise ValueError(f"「{spec.label}」要填数字") from None
        if not math.isfinite(value):
            raise ValueError(f"「{spec.label}」要填正常的数字")
    else:
        value = str(raw).strip()
        if spec.choices is not None and value.lower() not in spec.choices:
            raise ValueError(f"「{spec.label}」只能是：{' / '.join(spec.choices)}")
        if spec.choices is not None:
            value = value.lower()
        return value
    if spec.min is not None and value < spec.min:
        raise ValueError(f"「{spec.label}」不能小于 {spec.min:g}")
    if spec.max is not None and value > spec.max:
        raise ValueError(f"「{spec.label}」不能大于 {spec.max:g}")
    return value


def format_value(spec: Spec, value: Any) -> str:
    if spec.kind is bool:
        return "开" if value else "关"
    if spec.kind is float:
        return f"{value:g}"
    if spec.key == "persona":
        return "（默认人设）" if not value else f"{len(str(value))} 字的自定义人设"
    if value == "":
        return "（无）"
    return str(value)


# ---------------------------------------------------------------------------
# config.toml
# ---------------------------------------------------------------------------


@dataclass
class DeepSeekConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout_seconds: float = 90.0
    max_retries: int = 2


@dataclass
class BotConfig:
    timezone: str = "Asia/Taipei"
    database: str = "data/dsbot.sqlite3"
    sync_commands_on_start: bool = True
    log_level: str = "INFO"
    tick_seconds: float = 5.0


@dataclass
class EngineConfig:
    max_batch_messages: int = 40  # 一轮最多带多少条新消息（更早的只记数量）
    max_pending_messages: int = 120  # 缓冲区上限，防止长期没人叫它时无限增长
    pending_max_age_seconds: float = 21600  # 缓冲区里超过 6 小时的旧消息直接丢掉
    max_chars_per_message: int = 1200  # 单条消息进上下文前截断到多少字
    max_images_per_round: int = 4
    image_max_side: int = 1024  # 叫到它的那条消息里的图片：缩到最长边多少像素
    low_detail_max_side: int = 512  # 其他图片（detail=low，模型那边也只看 512×512）
    turn_merge_seconds: float = 20.0  # 同一个人这么多秒内连发算一轮
    react_cost: float = 0.3  # 只加表情时消耗多少额度
    typing_chars_per_second: float = 3.0
    typing_min_seconds: float = 2.0
    typing_max_seconds: float = 10.0
    max_output_tokens: int = 2000  # 不思考时
    max_thinking_tokens: int = 6000  # 思考时（含推理）
    hard_stop_multiplier: float = 2.0  # 花费超过预算这么多倍后，连点名也不回
    summary_max_chars: int = 600


@dataclass
class AppConfig:
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    pricing: Pricing = field(default_factory=Pricing)
    bot: BotConfig = field(default_factory=BotConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    defaults: dict[str, Any] = field(default_factory=lambda: dict(BUILTIN_DEFAULTS))


def _fill(cls: type, data: dict[str, Any], section: str) -> Any:
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"config.toml [{section}] 里有未知的配置项：{key}")
        kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path | None) -> AppConfig:
    data: dict[str, Any] = {}
    if path is not None and Path(path).exists():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)

    cfg = AppConfig()
    if "deepseek" in data:
        cfg.deepseek = _fill(DeepSeekConfig, data["deepseek"], "deepseek")
    if "bot" in data:
        cfg.bot = _fill(BotConfig, data["bot"], "bot")
    if "engine" in data:
        cfg.engine = _fill(EngineConfig, data["engine"], "engine")
    if "pricing" in data:
        p = dict(data["pricing"])
        if "peak_hours_utc" in p:
            p["peak_hours_utc"] = tuple(tuple(int(x) for x in pair) for pair in p["peak_hours_utc"])
        cfg.pricing = _fill(Pricing, p, "pricing")

    defaults = dict(BUILTIN_DEFAULTS)
    for key, raw in (data.get("defaults") or {}).items():
        if key not in SPECS:
            raise ValueError(f"config.toml [defaults] 里有未知的参数：{key}")
        defaults[key] = parse_value(SPECS[key], raw)
    cfg.defaults = defaults
    return cfg


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Secrets:
    discord_token: str
    deepseek_api_key: str
    owner_id: int
    control_guild_id: int | None = None
    discord_proxy: str | None = None


def load_secrets(env: dict[str, str] | None = None) -> Secrets:
    env = dict(os.environ) if env is None else env
    missing = [k for k in ("DISCORD_TOKEN", "DEEPSEEK_API_KEY", "OWNER_ID") if not env.get(k, "").strip()]
    if missing:
        raise SystemExit("缺少环境变量：" + "、".join(missing) + "（请按 .env.example 填好 .env）")
    try:
        owner_id = int(env["OWNER_ID"].strip())
        control = env.get("CONTROL_GUILD_ID", "").strip()
        control_id = int(control) if control else None
    except ValueError:
        raise SystemExit("OWNER_ID / CONTROL_GUILD_ID 必须是纯数字 ID") from None
    return Secrets(
        discord_token=env["DISCORD_TOKEN"].strip(),
        deepseek_api_key=env["DEEPSEEK_API_KEY"].strip(),
        owner_id=owner_id,
        control_guild_id=control_id,
        discord_proxy=env.get("DISCORD_PROXY", "").strip() or None,
    )
