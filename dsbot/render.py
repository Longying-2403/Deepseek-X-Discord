"""把一批新消息渲染成一条 user 消息。渲染结果写进数据库后不再改动（只追加，保证缓存命中）。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .models import DocBlock, IncomingMessage
from .prompts import HINT_MENTION, HINT_PROACTIVE
from .textutil import one_line, truncate


def _attachments(m: IncomingMessage) -> str:
    bits = []
    for kind, label in (("image", "图片"), ("gif", "GIF")):
        n = sum(1 for i in m.images if i.kind == kind)  # 贴图的图下面按名字标
        if n:
            bits.append(f"[{label}]" if n == 1 else f"[{label}×{n}]")
    for f in m.files:
        bits.append(f"[文件: {_neutralize(one_line(getattr(f, 'filename', f), 40))}]")
    for name in m.stickers:
        bits.append(f"[贴图: {_neutralize(one_line(name, 40))}]")
    return " ".join(bits)


_MARKERS = str.maketrans({"〔": "[", "〕": "]", "【": "[", "】": "]", "↪": "->"})


def neutralize(text: str) -> str:
    """群友的原话和文件里不能出现我们用来标注的符号，否则可以伪造“〔@你〕”、文件块之类的标记。"""
    return text.translate(_MARKERS).replace("<<<", "‹‹‹").replace(">>>", "›››")


_neutralize = neutralize


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


def render_doc(block: DocBlock) -> tuple[list[str], list[str]]:
    """一个文件内容块 → (文字行, 图片 data URI)。内容放在 <<< 和 >>> 之间。"""
    result = block.result
    head = f"【{block.label} 的文件「{_neutralize(one_line(block.filename, 60))}」"
    if result.error:
        return [f"{head}· 读不了：{_neutralize(one_line(result.error, 200))}】"], []
    info = _neutralize(one_line(result.kind + (f"，{result.detail}" if result.detail else ""), 300))
    lines = [f"{head}· {info}】" if info else f"{head}】"]
    if result.text:
        lines += ["<<<", _neutralize(result.text), ">>>"]
    return lines, list(result.images)


class _Builder:
    """攒文字行，遇到图片就切成内容块。"""

    def __init__(self, first_line: str | None = None):
        self.lines: list[str] = [first_line] if first_line else []
        self.parts: list[dict[str, Any]] = []

    def images(self, uris: list[tuple[str, str]], after: str | None = None) -> None:
        if not uris:
            return
        self.parts.append({"type": "text", "text": "\n".join(self.lines)})
        for uri, detail in uris:
            self.parts.append({"type": "image_url", "image_url": {"url": uri, "detail": detail}})
        self.lines = [after] if after else []

    def docs(self, blocks: list[DocBlock]) -> None:
        for block in blocks:
            text_lines, uris = render_doc(block)
            self.lines += text_lines
            name = _neutralize(one_line(block.filename, 60))
            self.images([(u, "auto") for u in uris], after=f"（以上是 {block.label} 的文件「{name}」的页面）")

    def finish(self) -> str | list[dict[str, Any]]:
        if not self.parts:
            return "\n".join(self.lines)
        self.parts.append({"type": "text", "text": "\n".join(self.lines)})
        return self.parts


def _hint(kind: str, targets: list[int]) -> str:
    if kind == "mention" and targets:
        return HINT_MENTION.format(targets="、".join(f"#{t}" for t in targets))
    return HINT_PROACTIVE


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
    docs: dict[int, list[DocBlock]] | None = None,
    max_chars: int = 1200,
) -> str | list[dict[str, Any]]:
    """messages: [(编号, 消息)]；images: message_id -> [(data_uri, detail)]；docs: message_id -> 放在这条消息后面的文件块。

    没有图片时返回字符串；有图片时返回内容块数组，图片紧跟在它所属的那条消息后面。
    """
    out = _Builder(_stamp(messages, now, tz))
    if omitted > 0:
        out.lines.append(f"（前面还有 {omitted} 条消息没显示）")
    images = images or {}
    docs = docs or {}
    for sid, m in messages:
        out.lines.append(render_line(m, sid, sid_by_msg, max_chars))
        out.images(images.get(m.message_id) or [], after=f"（以上是 #{sid} 的图片）")
        out.docs(docs.get(m.message_id) or [])
    out.lines.append(_hint(kind, targets))
    return out.finish()


def render_read_result(blocks: list[DocBlock], notes: list[str], *, targets: list[int]) -> str | list[dict[str, Any]]:
    """read 动作之后，把读到的文件内容交给模型。"""
    out = _Builder("（你要看的文件：）")
    out.docs(blocks)
    out.lines += notes
    out.lines.append(_hint("mention", targets))
    return out.finish()


_CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """按 DeepSeek 文档的经验值估算：1 个中文字符 ≈ 0.6 token，1 个英文字符 ≈ 0.3 token。"""
    cjk = len(_CJK.findall(text))
    return int(cjk * 0.6 + (len(text) - cjk) * 0.3) + 4


def content_tokens(content: Any) -> int:
    if isinstance(content, str):
        return estimate_tokens(content)
    total = 4
    for part in content or []:
        if part.get("type") == "text":
            total += estimate_tokens(part.get("text", ""))
        elif part.get("type") == "image_url":
            total += 200 if (part.get("image_url") or {}).get("detail") == "low" else 800
    return total


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
        elif a.get("type") == "read":
            out.append(f"{name} 打开了 #{to} 的文件")
    return "\n".join(out)
