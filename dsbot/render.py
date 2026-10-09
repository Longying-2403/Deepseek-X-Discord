"""把一批新消息渲染成一条 user 消息。渲染结果写进数据库后不再改动（只追加，保证缓存命中）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .models import IncomingMessage
from .prompts import HINT_MENTION, HINT_PROACTIVE
from .textutil import one_line, truncate


def _attachments(m: IncomingMessage) -> str:
    bits = []
    if m.images:
        bits.append("[图片]" if len(m.images) == 1 else f"[图片×{len(m.images)}]")
    for name in m.files:
        bits.append(f"[文件: {one_line(name, 40)}]")
    for name in m.stickers:
        bits.append(f"[贴图: {one_line(name, 40)}]")
    return " ".join(bits)


def _neutralize(text: str) -> str:
    """群友的原话里不能出现我们用来标注的符号，否则可以伪造“〔@你〕”之类的标记。"""
    return text.replace("〔", "[").replace("〕", "]").replace("↪", "->")


def render_line(m: IncomingMessage, sid: int, sid_by_msg: dict[int, int], max_chars: int) -> str:
    head = f"#{sid} {_neutralize(one_line(m.author_name or '某人'))}"
    if m.is_bot:
        head += " 〔bot〕"
    if m.reply_to_bot:
        head += " ↪你"
    elif m.reply_to_id is not None:
        target = sid_by_msg.get(m.reply_to_id)
        head += f" ↪#{target}" if target is not None else " ↪(更早的消息)"

    body = _neutralize(truncate(m.text.strip(), max_chars))
    # 多行消息的续行缩进，群友没法伪造一条以 #编号 开头的新消息
    body = "\n".join(line if i == 0 else "  " + line for i, line in enumerate(body.split("\n")))
    attach = _attachments(m)
    if attach:
        body = f"{attach} {body}".strip()
    line = f"{head}：{body}"
    if m.mentions_bot and not m.is_bot:
        line += " 〔@你〕"
    return line


def _stamp(messages: list[tuple[int, IncomingMessage]], now: float, tz: str) -> str:
    zone = ZoneInfo(tz)
    end = datetime.fromtimestamp(now, zone)
    starts = [m.received_at for _, m in messages if m.received_at]
    if not starts or now - min(starts) < 300:
        return f"【{end:%m-%d %H:%M}】"
    start = datetime.fromtimestamp(min(starts), zone)
    tail = f"{end:%H:%M}" if start.date() == end.date() else f"{end:%m-%d %H:%M}"
    return f"【{start:%m-%d %H:%M} ~ {tail}】"


def render_round(
    messages: list[tuple[int, IncomingMessage]],
    *,
    sid_by_msg: dict[int, int],
    kind: str,
    targets: list[int],
    now: float,
    tz: str,
    omitted: int = 0,
    images: dict[int, list[tuple[str, str]]] | None = None,
    max_chars: int = 1200,
) -> str | list[dict[str, Any]]:
    """messages: [(编号, 消息)]；images: message_id -> [(data_uri, detail)]。

    没有图片时返回字符串；有图片时返回内容块数组，图片紧跟在它所属的那条消息后面。
    """
    lines = [_stamp(messages, now, tz)]
    if omitted > 0:
        lines.append(f"（前面还有 {omitted} 条消息没显示）")

    parts: list[dict[str, Any]] = []
    images = images or {}
    for sid, m in messages:
        lines.append(render_line(m, sid, sid_by_msg, max_chars))
        imgs = images.get(m.message_id) or []
        if imgs:
            parts.append({"type": "text", "text": "\n".join(lines)})
            for uri, detail in imgs:
                parts.append({"type": "image_url", "image_url": {"url": uri, "detail": detail}})
            lines = [f"（以上是 #{sid} 的图片）"]

    if kind == "mention" and targets:
        lines.append(HINT_MENTION.format(targets="、".join(f"#{t}" for t in targets)))
    else:
        lines.append(HINT_PROACTIVE)

    if not parts:
        return "\n".join(lines)
    parts.append({"type": "text", "text": "\n".join(lines)})
    return parts


def content_text(content: Any) -> str:
    """把存下来的 user 内容还原成纯文本（压缩摘要时用），图片换成占位。"""
    if isinstance(content, str):
        return content
    out = []
    for part in content or []:
        if part.get("type") == "text":
            out.append(part.get("text", ""))
        else:
            out.append("[图片]")
    return "\n".join(out)


def assistant_text(content: str, name: str) -> str:
    """把存下来的 assistant JSON 还原成可读的一句话（压缩摘要时用）。"""
    try:
        data = json.loads(content)
        actions = data.get("actions", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, AttributeError):
        return ""
    out = []
    for a in actions if isinstance(actions, list) else []:
        if not isinstance(a, dict):
            continue
        to = a.get("to")
        prefix = f"（回复 #{to}）" if to is not None else ""
        if a.get("type") == "reply":
            msgs = a.get("messages") or []
            if isinstance(msgs, str):
                msgs = [msgs]
            out.append(f"{name}{prefix}：" + " / ".join(str(x) for x in msgs))
        elif a.get("type") == "react":
            out.append(f"{name} 给 #{to} 加了表情 {a.get('emoji', '')}")
    return "\n".join(out)
