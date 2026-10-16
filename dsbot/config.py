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
# level = "admin"：服务器管理员可以用 /xai 改（服务器默认或单个频道）
# level = "owner"：只有你能改（config.toml 或 CONTROL_GUILD_ID 那个服务器里的 /xai-owner）
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
    Spec("enabled", "允许回应", bool, "admin", "关掉后这里的消息它完全不看：不回 @、不主动说话，也不会发给 xAI"),
    Spec("proactive", "主动发言", bool, "admin", "没被点名时，是否自己判断要不要说话"),
    Spec("refill_turns", "额度恢复轮数", int, "admin", "别人每说这么多轮，恢复 1 次主动发言额度", 1, 500),
    Spec("quota_capacity", "额度上限", int, "admin", "主动发言额度最多攒几次", 1, 20),
    Spec("min_speak_gap", "最短发言间隔秒", int, "admin", "两次发言之间至少隔多少秒", 0, 86400),
    Spec("quiet_seconds", "冷场秒数", int, "admin", "多久没人说话、也没人在打字，算冷场（别的 bot 说话不算）", 5, 3600),
    Spec("max_wait_seconds", "最长等待秒", int, "admin", "有新消息后最多等多久就判断一次（即使没冷场）", 30, 86400),
    Spec("mention_rate_limit", "每人每分钟点名", int, "admin", "每个人每分钟最多能叫它几次", 1, 60),
    Spec("aliases", "别名", str, "admin", "消息里出现这些名字也算叫它，用逗号分隔，例如：Grok,小G（别和同一个服务器里别的 bot 的别名重复）"),
    Spec("persona", "人设", str, "admin", "人设描述，留空用默认人设"),
    Spec("share_memory", "记忆共享", bool, "admin", "和别的也开了共享的频道互通记忆（私密频道要单独开才会分享出去）"),
    Spec("images", "发图", bool, "admin", "让它自己发表情包库里的图，或者上网找图（找到的图它会先看一遍再决定发不发）"),
    Spec("cards", "人设卡", bool, "admin", "记住群友自己说的称呼、设定、喜好，聊天时用上（群友用 /xai-rp 管自己的卡，管理员看不到）"),
    Spec("long_memory", "长期记忆", bool, "admin", "记住这个服务器的约定、大事、长期的梗和剧情（用 /xai 长期记忆 管理）"),
    # —— 主人 ——
    Spec("min_eval_interval", "最短判断间隔秒", int, "owner", "两次主动判断之间至少隔多少秒（主要管成本）", 10, 86400),
    Spec("proactive_thinking", "主动判断思考", str, "owner", "主动判断时的推理强度（off = 不推理）", choices=("off", "low", "medium", "high")),
    Spec("mention_thinking", "点名回复思考", str, "owner", "被叫到时的推理强度（off = 不推理，最快）", choices=("off", "low", "medium", "high")),
    Spec("daily_budget_usd", "每日预算美元", float, "owner", "单个服务器每天最多花多少美元，超了只回点名", 0, 1000),
    Spec("global_daily_budget_usd", "全局每日预算美元", float, "owner", "所有服务器加起来每天最多花多少美元", 0, 10000),
    Spec("context_budget_tokens", "上下文预算", int, "owner", "上下文超过这么多 token 就把旧记录压成摘要", 8000, 200000),
    Spec("vision", "看图", bool, "owner", "是否把群里的图片发给模型"),
    Spec("files", "读文件", bool, "owner", "是否读取群友上传的文件（文本、代码、PDF、Word、Excel、PPT）"),
    Spec("web_search", "联网搜索", bool, "owner", "被叫到时，需要最新信息就上网搜（用 xAI 自带的搜索，每搜一次另收约 0.5 美分）"),
    Spec("daily_search_limit", "每日搜索上限", int, "owner", "每个服务器每天最多搜几次（0 = 不搜）", 0, 1000),
    Spec("links", "读链接", bool, "owner", "被叫到时打开消息里的链接（推文走 FxTwitter，别的网页要 Tavily）"),
    Spec("daily_image_limit", "每日找图上限", int, "owner", "每个服务器每天最多上网找几次图（0 = 不找，表情包库照常能用）", 0, 1000),
]

SPECS: dict[str, Spec] = {s.key: s for s in _SPEC_LIST}
ADMIN_KEYS = [s.key for s in _SPEC_LIST if s.level == "admin"]
OWNER_KEYS = [s.key for s in _SPEC_LIST if s.level == "owner"]

BUILTIN_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "proactive": False,
    "refill_turns": 15,
    "quota_capacity": 3,
    "min_speak_gap": 120,
    # 比 DeepSeek 版（30 / 180）晚一点再判断：两个 bot 在同一个频道时，它能先看到对方说了什么，不抢着一起插话
    "quiet_seconds": 60,
    "max_wait_seconds": 240,
    "mention_rate_limit": 3,
    "aliases": "",
    "persona": "",
    "share_memory": False,
    "images": True,
    "cards": True,
    "long_memory": True,
    "min_eval_interval": 60,
    "proactive_thinking": "low",
    "mention_thinking": "off",
    "daily_budget_usd": 1.0,
    "global_daily_budget_usd": 3.0,
    "context_budget_tokens": 30000,
    "vision": True,
    "files": True,
    "web_search": True,
    "daily_search_limit": 30,
    "links": True,
    "daily_image_limit": 10,
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
class XAIConfig:
    base_url: str = "https://api.x.ai/v1"
    model: str = "grok-4.3"  # 推理能完全关掉（off），价格也最低；grok-4.5 / 4.6 / 4.7 更强但贵，推理也关不掉
    timeout_seconds: float = 90.0
    max_retries: int = 2  # 429 / 5xx 时自动重试几次
    # 联网搜索走 xAI 的 Responses API（自带 web_search 工具）
    search_model: str = ""  # 留空 = 和 model 一样
    search_timeout_seconds: float = 90.0
    search_max_uses: int = 2  # 一次搜索里最多让它搜几轮（max_turns；每搜一次另收 0.5 美分，一轮里可能同时搜好几次）
    search_effort: str = "low"  # 搜索时的推理强度：low / medium / high；留空 = 用模型默认的

    @property
    def responses_url(self) -> str:
        return self.base_url.rstrip("/") + "/responses"

    @property
    def search_model_name(self) -> str:
        return self.search_model.strip() or self.model


@dataclass
class BotConfig:
    timezone: str = "Asia/Taipei"
    database: str = "data/xaibot.sqlite3"  # 和 DeepSeek 版的文件名不一样：万一两个 bot 指到同一个 data 文件夹也不会混在一起
    sync_commands_on_start: bool = True
    log_level: str = "INFO"
    tick_seconds: float = 5.0
    meme_dir: str = "data/表情包"  # 通用表情包库：自己在 NAS 上建这个文件夹放图
    server_meme_dir: str = "data/服务器表情包"  # 各服务器用 /xai 表情包 上传的
    cards_database: str = "data/xai-cards.sqlite3"  # 人设卡（和聊天记录分开存，方便备份、搬家）
    memory_database: str = "data/xai-memory.sqlite3"  # 长期记忆


@dataclass
class EngineConfig:
    max_batch_messages: int = 40  # 一轮最多带多少条新消息（更早的只记数量）
    max_pending_messages: int = 120  # 缓冲区上限，防止长期没人叫它时无限增长
    pending_max_age_seconds: float = 21600  # 缓冲区里超过 6 小时的旧消息直接丢掉
    max_chars_per_message: int = 1800  # 单条消息进上下文前截断到多少字（角色卡之类更长的，发 .txt 再叫它）
    max_images_per_round: int = 4
    image_max_side: int = 1024  # 叫到它的那条消息里的图片：缩到最长边多少像素
    low_detail_max_side: int = 512  # 其他图片（detail=low，模型那边也只看 512×512）
    file_max_bytes: int = 20_000_000  # 文件超过这个大小不读
    file_max_chars: int = 20000  # 单个文件最多读多少字
    file_round_max_chars: int = 40000  # 一轮里所有文件加起来最多多少字
    max_files_per_round: int = 3
    file_timeout_seconds: float = 30.0  # 解析一个文件最多等多久
    turn_merge_seconds: float = 20.0  # 同一个人这么多秒内连发算一轮
    react_cost: float = 0.3  # 只加表情时消耗多少额度
    typing_chars_per_second: float = 3.0
    typing_min_seconds: float = 2.0
    typing_max_seconds: float = 10.0
    max_output_tokens: int = 2000  # 不思考时
    max_thinking_tokens: int = 6000  # 思考时（含推理）
    hard_stop_multiplier: float = 2.0  # 花费超过预算这么多倍后，连点名也不回
    summary_max_chars: int = 600
    max_searches_per_round: int = 2  # 被叫到的一轮里最多搜几次
    search_max_chars: int = 2000  # 一次搜索的结果最多多少字
    share_recent_hours: float = 6.0  # 同一个人这么久之内在别的频道跟它聊过，就把那几句带过来
    recall_max_chars: int = 3000  # 翻别的频道的记录，一次最多多少字
    share_list_limit: int = 30  # 告诉它可以翻哪些频道，最多列多少个
    emoji_list_limit: int = 100  # 告诉它服务器里有哪些表情，最多列多少个
    sticker_list_limit: int = 30  # 告诉它服务器里有哪些贴图，最多列多少个
    link_max_chars: int = 4000  # 打开一个链接，最多读多少字
    max_links_per_round: int = 3  # 一轮最多打开几个链接
    daily_link_limit: int = 100  # 每个服务器每天最多打开多少个链接（Tavily 每打开 5 个网页算 1 次额度）
    image_cooldown_seconds: float = 600  # 同一个频道两次上网找图至少隔多久
    image_candidates: int = 4  # 上网找图时给它看几张候选
    image_max_bytes: int = 8_000_000  # 发出去的图最大多少（Discord 默认上传上限 10 MB）
    meme_list_limit: int = 80  # 告诉它表情包库里有哪些图，最多列多少个
    card_max_chars: int = 1600  # 一张人设卡最多多少字
    cards_per_round: int = 6  # 一轮最多附几个人的人设卡
    card_list_limit: int = 100  # 告诉它哪些人有人设卡，最多列多少个名字
    memory_max_entries: int = 50  # 每个服务器的长期记忆最多几条
    memory_entry_chars: int = 200  # 每条长期记忆最多多少字
    review_idle_seconds: float = 600  # 频道安静这么久、又攒了几轮新聊天，就整理一次记忆
    review_min_rounds: int = 3  # 至少攒了几轮新聊天才整理
    review_max_chars: int = 16000  # 整理记忆时一次给它看多少字的聊天记录（更长的分几次）
    # 最新的一条消息是别的 bot 发的（比如同一个频道里的 DeepSeek 版刚接过话）：先不主动插话，等有人再说话
    yield_to_bots: bool = True


@dataclass
class AppConfig:
    xai: XAIConfig = field(default_factory=XAIConfig)
    pricing: Pricing = field(default_factory=Pricing)
    bot: BotConfig = field(default_factory=BotConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    defaults: dict[str, Any] = field(default_factory=lambda: dict(BUILTIN_DEFAULTS))
    # 不认识的配置项（拼错了，或者 config.toml 比代码新）：跳过并在日志里提醒，不让 bot 因此起不来
    unknown: list[str] = field(default_factory=list)


def _fill(cls: type, data: dict[str, Any], section: str, unknown: list[str]) -> Any:
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            unknown.append(f"[{section}] {key}")
            continue
        kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path | None) -> AppConfig:
    data: dict[str, Any] = {}
    if path is not None and Path(path).exists():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)

    cfg = AppConfig()
    unknown = cfg.unknown
    if "xai" in data:
        cfg.xai = _fill(XAIConfig, data["xai"], "xai", unknown)
    if "bot" in data:
        cfg.bot = _fill(BotConfig, data["bot"], "bot", unknown)
    if "engine" in data:
        cfg.engine = _fill(EngineConfig, data["engine"], "engine", unknown)
    if "pricing" in data:
        cfg.pricing = _fill(Pricing, dict(data["pricing"]), "pricing", unknown)

    defaults = dict(BUILTIN_DEFAULTS)
    for key, raw in (data.get("defaults") or {}).items():
        if key not in SPECS:
            unknown.append(f"[defaults] {key}")
            continue
        defaults[key] = parse_value(SPECS[key], raw)  # 认识的参数填错了值还是要报错
    cfg.defaults = defaults
    for section in data:
        if section not in ("xai", "bot", "engine", "pricing", "defaults"):
            unknown.append(f"[{section}]")
    return cfg


def unknown_note(cfg: AppConfig) -> str | None:
    """给日志和自检看的提醒。"""
    if not cfg.unknown:
        return None
    return (
        "config.toml 里有不认识的配置项，已跳过：" + "、".join(cfg.unknown)
        + "（拼错了？或者 config.toml 比代码新：换了新版要重新构建）"
    )


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Secrets:
    discord_token: str
    xai_api_key: str
    owner_id: int
    control_guild_id: int | None = None
    discord_proxy: str | None = None
    tavily_api_key: str | None = None  # 可选：打开网页、上网找图都靠它；xAI 自带搜索用不了时也改用它


def load_secrets(env: dict[str, str] | None = None) -> Secrets:
    env = dict(os.environ) if env is None else env
    missing = [k for k in ("DISCORD_TOKEN", "XAI_API_KEY", "OWNER_ID") if not env.get(k, "").strip()]
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
        xai_api_key=env["XAI_API_KEY"].strip(),
        owner_id=owner_id,
        control_guild_id=control_id,
        discord_proxy=env.get("DISCORD_PROXY", "").strip() or None,
        tavily_api_key=env.get("TAVILY_API_KEY", "").strip() or None,
    )
