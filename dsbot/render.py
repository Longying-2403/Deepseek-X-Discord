"""Render a batch of new messages into one user message. Once written to the database it never changes (append-only, so the cache keeps hitting)."""

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
    for kind, label in (("image", "image"), ("gif", "GIF")):
        n = sum(1 for i in m.images if i.kind == kind)  # sticker images are labeled by name below
        if n:
            bits.append(f"[{label}]" if n == 1 else f"[{label}×{n}]")
    for f in m.files:
        bits.append(f"[file: {_neutralize(one_line(getattr(f, 'filename', f), 40))}]")
    for name in m.stickers:
        bits.append(f"[sticker: {_neutralize(one_line(name, 40))}]")
    return " ".join(bits)


_MARKERS = str.maketrans(
    {
        "〔": "[",
        "〕": "]",
        "【": "[",
        "】": "]",
        "↪": "->",
        # lookalike brackets get replaced too, so they can't be used to fake markers like "〔dsowner〕"
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
# Other kinds of line breaks become \n: that way every continuation line gets indented, and nobody can forge a new message starting with #number
_LINE_BREAKS = re.compile("\r\n?|[\u2028\u2029\x85\x0b\x0c]")


def neutralize(text: str) -> str:
    """Our marker symbols must never appear in what people say or in files, or they could forge markers like "〔@you〕" or file blocks."""
    text = _LINE_BREAKS.sub("\n", text)
    return text.translate(_MARKERS).replace("<<<", "‹‹‹").replace(">>>", "›››")


_neutralize = neutralize


def render_line(m: IncomingMessage, sid: int, sid_by_msg: dict[int, int], max_chars: int) -> str:
    head = f"#{sid} {_neutralize(one_line(m.author_name or 'someone'))}"
    if m.is_bot:
        head += " 〔bot〕"
    elif m.from_owner:
        head += " 〔dsowner〕"  # set from OWNER_ID, so nobody can fake it by changing their nickname (〔〕 get neutralized in both messages and names)
    if m.reply_to_bot:
        head += " ↪you"
    elif m.reply_to_id is not None:
        target = sid_by_msg.get(m.reply_to_id)
        head += f" ↪#{target}" if target is not None else " ↪(earlier message)"

    body = _neutralize(truncate(m.text.strip(), max_chars))
    # Indent the extra lines of a multi-line message, so nobody can forge a new message starting with #number
    body = "\n".join(line if i == 0 else "  " + line for i, line in enumerate(body.split("\n")))
    attach = _attachments(m)
    if attach:
        body = f"{attach} {body}".strip()
    line = f"{head}: {body}" if body else f"{head}:"
    if m.mentions_bot and not m.is_bot:
        line += " 〔@you〕"
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
    """One file (or opened link) content block → (text lines, image data URIs). The content goes between <<< and >>>."""
    result = block.result
    source = "Link" if block.source == "link" else "File"
    head = f'【{source} "{_neutralize(one_line(block.filename, 60))}" from {block.label}'
    if result.error:
        verb = "couldn't open" if source == "Link" else "couldn't read"
        return [f"{head} · {verb}: {_neutralize(one_line(result.error, 200))}】"], []
    info = _neutralize(one_line(result.kind + (f", {result.detail}" if result.detail else ""), 300))
    lines = [f"{head} · {info}】" if info else f"{head}】"]
    if result.text:
        lines += ["<<<", _neutralize(result.text), ">>>"]
    return lines, list(result.images)


class _Builder:
    """Collects text lines and cuts them into content blocks whenever images come in."""

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
            if block.source == "link":
                after = f'(Above: images in the link "{name}" from {block.label})'
            else:
                after = f'(Above: pages of the file "{name}" from {block.label})'
            self.images([(u, "auto") for u in uris], after=after)

    def gallery(self, gallery: "Gallery") -> None:
        """Candidate pictures found online: each one gets an "Image N: description" line before it, and the whole set sits between <<< and >>> (it's dropped as a whole when memory is compacted)."""
        self.lines += [GALLERY_HEAD.format(query=neutralize(one_line(gallery.query, 60)), count=len(gallery.items)), "<<<"]
        for i, (caption, uri) in enumerate(gallery.items, 1):
            self.lines.append(f"Image {i}: {neutralize(one_line(caption, 160)) or '(no description)'}")
            self.images([(uri, "low")])
        self.lines += [">>>", GALLERY_FOOT]

    def finish(self) -> str | list[dict[str, Any]]:
        if not self.parts:
            return "\n".join(self.lines)
        self.parts.append({"type": "text", "text": "\n".join(self.lines)})
        return self.parts


def _hint(kind: str, targets: list[int]) -> str:
    if kind == "mention" and targets:
        return HINT_MENTION.format(targets=", ".join(f"#{t}" for t in targets))
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
    """messages: [(number, message)]; images: message_id -> [(data_uri, detail)]; docs: message_id -> file blocks placed after that message.
    context: background placed before the messages (e.g. what this person just talked about with it in another channel).

    Returns a string when there are no images; otherwise a list of content blocks, with each image right after the message it belongs to.
    """
    out = _Builder(_stamp(messages, now, tz))
    out.lines += context or []
    if omitted > 0:
        out.lines.append(f"({omitted} earlier message{'s' if omitted != 1 else ''} not shown)")
    images = images or {}
    docs = docs or {}
    for sid, m in messages:
        out.lines.append(render_line(m, sid, sid_by_msg, max_chars))
        out.images(images.get(m.message_id) or [], after=f"(Above: images from #{sid})")
        out.docs(docs.get(m.message_id) or [])
    out.lines.append(_hint(kind, targets))
    return out.finish()


def render_read_result(blocks: list[DocBlock], notes: list[str], *, targets: list[int]) -> str | list[dict[str, Any]]:
    """After a read action, hand the file contents to the model."""
    return render_tool_result(blocks, notes, [], [], targets=targets)


@dataclass
class Gallery:
    """Candidate pictures from an online image search: [(description, downsized data URI)]."""

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
    """After reading files, opening links, searching, finding images or looking through other channels, hand the results to the model.

    files=False means no files were requested this time; header can replace the opening line (e.g. when it's links being opened this time)."""
    out = _Builder(header if header is not None else ("(The files you asked to read:)" if files else None))
    out.docs(blocks)
    out.lines += file_notes
    out.lines += sections
    if gallery is not None and gallery.items:
        out.gallery(gallery)
    out.lines += notes
    out.lines.append(_hint(kind, targets))
    return out.finish()


def render_search(query: str, summary: str, sources: list[tuple[str, str]], max_chars: int) -> str:
    """The result of one search. Like files, search results come from outside, so every marker symbol gets neutralized."""
    lines = [f'(You searched for "{neutralize(one_line(query, 80))}":)', "<<<", neutralize(truncate(summary.strip(), max_chars)), ">>>"]
    if sources:
        lines.append("Sources: " + "; ".join(f"{neutralize(one_line(title, 60))} {neutralize(one_line(url, 300))}" for title, url in sources))
    return "\n".join(lines)


def render_recall(channel: str, summary: str | None, lines: list[str], about: str | None) -> str:
    """History looked up in another channel. channel is the display name (#channel or thread "title"); it's neutralized again here to be safe."""
    head = f"(You looked through the history of {neutralize(one_line(channel, 90))}"
    head += f', searching for "{neutralize(one_line(about, 40))}":)' if about else ":)"
    body: list[str] = []
    if summary:
        body.append("Earlier summary: " + neutralize(summary.strip()))
    body += [neutralize(line) for line in lines] or ["(Nothing relevant found)"]
    return "\n".join([head, "<<<", *body, ">>>"])


LIST_HEAD = "(Other channels whose history you can look through:"
LIST_EMPTY = "(No other channels whose history you can look through right now)"
_FILE_HEAD = re.compile(r'^【(?:File|Link) "')
_GALLERY_START = GALLERY_HEAD.split('"', 1)[0]  # "(You looked for images of "


def render_channel_list(names: list[str], hidden: int = 0) -> list[str]:
    """The other channels whose history it can look through (only written into the log when the list changes)."""
    if not names:
        return [LIST_EMPTY]
    lines = [LIST_HEAD] + ["  " + n for n in names]
    if hidden:
        lines.append(f"  ({hidden} more not listed)")
    return lines + [")"]


def last_listed(turns: list[Any]) -> tuple[str, ...] | None:
    """The last channel list written in the log (used when the log is reloaded, so an unchanged list isn't written again)."""
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
                    if not (line.startswith("  (") and line.endswith(" more not listed)")):
                        names.append(line[2:])
                return tuple(names)
    return None


def own_text(content: Any) -> str:
    """For memory compaction: this channel's own content. Drops recent talk carried over from other channels, channel lists, looked-up history, search results and candidate pictures from image searches.

    Files people posted and the contents of opened links count as this channel's own, so they're kept.

    A line starting with "(" at column 0 is always added by the bot itself (people's messages start with #number, their extra lines are indented, and file contents sit inside <<< >>>).
    """
    out: list[str] = []
    skipping_block = False  # recent talk, channel list: a heading line followed by indented lines
    skipping_fence = False  # looked-up history, search results: a heading line followed by <<< … >>>
    file_fence = False  # this channel's own file contents: kept as is, and nothing inside counts as a marker
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
            if line == ")":  # end of the channel list
                continue
        if after_search and line.startswith("Sources: "):
            after_search = False
            continue
        if after_file and line == "<<<":
            out.append(line)
            file_fence, after_file = True, False
            continue
        after_search = after_file = False
        if line.startswith("(") and (line.endswith(" ago:") or line == LIST_HEAD):  # "(Alex chatted with you in #x 5 minutes ago:"
            skipping_block = True
        elif line.startswith(("(You looked through the history of ", "(You searched for ", _GALLERY_START)):
            skipping_fence = True
            after_search = line.startswith("(You searched for ")
        elif line != LIST_EMPTY and line != GALLERY_FOOT:
            out.append(line)
            after_file = bool(_FILE_HEAD.match(line))
    return "\n".join(out)


_MSG_LINE = re.compile(r"^#\d+ ")
_STAMP_LINE = re.compile(r"^【\d{2}-\d{2} \d{2}:\d{2}[^】]*】")
_REPLY_REF = re.compile(r" ↪(#\d+|\(earlier message\))")


def history_lines(turns: list[tuple[str, Any]], bot_name: str) -> list[str]:
    """Turn a stored conversation back into plain "name: content" lines (used when looking through another channel's history).

    Drops message numbers (those belong to that channel), file contents and hints; time lines are kept so it's clear when things were said.
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
                    said.append(f"[sticker: {a.name}]")
                elif a.kind == "meme" and a.name:
                    said.append(f"[meme: {a.name}]")
                elif a.kind == "pick":
                    said.append("[image]")
            if said:
                out.append(f"{bot_name}: " + " / ".join(one_line(m, 300) for m in said))
            continue
        fenced = False
        continuing = False  # the previous line was a message; indented lines continue it
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
                text = _REPLY_REF.sub("", _MSG_LINE.sub("", line, count=1)).replace(" 〔@you〕", "").replace(" 〔dsowner〕", "", 1)
                out.append(text)
                continuing = True
            elif continuing and line.startswith("  "):
                out[-1] = (out[-1] + " " + line.strip()).replace(" 〔@you〕", "")
            else:
                continuing = False  # hints, file headers, recent talk from other channels and so on
    return out


_CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """Estimate with DeepSeek's documented rule of thumb: 1 Chinese character ≈ 0.6 tokens, 1 English character ≈ 0.3 tokens."""
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
    """Turn stored user content back into plain text (used for summaries), with images replaced by a placeholder."""
    if isinstance(content, str):
        return content
    out = []
    for part in content or []:
        if part.get("type") == "text":
            out.append(part.get("text", ""))
        else:
            out.append("[image]")
    return "\n".join(out)


def assistant_text(content: str, name: str) -> str:
    """Turn stored assistant JSON back into readable lines (used for summaries)."""
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
        prefix = f" (replying to #{to})" if to is not None else ""
        if a.get("type") == "reply":
            msgs = a.get("messages") or []
            if isinstance(msgs, str):
                msgs = [msgs]
            out.append(f"{name}{prefix}: " + " / ".join(str(x) for x in msgs))
        elif a.get("type") == "react":
            out.append(f"{name} reacted to #{to} with {a.get('emoji', '')}")
        elif a.get("type") == "read":
            out.append(f"{name} opened the files or links in #{to}")
        elif a.get("type") == "sticker":
            out.append(f'{name}{prefix} sent the sticker "{a.get("name", "")}"')
        elif a.get("type") == "meme":
            out.append(f'{name}{prefix} sent the meme "{a.get("name", "")}"')
        elif a.get("type") == "pick":
            out.append(f"{name}{prefix} sent a picture found online")
        elif a.get("type") == "image":
            out.append(f'{name} looked online for images of "{a.get("query", "")}"')
        elif a.get("type") == "persona":
            out.append(f"{name} set out to change its persona as #{to} asked")
    return "\n".join(out)
