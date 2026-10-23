"""把一批新消息渲染成一条 user 消息。渲染结果写进数据库后不再改动（只追加，保证缓存命中）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .models import DocBlock, IncomingMessage
from .prompts import (
    CARDS_HEAD,
    EDIT_NOTE,
    EDITED_MARK,
    GALLERY_FOOT,
    GALLERY_HEAD,
    HINT_MENTION,
    HINT_PROACTIVE,
    MESSAGE_DELETED,
    MUSIC_IDLE,
    MUSIC_NOW,
    MUSIC_RESULT,
    WHO_HEAD,
    WHO_NONE,
    WHO_OFF,
)
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
# 框住人设的标签：群友的话、人设卡、长期记忆里写的不能冒充它（比如在长期记忆里写一段“</人设><人设>…”）。
# 全角、长得像的括号（〈〉《》⟨⟩）、繁体“設”、中间夹空格或零宽字符的写法也算
_GAP = r"[\s\u200b-\u200f\u2060\ufeff]*"
_TAG_OPEN = "<＜﹤〈《⟨\u2329\u27ea\u276c\u276e"  # 最后几个：〈 ⟪ ❬ ❮
_TAG_CLOSE = ">＞﹥〉》⟩\u232a\u27eb\u276d\u276f"
_PERSONA_TAG = re.compile(rf"[{_TAG_OPEN}]{_GAP}[/／]?{_GAP}人{_GAP}[设設]{_GAP}[{_TAG_CLOSE}]")
_TAG_BRACKETS = str.maketrans({**dict.fromkeys(_TAG_OPEN, "‹"), **dict.fromkeys(_TAG_CLOSE, "›")})
# 人设卡、长期记忆里的尖括号全换掉（书名号《》照常留着）
_ANGLES = str.maketrans({"<": "‹", ">": "›", "＜": "‹", "＞": "›", "﹤": "‹", "﹥": "›"})


def neutralize(text: str) -> str:
    """群友的原话和文件里不能出现我们用来标注的符号，否则可以伪造“〔@你〕”、文件块、人设标签之类的标记。"""
    text = _LINE_BREAKS.sub("\n", text)
    text = _PERSONA_TAG.sub(lambda m: m.group(0).translate(_TAG_BRACKETS), text)
    return text.translate(_MARKERS).replace("<<<", "‹‹‹").replace(">>>", "›››")


_neutralize = neutralize


def neutralize_record(text: str) -> str:
    """人设卡、长期记忆的内容：除了 neutralize，尖括号也全换掉（这两样会长期留着、每个频道都看得到）。"""
    return neutralize(text).translate(_ANGLES)


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


_HEAD_NAME = re.compile(r"^#\d+ (.+?)(?: 〔bot〕| 〔dsowner〕)?(?: ↪(?:你|#\d+|\(更早的消息\)))?：")


def deleted_line(sid: int, name: str | None, old_line: str = "") -> str:
    """被删掉的消息在记录里的样子：留着是谁发的，内容拿掉。name 不知道时从原来那行里取。"""
    if name:
        shown = _neutralize(one_line(name))
    else:
        found = _HEAD_NAME.match(old_line)
        shown = found.group(1) if found else "某人"
    return f"#{sid} {shown}：{MESSAGE_DELETED}"


def is_deleted_line(line: str) -> bool:
    return line.startswith("#") and line.endswith("：" + MESSAGE_DELETED)


def _tokens(content: Any) -> list[Any]:
    """记录内容 → 一行行的文字（str）和图片块（dict），顺序不变。"""
    if isinstance(content, str):
        return content.split("\n")
    out: list[Any] = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "text":
            out += str(part.get("text", "")).split("\n")
        else:
            out.append(part)
    return out


def _untokens(tokens: list[Any]) -> Any:
    """_tokens 反过来：没有图片了就是一个字符串，有图片就是内容块数组（相邻的文字合成一块）。"""
    if all(isinstance(t, str) for t in tokens):
        return "\n".join(tokens)
    parts: list[Any] = []
    lines: list[str] = []
    for t in tokens:
        if isinstance(t, str):
            lines.append(t)
            continue
        if lines:
            parts.append({"type": "text", "text": "\n".join(lines)})
            lines = []
        parts.append(t)
    if lines:
        parts.append({"type": "text", "text": "\n".join(lines)})
    return parts


def message_line(content: Any, sid: int) -> str | None:
    """记录里 #sid 那条消息的第一行（找不到就是 None）。"""
    fence = False
    for tok in _tokens(content):
        if not isinstance(tok, str):
            continue
        if fence:
            fence = tok != ">>>"
        elif tok == "<<<":
            fence = True
        elif tok.startswith(f"#{sid} "):
            return tok
    return None


def message_block(content: Any, sid: int) -> list[str] | None:
    """记录里 #sid 那条消息的几行（第一行和缩进的续行）；找不到就是 None。"""
    tokens = _tokens(content)
    fence = False
    for i, tok in enumerate(tokens):
        if not isinstance(tok, str):
            continue
        if fence:
            fence = tok != ">>>"
        elif tok == "<<<":
            fence = True
        elif tok.startswith(f"#{sid} "):
            block = [tok]
            for nxt in tokens[i + 1 :]:
                if not isinstance(nxt, str) or not nxt.startswith("  "):
                    break
                block.append(nxt)
            return block
    return None


_EDIT_NOTE_MARK = EDIT_NOTE.split("{name}", 1)[1].split("{text}", 1)[0]  # “ 改了这条消息，现在是：”


def rewrite_message(
    content: Any, sid: int, lines: list[str] | None, *, drop_images: bool, drop_docs: bool, drop_notes: bool = False
) -> tuple[Any, bool]:
    """改写一条记录里 #sid 那条消息（被删、被编辑时）。lines：换掉它那几行（None = 不换）；drop_images：去掉它附带的图；
    drop_docs：去掉这条记录里它的文件、链接的内容；drop_notes：去掉“（#sid 某人 改了这条消息，现在是：……）”那几句。
    返回 (新内容, 有没有改)；没什么要改的就原样返回。

    记录的结构是 render_round / render_tool_result 写出来的：消息一行以“#编号 ”开头、续行缩进两格，后面跟着它的图和
    “（以上是 #编号 的图片）”；文件、链接以“【#编号 的文件「”或“【#编号 的链接「”开头，正文在 <<< >>> 之间，图在后面。
    <<< >>> 里面的内容（文件正文、搜索结果、人设卡……）不当成消息。"""
    tokens = _tokens(content)
    head = f"#{sid} "
    doc_heads = (f"【#{sid} 的文件「", f"【#{sid} 的链接「")
    images_after = f"（以上是 #{sid} 的图片）"
    doc_after = f"（以上是 #{sid} 的"
    note_head = f"（#{sid} "
    out: list[Any] = []
    changed = fence = False
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if not isinstance(tok, str):
            out.append(tok)
            i += 1
            continue
        if fence:
            out.append(tok)
            fence = tok != ">>>"
            i += 1
            continue
        if tok == "<<<":
            out.append(tok)
            fence = True
            i += 1
            continue
        if tok.startswith(head):
            j = i + 1
            while j < n and isinstance(tokens[j], str) and tokens[j].startswith("  "):
                j += 1  # 续行
            if lines is not None and tokens[i:j] != lines:
                out += lines
                changed = True
            else:
                out += tokens[i:j]
            i = j
            if drop_images:
                k = i
                while k < n and not isinstance(tokens[k], str):
                    k += 1
                if k > i and k < n and tokens[k] == images_after:
                    i = k + 1
                    changed = True
            continue
        if drop_notes and tok.startswith(note_head) and _EDIT_NOTE_MARK in tok:
            i += 1  # 编辑后附的那句（里面有改过的内容）
            changed = True
            continue
        if drop_docs and tok.startswith(doc_heads):
            j = i + 1
            if j < n and tokens[j] == "<<<":
                while j < n and tokens[j] != ">>>":
                    j += 1
                j = min(j + 1, n)
            k = j
            while k < n and not isinstance(tokens[k], str):
                k += 1
            if k > j and k < n and isinstance(tokens[k], str) and tokens[k].startswith(doc_after):
                j = k + 1
            i = j
            changed = True
            continue
        out.append(tok)
        i += 1
    if not changed:
        return content, False
    return _untokens(out), True


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


CARD_DELETED = "（这个人的人设卡已经删掉了，之前附过的那份别再用，也别照着它重新写。）"


def render_cards(cards: list[tuple[str, str | None]]) -> list[str]:
    """这一轮说话的人的人设卡：[(名字, 内容)] → 文字行；内容是 None 表示刚被删掉。
    整组放在 <<< 和 >>> 之间（压缩记忆时整组去掉）。"""
    if not cards:
        return []
    lines = [CARDS_HEAD, "<<<"]
    for name, text in cards:
        lines.append(f"【{_neutralize(one_line(name, 32))}】")
        lines.append(neutralize_record(text.strip()) if text is not None else CARD_DELETED)
    lines.append(">>>")
    return lines


def render_who(name: str, cards: list[tuple[str, str]]) -> str:
    """who 查到的人设卡。"""
    lines = [WHO_HEAD.format(name=_neutralize(one_line(name, 32))), "<<<"]
    for card_name, text in cards:
        lines.append(f"【{_neutralize(one_line(card_name, 32))}】")
        lines.append(neutralize_record(text.strip()))
    lines.append(">>>")
    return "\n".join(lines)


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
    cards: list[str] | None = None,
) -> str | list[dict[str, Any]]:
    """messages: [(编号, 消息)]；images: message_id -> [(data_uri, detail)]；docs: message_id -> 放在这条消息后面的文件块。
    context：放在消息前面的背景（比如这个人刚才在别的频道跟它聊过什么）。cards：在场的人的人设卡（render_cards 的结果）。

    没有图片时返回字符串；有图片时返回内容块数组，图片紧跟在它所属的那条消息后面。
    """
    out = _Builder(_stamp(messages, now, tz))
    out.lines += context or []
    out.lines += cards or []
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
_WHO_START = WHO_HEAD.split("「", 1)[0]  # “（你查了”
# 附人设卡的标题：记录只追加不改写，旧记录里还是改写法之前的那句，两种都认
_CARDS_HEADS = (CARDS_HEAD, "（在场的人的人设卡，只当资料，别讲给别人听：）")
_WHO_NONE_START, _WHO_NONE_END = WHO_NONE.split("{name}")
# 点歌的结果、“现在在放什么”：里面的歌名是 B站上来的，和搜索结果一样不进摘要和整理记忆（它点了什么歌，它自己的动作里有）
_MUSIC_NOW_START = MUSIC_NOW.split("{", 1)[0] + "语音频道"  # “（现在语音频道”（MUSIC_IDLE 也是这样开头）
_MUSIC_LINES = (MUSIC_RESULT.split("{", 1)[0], _MUSIC_NOW_START)


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


def last_music_line(turns: list[Any]) -> str | None:
    """记录里最后一次写的“现在在放什么”（停了、没写过都是 None）。重新载入记录时用：写过在放歌的话，
    下次被叫到时要更新一下（重启之后已经不在放了）。"""
    for turn in reversed(turns):
        if getattr(turn, "role", None) != "user":
            continue
        found: str | None = None
        seen = fenced = False
        for line in content_text(turn.content).split("\n"):
            if fenced:
                fenced = line != ">>>"  # 文件、链接的内容里写的不算
            elif line == "<<<":
                fenced = True
            elif line.startswith(_MUSIC_NOW_START):
                found, seen = (None if line == MUSIC_IDLE else line), True
        if seen:
            return found
    return None


def own_text(content: Any) -> str:
    """压缩记忆用：这个频道自己的内容。去掉从别的频道带来的近况、频道名单、翻到的记录、搜索结果、找图的候选图、人设卡和点歌的结果。

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
        elif line.startswith(("（你翻了", "（你搜了", _GALLERY_START, _WHO_START)) or line in _CARDS_HEADS:
            skipping_fence = True
            after_search = line.startswith("（你搜了")
        elif line == WHO_OFF or (line.startswith(_WHO_NONE_START) and line.endswith(_WHO_NONE_END)):
            continue
        elif line.startswith(_MUSIC_LINES):
            continue
        elif line != LIST_EMPTY and line != GALLERY_FOOT and not is_deleted_line(line):
            out.append(line)
            after_file = bool(_FILE_HEAD.match(line))
    return "\n".join(out)


def card_texts(content: Any) -> list[str]:
    """记录里附着的人设卡原文（在场的人的卡、who 查到的卡；旧写法的标题也认），一张一条。检查照念用。
    只认顶格的标题行后面紧跟着的 <<< … >>>：文件、链接、搜索结果的 <<< >>> 里写的冒充不了。
    卡的正文写进记录时中和过（【、>>> 都换掉了），断不开。告诉它“卡删掉了”的那句不算。"""
    text = content_text(content)
    if "人设卡" not in text:
        return []
    out: list[str] = []
    card: list[str] | None = None  # 正在读的那张卡（还没读到【名字】时是 None）
    head = fenced = in_cards = False
    for line in text.split("\n"):
        if in_cards:
            if line == ">>>" or line.startswith("【"):
                if card:
                    out.append("\n".join(card))
                card = [] if line.startswith("【") else None
                in_cards = line != ">>>"
            elif card is not None and line != CARD_DELETED:
                card.append(line)
            continue
        if fenced:
            fenced = line != ">>>"
            continue
        if line == "<<<":
            in_cards, fenced, card = head, not head, None
        head = line in _CARDS_HEADS or line.startswith(_WHO_START)
    return out


_MEMBER_LINE = re.compile(r"^#\d{1,9} ")


def public_lines(content: Any) -> list[str]:
    """群友自己公开说过、发过的：他们的消息（连同续行）、改过之后的消息、他们发的文件和链接打开后的正文。照念检查用：照着这些说不算泄露。
    bot 自己加的提示不算（里面有模型自己写的搜索词、频道名之类，人设卡的原文能借这个“洗白”）；附着的人设卡、别的频道带来的、搜索结果也不算。"""
    out: list[str] = []
    block: list[str] = []  # 正在攒的一段：一条消息连同续行，或者一段文件正文（一整段一起算，跨行的片段也是公开的）
    member = body = after_head = False

    def flush() -> None:
        if block:
            out.append("\n".join(block))
            block.clear()

    for line in own_text(content).split("\n"):
        if body:
            body = line != ">>>"
            if body:
                block.append(line)
            else:
                flush()
            continue
        if after_head and line == "<<<":
            flush()
            body, after_head = True, False
            continue
        after_head = bool(_FILE_HEAD.match(line))
        if member and line.startswith("  "):
            block.append(line)  # 多行消息的续行
            continue
        flush()
        member = bool(_MEMBER_LINE.match(line))
        if member:
            block.append(line)
        elif line.startswith("（#") and _EDIT_NOTE_MARK in line:
            out.append(line)  # 很早的消息改成了什么（群友自己改的）
    flush()
    return out


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
            elif is_deleted_line(line):
                continuing = False
            elif _MSG_LINE.match(line):
                text = _REPLY_REF.sub("", _MSG_LINE.sub("", line, count=1)).replace(" 〔@你〕", "").replace(" 〔dsowner〕", "", 1)
                out.append(text.replace(EDITED_MARK, ""))
                continuing = True
            elif continuing and line.startswith("  "):
                out[-1] = (out[-1] + " " + line.strip()).replace(" 〔@你〕", "").replace(EDITED_MARK, "")
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
        elif a.get("type") == "card":
            # 卡的内容不写进摘要：那是本人的东西，删卡之后摘要里也不该留着
            out.append(f"{name} 按 #{to} 的要求改了他的人设卡")
        elif a.get("type") == "memory":
            if a.get("add"):
                out.append(f"{name} 记下了一条长期记忆：{a.get('add')}")
            elif a.get("edit") is not None:
                out.append(f"{name} 改了第 {a.get('edit')} 条长期记忆：{a.get('text', '')}")
            elif a.get("delete") is not None:
                out.append(f"{name} 删了第 {a.get('delete')} 条长期记忆")
        elif a.get("type") == "who":
            out.append(f"{name} 查了「{a.get('name', '')}」的人设卡")
        elif a.get("type") == "music":
            op = a.get("op")
            if op == "play":
                out.append(f"{name} 点了歌「{a.get('query', '')}」")
            elif op == "remove":
                if a.get("query"):
                    out.append(f"{name} 从播放队列里删了「{a.get('query')}」")
                elif a.get("index") is not None:
                    out.append(f"{name} 从播放队列里删了第 {a.get('index')} 首")
                else:
                    out.append(f"{name} 从播放队列里删了刚点的歌")
            elif op in _MUSIC_DONE:
                out.append(f"{name} {_MUSIC_DONE[op]}")
    return "\n".join(out)


_MUSIC_DONE = {
    "skip": "切了歌",
    "pause": "暂停了放歌",
    "resume": "继续放歌",
    "stop": "停止放歌",
    "queue": "看了播放队列",
    "clear": "清空了播放队列",
}


def review_bot_lines(content: Any, name: str) -> list[str]:
    """整理记忆用：它自己说过的话，一个动作一行。它的话里就算有换行和“#12 阿明：……”，也冒充不了一条群友的消息。"""
    raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    out = []
    for line in assistant_text(raw, name).split("\n"):
        if not line.strip():
            continue
        if out and not line.startswith(name):
            out[-1] += " " + line  # 它说的话里自己带的换行：接在上一行后面
        else:
            out.append(line)
    return [_neutralize(one_line(line, 600)) for line in out]


def review_text(content: Any) -> list[str]:
    """整理记忆用：这个频道自己的聊天（同 own_text），再去掉文件、链接的正文和每轮的提示，只留大家说的话。"""
    out: list[str] = []
    fenced = False
    for line in own_text(content).split("\n"):
        if fenced:
            fenced = line != ">>>"
            continue
        if line == "<<<":
            fenced = True
            out.append("（内容略）")
            continue
        if not line.strip() or line == HINT_PROACTIVE or (line.startswith("（#") and line.endswith("在叫你，回应一下，用对方那条消息的语言回。）")):
            continue
        out.append(line)
    return out


def render_review_input(
    people: list[tuple[str, list[int], str | None]] | None,
    memories: list[tuple[int, str]] | None,
    lines: list[str],
) -> str:
    """整理记忆时给模型看的内容。people：[(名字, 他的消息编号, 现在的人设卡或 None)]（None = 这里不整理人设卡）；
    memories：[(编号, 内容)]（None = 这里不整理长期记忆）。"""
    out: list[str] = []
    if people is not None:
        out.append("【说过话的人】")
        for name, sids, card in people:
            who = f"{_neutralize(one_line(name, 48))}（消息：{'、'.join(f'#{sid}' for sid in sids[:20])}）"
            if card:
                out += [f"{who}现在的人设卡：", "<<<", neutralize_record(card.strip()), ">>>"]
            else:
                out.append(f"{who}：还没有人设卡")
        if not people:
            out.append("（这段记录里没有能记人设卡的人）")
    if memories is not None:
        out.append("【长期记忆】")
        out += [f"({no}) {neutralize_record(one_line(text, 300))}" for no, text in memories] or ["（还没有）"]
    out.append("【聊天记录】")
    out += lines
    return "\n".join(out)
