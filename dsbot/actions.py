"""解析模型输出的 JSON 动作。尽量宽容：模型偶尔会换个字段名或包一层代码块。"""

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
MAX_PERSONA_PARSE_CHARS = 8000  # 解析时先留这么多；超过人设上限（4000 字）的由引擎拒绝并告诉模型
MAX_CARD_PARSE_CHARS = 4000  # 人设卡同理（上限由引擎按配置检查）
MAX_MEMORY_OPS = 3  # 一轮里最多改几条长期记忆
_MEMORY_KINDS = ("memory", "memo", "long_memory", "note", "长期记忆")
_CARD_KINDS = ("card", "set_card", "update_card", "persona_card", "人设卡")
_WHO_KINDS = ("who", "lookup_card", "find_card", "card_lookup", "查人设卡")
_MUSIC_KINDS = ("music", "play_music", "song", "play", "点歌", "放歌")
_DESKTOP_KINDS = ("desktop", "computer", "pc", "电脑")
_DESKTOP_OPS = {
    "open": "open", "launch": "open", "start": "open", "run": "open", "打开": "open", "开": "open", "启动": "open",
    "close": "close", "quit": "close", "exit": "close", "kill": "close", "stop": "close", "关": "close", "关掉": "close", "关闭": "close",
    "url": "url", "browse": "url", "open_url": "url", "link": "url", "网址": "url", "网页": "url",
}
MAX_DESKTOP = 3  # 一轮最多让电脑办几件事
MAX_MUSIC = 3  # 一轮最多点几首
MAX_REMOVE = 10  # 一轮最多从队列里删几首
_MUSIC_OPS = {
    **dict.fromkeys(("play", "add", "enqueue", "queue_add", "点歌", "放", "播放"), "play"),
    **dict.fromkeys(("skip", "next", "切歌", "下一首", "跳过"), "skip"),
    **dict.fromkeys(("pause", "暂停"), "pause"),
    **dict.fromkeys(("resume", "continue", "unpause", "继续"), "resume"),
    **dict.fromkeys(("stop", "leave", "disconnect", "停止", "停", "离开"), "stop"),
    **dict.fromkeys(("queue", "list", "now", "now_playing", "nowplaying", "status", "队列", "在放什么"), "queue"),
    **dict.fromkeys(("remove", "delete", "del", "drop", "unqueue", "dequeue", "删除", "删", "删歌", "移除", "去掉"), "remove"),
    **dict.fromkeys(("clear", "clear_queue", "clearqueue", "empty", "清空", "清空队列", "清掉"), "clear"),
}
_SERVER_SCOPE = {"server", "guild", "whole server", "all", "服务器", "全服务器", "整个服务器", "全服", "本服务器"}
_TRUE_WORDS = {"true", "yes", "1", "是", "对"}


def _persona_scope(raw: Any) -> str | None:
    """不写 = 改这里现在用的那份；明确是全服务器才算 server；其他写法（频道、帖子、类别……）一律按最小的“这个频道”。"""
    text = str(raw or "").strip().lower()
    if not text or text in ("auto", "default", "默认", "这里"):
        return None
    return "server" if text in _SERVER_SCOPE else "channel"

_FENCED = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S | re.I)
_OBJECT = re.compile(r"\{.*\}", re.S)
_ASCII_DIGITS = re.compile(r"[0-9]{1,18}")


def _coerce_id(value: Any) -> int | None:
    """消息编号、图和歌的序号：都是不大的数。大得离谱的不认（存不进 SQLite，写进记录后每一轮都会出错）。"""
    if isinstance(value, bool):
        return None
    number: int | None = None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        digits = value.strip().lstrip("#")
        if _ASCII_DIGITS.fullmatch(digits):  # str.isdigit() 会放过 "①" "²" 之类，int() 却转不了
            number = int(digits)
    return number if number is not None and abs(number) < 2**31 else None


def _coerce_index(value: Any) -> int | None:
    """找到的图的编号：2、"2"、"图2"、"#2"、"第2张" 都认。"""
    if isinstance(value, str):
        value = value.strip().removeprefix("图").removeprefix("第").removesuffix("张").strip()
    return _coerce_id(value)


def _music_numbers(value: Any) -> tuple[list[int], list[str]]:
    """music remove 的 index：2、"2"、[2, 3] → 队列里的编号；别的写法（"第2首"、"2-4"、"稻香"）另外返回，当成 query。"""
    numbers: list[int] = []
    words: list[str] = []
    for v in (value if isinstance(value, list) else [value])[:MAX_REMOVE]:
        number = None
        if isinstance(v, str):
            number = _coerce_id(v)  # 只认纯数字（"2"、"#2"）；"第2首"“2-4”这些原样交给点歌那边认（它们不算歌名《1874》）
            name = " ".join(v.split())[:MAX_QUERY_CHARS]
            if number is None and name and name not in words:
                words.append(name)
        elif isinstance(v, (int, float)):
            number = _coerce_id(v)
        if number is not None and number not in numbers:
            numbers.append(number)
    return numbers, words


def _music_removals(item: dict[str, Any], to: int | None, query: str | None) -> list[Action]:
    """{"op": "remove"}：写了 query（歌名）就按歌名删（编号可能是它猜的，歌名不会错位），没写就按 index 删，
    都没写就是删叫它的那个人自己最近点的那首。"""
    raw = next((item[k] for k in ("index", "number", "no", "position", "n") if item.get(k) is not None), None)
    numbers, words = _music_numbers(raw) if raw is not None else ([], [])
    if query:
        return [Action("music", to, op="remove", query=query)]
    out = [Action("music", to, op="remove", index=n) for n in numbers]
    out += [Action("music", to, op="remove", query=w) for w in words]  # index 里写成了“第2首”、歌名之类
    return out or [Action("music", to, op="remove")]


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
        elif kind in ("sticker", "send_sticker"):
            name = _text_field(item, "name", "sticker", "text")
            if name and not any(a.kind == "sticker" for a in actions):  # 一轮最多一张贴图
                actions.append(Action("sticker", to, name=name))
        elif kind in ("meme", "send_meme", "表情包"):
            name = _text_field(item, "name", "meme", "text")
            if name and not any(a.kind == "meme" for a in actions):  # 一轮最多一张表情包
                actions.append(Action("meme", to, name=name))
        elif kind in ("image", "find_image", "image_search", "search_image", "picture") and _text_field(item, "query", "q", "keywords", "text"):
            query = _text_field(item, "query", "q", "keywords", "text")
            if not any(a.kind == "image" for a in actions):  # 一轮只找一次图
                actions.append(Action("image", query=query))
        elif kind in ("pick", "send_image", "choose", "choose_image", "image"):
            # {"type": "image", "image": 2} 这种写混了的也当成发图
            index = _coerce_index(item.get("image", item.get("pick", item.get("index", item.get("n")))))
            if index is not None and not any(a.kind == "pick" for a in actions):
                actions.append(Action("pick", to, image=index))
        elif kind in ("persona", "set_persona", "change_persona"):
            raw = item.get("text", item.get("persona", item.get("content")))
            text = raw.strip()[:MAX_PERSONA_PARSE_CHARS] if isinstance(raw, str) else None  # None：没写新的人设
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
        elif kind in _MUSIC_KINDS:
            raw_op = str(item.get("op", item.get("action", item.get("do", ""))) or "").strip().lower()
            query = _text_field(item, "query", "song", "q", "url", "link", "name", "title", "text")
            op = _MUSIC_OPS.get(raw_op) or ("play" if not raw_op and query else None)
            if op == "queue" and query:
                op = "play"  # {"op": "queue", "query": "稻香"}：是要加进队列
            if op == "remove":
                removes = [a for a in actions if a.kind == "music" and a.op == "remove"]
                for removal in _music_removals(item, to, query):
                    same = any((a.to, a.query, a.index) == (removal.to, removal.query, removal.index) for a in removes)
                    if len(removes) < MAX_REMOVE and not same:
                        actions.append(removal)
                        removes.append(removal)
                continue
            if op == "play" and not query:
                continue
            if op is not None and op != "play":
                query = None
            plays = [a.query for a in actions if a.kind == "music" and a.op == "play"]
            if op == "play" and len(plays) < MAX_MUSIC and query not in plays:
                actions.append(Action("music", to, query=query, op=op))
            elif op is not None and op != "play" and not any(a.kind == "music" and a.op == op for a in actions):
                actions.append(Action("music", to, op=op))
        elif kind in _DESKTOP_KINDS:
            op = _DESKTOP_OPS.get(str(item.get("op", item.get("action", "")) or "").strip().lower())
            target = _text_field(item, "target", "app", "program", "name", "url", "link")
            if op is None and target and target.lower().startswith(("http://", "https://")):
                op = "url"
            same = any(a.kind == "desktop" and (a.op, a.query) == (op, target) for a in actions)
            if op and target and not same and sum(a.kind == "desktop" for a in actions) < MAX_DESKTOP:
                actions.append(Action("desktop", to, op=op, query=target[:500]))
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
