"""文本小工具：Discord 消息切分、打字延迟、清理。"""

from __future__ import annotations

import random
import re

DISCORD_LIMIT = 2000
_CUSTOM_EMOJI = re.compile(r"<a?:(\w{1,64}):\d{5,25}>")
_FENCE = re.compile(r"^\s*```(\S*)")
# 模型写的 :名字:（Discord 表情名只有英文字母、数字和下划线）。只换服务器里真有的名字，所以前后紧跟什么都行
# （“笑死:doge:”“:doge:666”“hhh:doge:”）；只是不能动已经写好的 <:名字:id> / <a:名字:id>
_EMOJI_SHORTCODE = re.compile(r"(?<!<)(?<!<a):([A-Za-z0-9_]{2,32}):")
_CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def clean_text(text: str) -> str:
    """去掉不成对的代理字符（\\ud800 之类）。它们存数据库、发请求时都会出错。"""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def clean_custom_emoji(text: str) -> str:
    """<:pepe:123> -> :pepe:"""
    return _CUSTOM_EMOJI.sub(r":\1:", text)


def render_custom_emoji(text: str, lookup: dict[str, str]) -> str:
    """把模型写的 :名字: 换成 Discord 的自定义表情 <:名字:id>。lookup：表情名 -> 发出去的写法。

    名字先精确匹配，再忽略大小写；不认识的名字原样留着。代码块和行内代码里的不动。
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
    return text[:limit] + "…（太长，后面省略）"


def split_for_discord(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """按行切成不超过 limit 的块；切在代码块中间时自动补上闭合/重新打开的 ```。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    budget = limit - 4  # 给闭合的 "\n```" 留位置
    chunks: list[str] = []
    lines: list[str] = []
    size = 0  # len("\n".join(lines))
    fence: str | None = None  # 当前所在代码块的开头（如 "```py"），不在代码块里为 None

    def flush() -> None:
        nonlocal lines, size
        if lines and lines != [fence]:
            body = "\n".join(lines)
            chunks.append(body + "\n```" if fence is not None else body)
        lines = [fence] if fence is not None else []
        size = len(fence) if fence is not None else 0

    for raw in text.split("\n"):
        # 每一段都保证放进一个新块（块开头可能有重新打开的 ```xx）也不会超长
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
    """按字数估算“打字”时间，加点随机，限制在 [low, high]。"""
    rng = rng or random
    seconds = len(text) / max(cps, 0.1) * rng.uniform(0.8, 1.2)
    return max(low, min(high, seconds))
