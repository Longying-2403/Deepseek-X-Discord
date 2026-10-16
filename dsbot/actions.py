"""Parse the JSON actions the model outputs. Be forgiving: the model sometimes uses a different field name or wraps it in a code block."""

from __future__ import annotations

import json
import re
from typing import Any

from .memory import parse_memory_op
from .models import Action

MAX_ACTIONS = 8
MAX_MESSAGES_PER_REPLY = 3
MAX_MESSAGE_CHARS = 1900
MAX_QUERY_CHARS = 200
MAX_PERSONA_PARSE_CHARS = 8000  # keep this much when parsing; anything over the persona limit (4000 characters) is rejected by the engine, which tells the model
MAX_CARD_PARSE_CHARS = 4000  # same for profile cards (the engine checks the limit from the config)
MAX_MEMORY_OPS = 3  # max long-term memory changes in one round
_MEMORY_KINDS = ("memory", "memo", "long_memory", "long_term_memory", "note", "长期记忆")
_CARD_KINDS = ("card", "set_card", "update_card", "persona_card", "profile_card", "人设卡")
_WHO_KINDS = ("who", "lookup_card", "find_card", "card_lookup", "查人设卡")
# Chinese spellings are still accepted too: the model sometimes answers in the chat's language
_SERVER_SCOPE = {
    "server", "guild", "whole server", "all", "entire server", "server-wide", "this server",
    "服务器", "全服务器", "整个服务器", "全服", "本服务器",
}  # fmt: skip
_TRUE_WORDS = {"true", "yes", "1", "是", "对"}


def _persona_scope(raw: Any) -> str | None:
    """Not given = the one currently used here; only an explicit whole-server value counts as server; anything else (channel, thread, category…) is treated as the narrowest, "this channel"."""
    text = str(raw or "").strip().lower()
    if not text or text in ("auto", "default", "here", "默认", "这里"):
        return None
    return "server" if text in _SERVER_SCOPE else "channel"

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
        if _ASCII_DIGITS.fullmatch(digits):  # str.isdigit() lets "①", "²" and the like through, but int() can't convert them
            return int(digits)
    return None


def _coerce_index(value: Any) -> int | None:
    """The number of a found picture: 2, "2", "Image 2", "#2", and the Chinese "图2" and "第2张" are all accepted."""
    if isinstance(value, str):
        value = value.strip().lower().removeprefix("image").removeprefix("图").removeprefix("第").removesuffix("张").strip()
    return _coerce_id(value)


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
    """Returns (actions, whether parsing succeeded). An empty list + True means the model decided to stay quiet."""
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
        elif kind in ("sticker", "send_sticker"):
            name = _text_field(item, "name", "sticker", "text")
            if name and not any(a.kind == "sticker" for a in actions):  # at most one sticker per round
                actions.append(Action("sticker", to, name=name))
        elif kind in ("meme", "send_meme", "表情包"):
            name = _text_field(item, "name", "meme", "text")
            if name and not any(a.kind == "meme" for a in actions):  # at most one meme per round
                actions.append(Action("meme", to, name=name))
        elif kind in ("image", "find_image", "image_search", "search_image", "picture") and _text_field(item, "query", "q", "keywords", "text"):
            query = _text_field(item, "query", "q", "keywords", "text")
            if not any(a.kind == "image" for a in actions):  # only one image search per round
                actions.append(Action("image", query=query))
        elif kind in ("pick", "send_image", "choose", "choose_image", "image"):
            # a mixed-up {"type": "image", "image": 2} also counts as sending a picture
            index = _coerce_index(item.get("image", item.get("pick", item.get("index", item.get("n")))))
            if index is not None and not any(a.kind == "pick" for a in actions):
                actions.append(Action("pick", to, image=index))
        elif kind in ("persona", "set_persona", "change_persona"):
            raw = item.get("text", item.get("persona", item.get("content")))
            text = raw.strip()[:MAX_PERSONA_PARSE_CHARS] if isinstance(raw, str) else None  # None: no new persona given
            undo = item.get("undo") is True or str(item.get("undo", "")).strip().lower() in _TRUE_WORDS
            if not any(a.kind == "persona" for a in actions):
                actions.append(Action("persona", to, text=text, scope=_persona_scope(item.get("scope")), undo=undo))
        elif kind in _CARD_KINDS:
            raw = item.get("text", item.get("card", item.get("content")))
            if isinstance(raw, str) and to is not None and not any(a.kind == "card" and a.to == to for a in actions):
                actions.append(Action("card", to, text=raw.strip()[:MAX_CARD_PARSE_CHARS]))
        elif kind in _MEMORY_KINDS or (kind == "remember" and not any(k in item for k in ("channel", "from", "where"))):
            op = parse_memory_op(item)
            if op is not None and sum(a.kind == "memory" for a in actions) < MAX_MEMORY_OPS:
                actions.append(Action("memory", op=op.op, index=op.no, text=op.text))
        elif kind in _WHO_KINDS:
            name = _text_field(item, "name", "who", "user", "query")
            if name and not any(a.kind == "who" and a.name == name for a in actions):
                actions.append(Action("who", name=name.lstrip("@＠ ").strip() or name))
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
                channel = channel.lstrip("#＃ ").strip()  # also strips a full-width ＃
            about = _text_field(item, "about", "query", "keywords")
            if channel and not any(a.kind == "recall" and a.channel == channel and a.query == about for a in actions):
                actions.append(Action("recall", channel=channel, query=about))
        elif kind in ("pass", "none", "silence"):
            continue
    return actions, True


def dump_actions(actions: list[Action]) -> str:
    """Normalized JSON, written into the history when the model output couldn't be parsed."""
    return json.dumps({"actions": [a.to_json() for a in actions]}, ensure_ascii=False)
