"""Configuration: secrets from .env + defaults from config.toml + definitions of the settings adjustable at runtime."""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .pricing import Pricing


# ---------------------------------------------------------------------------
# Definitions of the adjustable settings
# level = "admin": server admins can change it with /ds (server default, a category or a single channel)
# level = "owner": only you can change it (config.toml, or /dsowner in the CONTROL_GUILD_ID server)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    key: str
    label: str  # display name shown in the commands
    kind: type  # bool / int / float / str
    level: str  # "admin" | "owner"
    help: str
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] | None = None


_SPEC_LIST = [
    # —— admins ——
    Spec("enabled", "Allowed to respond", bool, "admin", "When off, it ignores messages here completely: no replies to @mentions, no proactive chat, nothing sent to DeepSeek"),
    Spec("proactive", "Proactive chat", bool, "admin", "Whether it decides on its own to speak when nobody calls it"),
    Spec("refill_turns", "Quota refill turns", int, "admin", "Each time other people take this many turns, 1 proactive message is added back to the quota", 1, 500),
    Spec("quota_capacity", "Quota cap", int, "admin", "The most proactive messages the quota can store up", 1, 20),
    Spec("min_speak_gap", "Min seconds between messages", int, "admin", "Minimum seconds between two of its messages", 0, 86400),
    Spec("quiet_seconds", "Lull seconds", int, "admin", "How long with nobody talking or typing counts as a lull", 5, 3600),
    Spec("max_wait_seconds", "Max wait seconds", int, "admin", "Once there are new messages, the longest it waits before checking anyway (even without a lull)", 30, 86400),
    Spec("mention_rate_limit", "Mentions per person per minute", int, "admin", "How many times per minute each person can call it", 1, 60),
    Spec("aliases", "Aliases", str, "admin", "Names that also count as calling it when they appear in a message, comma-separated, e.g. DS,Dee"),
    Spec("persona", "Persona", str, "admin", "Persona description; leave empty for the default persona"),
    Spec("share_memory", "Memory sharing", bool, "admin", "Share memory with other channels that also have sharing on (private channels must turn it on themselves to share out)"),
    # —— owner ——
    Spec("min_eval_interval", "Min seconds between checks", int, "owner", "Minimum seconds between two proactive checks (mainly controls cost)", 10, 86400),
    Spec("proactive_thinking", "Thinking for proactive checks", str, "owner", "Thinking level for proactive checks", choices=("off", "low", "high")),
    Spec("mention_thinking", "Thinking for mention replies", str, "owner", "Thinking level when it's called", choices=("off", "low", "high")),
    Spec("daily_budget_usd", "Daily budget (USD)", float, "owner", "Most USD a single server may spend per day; over it, it only answers mentions", 0, 1000),
    Spec("global_daily_budget_usd", "Global daily budget (USD)", float, "owner", "Most USD all servers together may spend per day", 0, 10000),
    Spec("context_budget_tokens", "Context budget (tokens)", int, "owner", "Once the context goes over this many tokens, old history is compacted into a summary", 8000, 200000),
    Spec("vision", "Vision", bool, "owner", "Whether images in the chat are sent to the model"),
    Spec("files", "Read files", bool, "owner", "Whether files people upload are read (text, code, PDF, Word, Excel, PowerPoint)"),
    Spec("web_search", "Web search", bool, "owner", "When it's called and needs current information, search the web (with DeepSeek's built-in search)"),
    Spec("daily_search_limit", "Daily search limit", int, "owner", "Most searches per server per day (0 = no searching)", 0, 1000),
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
    "quiet_seconds": 30,
    "max_wait_seconds": 180,
    "mention_rate_limit": 3,
    "aliases": "",
    "persona": "",
    "share_memory": False,
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
}

# One-click presets: they only touch the three settings that control how much it talks
PRESETS: dict[str, dict[str, int]] = {
    "Quiet": {"refill_turns": 30, "quota_capacity": 1, "min_speak_gap": 600},
    "Normal": {"refill_turns": 15, "quota_capacity": 3, "min_speak_gap": 120},
    "Chatty": {"refill_turns": 6, "quota_capacity": 4, "min_speak_gap": 30},
}

_TRUE = {"on", "true", "1", "yes", "y", "enable", "enabled"}
_FALSE = {"off", "false", "0", "no", "n", "disable", "disabled"}
RESET_WORDS = {"default", "reset", "clear"}  # compared case-insensitively


def parse_value(spec: Spec, raw: Any) -> Any:
    """Turn a string typed into a command (or a value from the toml) into the setting's value; raises ValueError with an explanation if it's invalid."""
    if spec.kind is bool:
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ValueError(f'"{spec.label}" must be on or off')
    if spec.kind is int:
        try:
            number = float(str(raw).strip())
            if not math.isfinite(number):
                raise ValueError
            value: Any = int(number)
        except (ValueError, OverflowError):
            raise ValueError(f'"{spec.label}" must be a whole number') from None
    elif spec.kind is float:
        try:
            value = float(str(raw).strip())
        except (ValueError, OverflowError):
            raise ValueError(f'"{spec.label}" must be a number') from None
        if not math.isfinite(value):
            raise ValueError(f'"{spec.label}" must be a regular number')
    else:
        value = str(raw).strip()
        if spec.choices is not None and value.lower() not in spec.choices:
            raise ValueError(f'"{spec.label}" must be one of: {" / ".join(spec.choices)}')
        if spec.choices is not None:
            value = value.lower()
        return value
    if spec.min is not None and value < spec.min:
        raise ValueError(f'"{spec.label}" can\'t be less than {spec.min:g}')
    if spec.max is not None and value > spec.max:
        raise ValueError(f'"{spec.label}" can\'t be more than {spec.max:g}')
    return value


def format_value(spec: Spec, value: Any) -> str:
    if spec.kind is bool:
        return "on" if value else "off"
    if spec.kind is float:
        return f"{value:g}"
    if spec.key == "persona":
        return "(default persona)" if not value else f"custom persona, {len(str(value))} characters"
    if value == "":
        return "(none)"
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
    # web search goes through DeepSeek's Anthropic-compatible API (with the built-in web_search tool); empty = base_url + "/anthropic"
    search_base_url: str = ""
    search_timeout_seconds: float = 60.0
    search_max_uses: int = 3  # max rounds of searching it may do within one search

    @property
    def anthropic_url(self) -> str:
        base = (self.search_base_url or self.base_url.rstrip("/") + "/anthropic").rstrip("/")
        return base + "/v1/messages"


@dataclass
class BotConfig:
    timezone: str = "Asia/Taipei"
    database: str = "data/dsbot.sqlite3"
    sync_commands_on_start: bool = True
    log_level: str = "INFO"
    tick_seconds: float = 5.0


@dataclass
class EngineConfig:
    max_batch_messages: int = 40  # max new messages per round (older ones are only counted)
    max_pending_messages: int = 120  # buffer cap, so it can't grow forever when nobody calls it for a long time
    pending_max_age_seconds: float = 21600  # messages older than 6 hours are dropped from the buffer
    max_chars_per_message: int = 1200  # each message is cut to this many characters before going into the context
    max_images_per_round: int = 4
    image_max_side: int = 1024  # images in the message that calls it: longest side in pixels
    low_detail_max_side: int = 512  # other images (detail=low; the model only looks at 512×512 anyway)
    file_max_bytes: int = 20_000_000  # files larger than this aren't read
    file_max_chars: int = 20000  # max characters read from a single file
    file_round_max_chars: int = 40000  # max characters from all files combined in one round
    max_files_per_round: int = 3
    file_timeout_seconds: float = 30.0  # max time to wait for parsing one file
    turn_merge_seconds: float = 20.0  # messages from the same person within this many seconds count as one turn
    react_cost: float = 0.3  # quota used by a reaction alone
    typing_chars_per_second: float = 3.0
    typing_min_seconds: float = 2.0
    typing_max_seconds: float = 10.0
    max_output_tokens: int = 2000  # without thinking
    max_thinking_tokens: int = 6000  # with thinking (including the reasoning)
    hard_stop_multiplier: float = 2.0  # once spending reaches this multiple of the budget, it doesn't even answer mentions
    summary_max_chars: int = 1500  # max length of the memory summary (600 in the Chinese version; English needs more characters for the same content)
    max_searches_per_round: int = 2  # max searches in a round where it's called
    search_max_chars: int = 2000  # max characters of one search result
    share_recent_hours: float = 6.0  # if the same person talked with it in another channel within this many hours, carry those lines over
    recall_max_chars: int = 3000  # max characters when looking through another channel's history
    share_list_limit: int = 30  # max channels listed when telling it which channels it can look through


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
            raise ValueError(f"config.toml [{section}] has an unknown option: {key}")
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
            raise ValueError(f"config.toml [defaults] has an unknown setting: {key}")
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
    tavily_api_key: str | None = None  # optional: use Tavily when DeepSeek's built-in search doesn't work


def load_secrets(env: dict[str, str] | None = None) -> Secrets:
    env = dict(os.environ) if env is None else env
    missing = [k for k in ("DISCORD_TOKEN", "DEEPSEEK_API_KEY", "OWNER_ID") if not env.get(k, "").strip()]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing) + " (fill in .env following .env.example)")
    try:
        owner_id = int(env["OWNER_ID"].strip())
        control = env.get("CONTROL_GUILD_ID", "").strip()
        control_id = int(control) if control else None
    except ValueError:
        raise SystemExit("OWNER_ID / CONTROL_GUILD_ID must be numeric IDs") from None
    return Secrets(
        discord_token=env["DISCORD_TOKEN"].strip(),
        deepseek_api_key=env["DEEPSEEK_API_KEY"].strip(),
        owner_id=owner_id,
        control_guild_id=control_id,
        discord_proxy=env.get("DISCORD_PROXY", "").strip() or None,
        tavily_api_key=env.get("TAVILY_API_KEY", "").strip() or None,
    )
