"""把一批新消息渲染成一条 user 消息。渲染结果写进数据库后不再改动（只追加，保证缓存命中）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .models import DocBlock, IncomingMessage
from .prompts import GALLERY_FOOT, GALLERY_HEAD, HINT_MENTION, HINT_PROACTIVE
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


_MARKERS = str.maketrans(
    {
        "〔": "[",
        "〕": "]",
        "【": "[",
        "】": "]",
        "↪": "->",
        # 长得像的括号也换掉，免得拿来冒充“〔dsowner〕”之类的标记
        "﹝": "[",
        "﹞": "]",
        "〘": "[",
        "〙": "]",
        "〖": "[",
        "〗": "]",
        "⦗": "[",
        "⦘": "]",
        "︹": "[",
        "︺": "]",
        "︻": "[",
        "︼": "]",
        "❲": "[",
        "❳": "]",
        "⟬": "[",
        "⟭": "]",
    }
)
# 别的换行写法统一成 \n：续行才会被缩进，没法伪造一条以 #编号 开头的新消息
_LINE_BREAKS = re.compile("\r\n?|[\u2028\u2029\x85\x0b\x0c]")


def neutralize(text: str) -> str:
    """群友的原话和文件里不能出现我们用来标注的符号，否则可以伪造“〔@你〕”、文件块之类的标记。"""
    text = _LINE_BREAKS.sub("\n", text)
    return text.translate(_MARKERS).replace("<<<", "‹‹‹").replace(">>>", "›››")


_neutralize = neutralize


def render_line(m: IncomingMessage, sid: int, sid_by_msg: dict[int, int], max_chars: int) -> str:
    head = f"#{sid} {_neutralize(one_line(m.author_name or '某人'))}"
    if m.is_bot:
        head += " 〔bot〕"
    elif m.from_owner:
        head += " 〔dsowner〕"  # 按 OWNER_ID 标的，群友改昵称冒充不了（〔〕在原话和名字里都会被中和）
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
    """一个文件（或打开的链接）内容块 → (文字行, 图片 data URI)。内容放在 <<< 和 >>> 之间。"""
    result = block.result
    source = "链接" if block.source == "链接" else "文件"
    head = f"【{block.label} 的{source}「{_neutralize(one_line(block.filename, 60))}」"
    if result.error:
        verb = "打不开" if source == "链接" else "读不了"
        return [f"{head}· {verb}：{_neutralize(one_line(result.error, 200))}】"], []
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
            if block.source == "链接":
                after = f"（以上是 {block.label} 的链接「{name}」里的图）"
            else:
                after = f"（以上是 {block.label} 的文件「{name}」的页面）"
            self.images([(u, "auto") for u in uris], after=after)

    def gallery(self, gallery: "Gallery") -> None:
        """上网找到的候选图：每张图前面一行「图N：说明」，整组放在 <<< 和 >>> 之间（压缩记忆时整组去掉）。"""
        self.lines += [GALLERY_HEAD.format(query=neutralize(one_line(gallery.query, 60)), count=len(gallery.items)), "<<<"]
        for i, (caption, uri) in enumerate(gallery.items, 1):
            self.lines.append(f"图{i}：{neutralize(one_line(caption, 160)) or '（没有说明）'}")
            self.images([(uri, "low")])
        self.lines += [">>>", GALLERY_FOOT]

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
    max_chars: int = 1800,
    context: list[str] | None = None,
) -> str | list[dict[str, Any]]:
    """messages: [(编号, 消息)]；images: message_id -> [(data_uri, detail)]；docs: message_id -> 放在这条消息后面的文件块。
    context：放在消息前面的背景（比如这个人刚才在别的频道跟它聊过什么）。

    没有图片时返回字符串；有图片时返回内容块数组，图片紧跟在它所属的那条消息后面。
    """
    out = _Builder(_stamp(messages, now, tz))
    out.lines += context or []
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
    return render_tool_result(blocks, notes, [], [], targets=targets)


@dataclass
class Gallery:
    """上网找图找到的候选图：[(说明, 缩小后的 data URI)]。"""

    query: str
    items: list[tuple[str, str]]


def render_tool_result(
    blocks: list[DocBlock],
    file_notes: list[str],
    sections: list[str],
    notes: list[str],
    *,
    targets: list[int],
    files: bool = True,
    header: str | None = None,
    gallery: Gallery | None = None,
    kind: str = "mention",
) -> str | list[dict[str, Any]]:
    """看文件、打开链接、搜索、找图、翻别的频道之后，把结果交给模型。

    files=False 表示这次没要看文件；header 可以换掉开头那句（比如这次要打开的是链接）。"""
    out = _Builder(header if header is not None else ("（你要看的文件：）" if files else None))
    out.docs(blocks)
    out.lines += file_notes
    out.lines += sections
    if gallery is not None and gallery.items:
        out.gallery(gallery)
    out.lines += notes
    out.lines.append(_hint(kind, targets))
    return out.finish()


def render_search(query: str, summary: str, sources: list[tuple[str, str]], max_chars: int) -> str:
    """一次搜索的结果。搜到的内容和文件一样是外面来的，标记符号都要中和掉。"""
    lines = [f"（你搜了「{neutralize(one_line(query, 80))}」：）", "<<<", neutralize(truncate(summary.strip(), max_chars)), ">>>"]
    if sources:
        lines.append("来源：" + "；".join(f"{neutralize(one_line(title, 60))} {neutralize(one_line(url, 300))}" for title, url in sources))
    return "\n".join(lines)


def render_recall(channel: str, summary: str | None, lines: list[str], about: str | None) -> str:
    """翻到的别的频道的记录。channel 是显示名（#频道 或 帖子「标题」），这里再保险处理一次。"""
    head = f"（你翻了 {neutralize(one_line(channel, 90))} 的记录"
    head += f"，找的是「{neutralize(one_line(about, 40))}」：）" if about else "：）"
    body: list[str] = []
    if summary:
        body.append("更早的摘要：" + neutralize(summary.strip()))
    body += [neutralize(line) for line in lines] or ["（没找到相关的内容）"]
    return "\n".join([head, "<<<", *body, ">>>"])


LIST_HEAD = "（能翻记录的别的频道："
LIST_EMPTY = "（现在没有能翻记录的别的频道）"
_FILE_HEAD = re.compile(r"^【\S+ 的(?:文件|链接)「")
_GALLERY_START = GALLERY_HEAD.split("「", 1)[0]  # “（你找了”


def render_channel_list(names: list[str], hidden: int = 0) -> list[str]:
    """能翻记录的别的频道（名单变了才写进记录）。"""
    if not names:
        return [LIST_EMPTY]
    lines = [LIST_HEAD] + ["  " + n for n in names]
    if hidden:
        lines.append(f"  （还有 {hidden} 个没列出来）")
    return lines + ["）"]


def last_listed(turns: list[Any]) -> tuple[str, ...] | None:
    """记录里最后一次写的频道名单（重新载入记录时用，名单没变就不用再写一遍）。"""
    for turn in reversed(turns):
        if getattr(turn, "role", None) != "user":
            continue
        lines = content_text(turn.content).split("\n")
        for i in range(len(lines) - 1, -1, -1):
            if lines[i] == LIST_EMPTY:
                return ()
            if lines[i] == LIST_HEAD:
                names = []
                for line in lines[i + 1 :]:
                    if not line.startswith("  "):
                        break
                    if not line.startswith("  （还有"):
                        names.append(line[2:])
                return tuple(names)
    return None


def own_text(content: Any) -> str:
    """压缩记忆用：这个频道自己的内容。去掉从别的频道带来的近况、频道名单、翻到的记录、搜索结果和找图的候选图。

    群友发的文件和链接打开后的内容算这个频道自己的，留着。

    顶格以“（”开头的行一定是 bot 自己加的（群友的消息都以 #编号 开头，续行缩进，文件内容在 <<< >>> 里）。
    """
    out: list[str] = []
    skipping_block = False  # 近况、名单：标题行后面跟着缩进的行
    skipping_fence = False  # 翻到的记录、搜索结果：标题行后面跟着 <<< … >>>
    file_fence = False  # 这个频道自己的文件内容：原样保留，里面的字不当成标记
    after_search = after_file = False
    for line in content_text(content).split("\n"):
        if file_fence:
            out.append(line)
            file_fence = line != ">>>"
            continue
        if skipping_fence:
            if line == ">>>":
                skipping_fence = False
            continue
        if skipping_block:
            if line.startswith("  "):
                continue
            skipping_block = False
            if line == "）":  # 名单的收尾
                continue
        if after_search and line.startswith("来源："):
            after_search = False
            continue
        if after_file and line == "<<<":
            out.append(line)
            file_fence, after_file = True, False
            continue
        after_search = after_file = False
        if line.startswith("（") and (line.endswith("跟你聊过：") or line == LIST_HEAD):
            skipping_block = True
        elif line.startswith(("（你翻了", "（你搜了", _GALLERY_START)):
            skipping_fence = True
            after_search = line.startswith("（你搜了")
        elif line != LIST_EMPTY and line != GALLERY_FOOT:
            out.append(line)
            after_file = bool(_FILE_HEAD.match(line))
    return "\n".join(out)


_MSG_LINE = re.compile(r"^#\d+ ")
_STAMP_LINE = re.compile(r"^【\d{2}-\d{2} \d{2}:\d{2}[^】]*】")
_REPLY_REF = re.compile(r" ↪(#\d+|\(更早的消息\))")


def history_lines(turns: list[tuple[str, Any]], bot_name: str) -> list[str]:
    """把存下来的对话还原成“名字：内容”的纯文本行（翻别的频道的记录时用）。

    去掉消息编号（那是那个频道自己的）、文件内容、提示语；时间行保留，方便看出是什么时候聊的。
    """
    from .actions import parse_actions

    out: list[str] = []
    for role, content in turns:
        if role != "user":
            raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            said = []
            for a in parse_actions(raw)[0]:
                if a.kind == "reply":
                    said += a.messages
                elif a.kind == "sticker" and a.name:
                    said.append(f"[贴图: {a.name}]")
                elif a.kind == "meme" and a.name:
                    said.append(f"[表情包: {a.name}]")
                elif a.kind == "pick":
                    said.append("[图片]")
            if said:
                out.append(f"{bot_name}：" + " / ".join(one_line(m, 300) for m in said))
            continue
        fenced = False
        continuing = False  # 上一行是消息，缩进的行是它的后续
        for line in content_text(content).split("\n"):
            if line == "<<<":
                fenced, continuing = True, False
            elif line == ">>>":
                fenced = False
            elif fenced:
                continue
            elif _STAMP_LINE.match(line):
                out.append(_STAMP_LINE.match(line).group(0))
                continuing = False
            elif _MSG_LINE.match(line):
                text = _REPLY_REF.sub("", _MSG_LINE.sub("", line, count=1)).replace(" 〔@你〕", "").replace(" 〔dsowner〕", "", 1)
                out.append(text)
                continuing = True
            elif continuing and line.startswith("  "):
                out[-1] = (out[-1] + " " + line.strip()).replace(" 〔@你〕", "")
            else:
                continuing = False  # 提示、文件头、别的频道的近况之类
    return out


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
            out.append(f"{name} 打开了 #{to} 的文件或链接")
        elif a.get("type") == "sticker":
            out.append(f"{name}{prefix} 发了贴图「{a.get('name', '')}」")
        elif a.get("type") == "meme":
            out.append(f"{name}{prefix} 发了表情包「{a.get('name', '')}」")
        elif a.get("type") == "pick":
            out.append(f"{name}{prefix} 发了一张网上找的图")
        elif a.get("type") == "image":
            out.append(f"{name} 上网找了「{a.get('query', '')}」的图")
        elif a.get("type") == "persona":
            out.append(f"{name} 想按 #{to} 的要求改人设")
    return "\n".join(out)
