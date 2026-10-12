"""解析模型输出的 JSON 动作。尽量宽容：模型偶尔会换个字段名或包一层代码块。"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import Action

MAX_ACTIONS = 6
MAX_MESSAGES_PER_REPLY = 3
MAX_MESSAGE_CHARS = 1900
MAX_QUERY_CHARS = 200

_FENCED = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S | re.I)
_OBJECT = re.compile(r"\{.*\}", re.S)
_ASCII_DIGITS = re.compile(r"[0-9]{1,18}")


def _coerce_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        digits = value.strip().lstrip("#")
        if _ASCII_DIGITS.fullmatch(digits):  # str.isdigit() 会放过 "①" "²" 之类，int() 却转不了
            return int(digits)
    return None


def _text_field(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, list):
            value = " ".join(str(v) for v in value if isinstance(v, (str, int, float)))
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = " ".join(str(value).split())[:MAX_QUERY_CHARS]
            if text:
                return text
    return None


def _load(text: str) -> Any:
    text = text.strip()
    m = _FENCED.match(text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _OBJECT.search(text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def parse_actions(content: str) -> tuple[list[Action], bool]:
    """返回 (动作列表, 是否成功解析)。空列表 + True 表示模型决定不说话。"""
    if not content or not content.strip():
        return [], False
    data = _load(content)
    if data is None:
        return [], False

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if str(data.get("action", "")).lower() == "pass" or str(data.get("type", "")).lower() == "pass":
            return [], True
        if "actions" in data:
            items = data["actions"]
            if items is None or (isinstance(items, str) and items.strip().lower() in ("pass", "none", "")):
                return [], True
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                return [], False
        elif "type" in data:
            items = [data]
        else:
            return [], False
    else:
        return [], False

    actions: list[Action] = []
    reacted: set[int | None] = set()
    for item in items[:MAX_ACTIONS]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", "")).strip().lower()
        to = _coerce_id(item.get("to"))
        if kind in ("reply", "message", "say", "send", "text"):
            raw = item.get("messages")
            if raw is None:
                raw = item.get("message", item.get("text", item.get("content")))
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                continue
            msgs = [m.strip()[:MAX_MESSAGE_CHARS] for m in raw if isinstance(m, str) and m.strip()]
            if msgs:
                actions.append(Action("reply", to, msgs[:MAX_MESSAGES_PER_REPLY]))
        elif kind in ("react", "reaction", "emoji"):
            emoji = item.get("emoji")
            if isinstance(emoji, str) and 0 < len(emoji.strip()) <= 64 and to not in reacted:
                reacted.add(to)
                actions.append(Action("react", to, emoji=emoji.strip()))
        elif kind in ("read", "read_file", "open", "look"):
            if to is not None and not any(a.kind == "read" and a.to == to for a in actions):
                actions.append(Action("read", to))
        elif kind in ("search", "web_search", "websearch", "google", "lookup"):
            query = _text_field(item, "query", "q", "keywords", "text")
            if query and not any(a.kind == "search" and a.query == query for a in actions):
                actions.append(Action("search", query=query))
        elif kind in ("recall", "remember", "history", "look_back"):
            channel = _text_field(item, "channel", "from", "where")
            if channel:
                channel = channel.lstrip("#＃ ").strip()
            about = _text_field(item, "about", "query", "keywords")
            if channel and not any(a.kind == "recall" and a.channel == channel and a.query == about for a in actions):
                actions.append(Action("recall", channel=channel, query=about))
        elif kind in ("pass", "none", "silence"):
            continue
    return actions, True


def dump_actions(actions: list[Action]) -> str:
    """规范化的 JSON，用于模型输出解析失败时写进历史。"""
    return json.dumps({"actions": [a.to_json() for a in actions]}, ensure_ascii=False)
