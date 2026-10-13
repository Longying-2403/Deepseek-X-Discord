"""Small text helpers: splitting Discord messages, typing delay, cleanup."""

from __future__ import annotations

import random
import re

DISCORD_LIMIT = 2000
_CUSTOM_EMOJI = re.compile(r"<a?:(\w{1,64}):\d{5,25}>")
_FENCE = re.compile(r"^\s*```(\S*)")
# :name: as the model writes it (Discord emoji names are only ASCII letters, digits and underscores). Only names that really exist
# on the server get replaced, so anything may come right before or after ("lol:doge:", ":doge:!!!", "hhh:doge:"); it just must
# not touch an already-written <:name:id> / <a:name:id>
_EMOJI_SHORTCODE = re.compile(r"(?<!<)(?<!<a):([A-Za-z0-9_]{2,32}):")
_CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def clean_text(text: str) -> str:
    """Remove unpaired surrogate characters (like U+D800). They make both database writes and requests fail."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def clean_custom_emoji(text: str) -> str:
    """<:pepe:123> -> :pepe:"""
    return _CUSTOM_EMOJI.sub(r":\1:", text)


def render_custom_emoji(text: str, lookup: dict[str, str]) -> str:
    """Turn the :name: the model writes into Discord's custom emoji <:name:id>. lookup: emoji name -> how it's written when sent.

    Names are matched exactly first, then ignoring case; unknown names are left as they are. Nothing inside code blocks or inline code is touched.
    """
    if not lookup or ":" not in text:
        return text
    folded = {}
    for name, code in lookup.items():
        folded.setdefault(name.casefold(), code)

    def swap(m: re.Match[str]) -> str:
        name = m.group(1)
        return lookup.get(name) or folded.get(name.casefold()) or m.group(0)

    out: list[str] = []
    last = 0
    for code in _CODE_SPAN.finditer(text):
        out.append(_EMOJI_SHORTCODE.sub(swap, text[last : code.start()]))
        out.append(code.group(0))
        last = code.end()
    out.append(_EMOJI_SHORTCODE.sub(swap, text[last:]))
    return "".join(out)


def one_line(text: str, limit: int = 32) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "… (too long, the rest was cut)"


def split_for_discord(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split by lines into chunks of at most limit characters; when a cut falls inside a code block, close it and reopen it with ``` automatically."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    budget = limit - 4  # leave room for the closing "\n```"
    chunks: list[str] = []
    lines: list[str] = []
    size = 0  # len("\n".join(lines))
    fence: str | None = None  # the opening line of the current code block (like "```py"), or None outside a code block

    def flush() -> None:
        nonlocal lines, size
        if lines and lines != [fence]:
            body = "\n".join(lines)
            chunks.append(body + "\n```" if fence is not None else body)
        lines = [fence] if fence is not None else []
        size = len(fence) if fence is not None else 0

    for raw in text.split("\n"):
        # make sure every piece fits into a fresh chunk (which may start with a reopened ```xx) without going over
        width = max(1, budget - (len(fence) + 1 if fence is not None else 0))
        pieces = [raw[i : i + width] for i in range(0, len(raw), width)] or [""]
        for piece in pieces:
            added = len(piece) + (1 if lines else 0)
            if lines and size + added > budget:
                flush()
                added = len(piece) + (1 if lines else 0)
            lines.append(piece)
            size += added
        m = _FENCE.match(raw)
        if m:
            fence = None if fence is not None else "```" + m.group(1)
    if lines and lines != [fence]:
        chunks.append("\n".join(lines))
    return [c for c in chunks if c.strip()]


def typing_delay(text: str, cps: float, low: float, high: float, rng: random.Random | None = None) -> float:
    """Estimate "typing" time from the length, add a little randomness, and clamp it to [low, high]."""
    rng = rng or random
    seconds = len(text) / max(cps, 0.1) * rng.uniform(0.8, 1.2)
    return max(low, min(high, seconds))
