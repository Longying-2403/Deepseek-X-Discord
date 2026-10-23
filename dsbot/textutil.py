"""文本小工具：Discord 消息切分、打字延迟、清理。"""

from __future__ import annotations

import random
import re
import unicodedata
from functools import lru_cache
from typing import Callable, Iterable

from .hanzi import T2S

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


# 照念检查（人设卡的原文不能原样发出去）：统一写法（NFKC、不分大小写）之后，一个汉字（假名、谚文）算一个词，
# 一串连着的字母、数字算一个词；标点、空格、表情都不算（字中间加空格、换标点躲不过）。
# 人称代词当成同一个字：把卡上的“我”换成“他”照着念也认得出来；繁体字当成简体字：换成繁体照着念也认得出来
_CJK_CHARS = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U0003134f"
_QUOTE_TOKEN = re.compile(rf"[{_CJK_CHARS}]|[^\W_{_CJK_CHARS}]+")
_PRONOUNS = str.maketrans(dict.fromkeys("我你妳您他她它牠祂", "他"))
QUOTE_GRAM = 8  # 连着这么多个词一样才算抄了一段（称呼、口头禅一般比这短）
QUOTE_RUN = 16  # 连着这么长和卡上一样：照念（卡比这短的，整张一样才算）
QUOTE_TOTAL = 32  # 东抄一段西抄一段，加起来这么长也算（短一点的卡：抄到卡上四分之三、至少 QUOTE_RUN 个词就算）
QUOTE_MIN = 10  # 不到这么多个词的卡不查（多半就是个称呼，和正常的描述分不开）


def quote_tokens(text: str) -> list[str]:
    return _QUOTE_TOKEN.findall(unicodedata.normalize("NFKC", text).casefold().translate(T2S).translate(_PRONOUNS))


def _grams(tokens: list[str]) -> list[int]:
    return [hash(tuple(tokens[i : i + QUOTE_GRAM])) for i in range(len(tokens) - QUOTE_GRAM + 1)]


@lru_cache(maxsize=512)
def _card_grams(text: str) -> tuple[int, dict[int, int]]:
    """一张卡：(词数, 卡上每一段连着 QUOTE_GRAM 个词的片段 -> 在卡上的位置)。按原文缓存（卡改了就是新的一份），别改它。"""
    tokens = quote_tokens(text)
    index: dict[int, int] = {}
    for i, g in enumerate(_grams(tokens)):
        index.setdefault(g, i)
    return len(tokens), index


class Said:
    """群友公开说过的话（照着说不算泄露）。要用的时候才去找、才切词（多数时候用不上），算过一次就记着（一轮里要查好几次）。"""

    def __init__(self, make: Callable[[], Iterable[str]], base: Said | None = None):
        self._make = make
        self._base = base
        self._grams: set[int] | None = None

    def grams(self) -> set[int]:
        if self._grams is None:
            grams = {g for line in self._make() for g in _grams(quote_tokens(line))}
            self._grams = grams | self._base.grams() if self._base is not None else grams
        return self._grams

    def plus(self, lines: Iterable[str]) -> Said:
        """再加上几段（比如他自己卡上本来就有的）。"""
        extra = list(lines)
        return Said(lambda: extra, base=self)


def quotes_card(text: str, cards: Iterable[str], *, said: Iterable[str] | Said = ()) -> bool:
    """text 里有没有照念 cards 里某一张卡的原文：连着 QUOTE_RUN 个词一样（卡比这短的：整张都一样），
    或者东一段西一段（每段至少 QUOTE_GRAM 个词；编号列出来、调换顺序都算）加起来抄到卡上 QUOTE_TOTAL 个词
    （短一点的卡：卡的四分之三，至少 QUOTE_RUN 个词；只用上称呼加口头禅到不了）。用自己的话说、翻译、只用上称呼和口头禅都不算。
    said：群友公开说过的话，和这些话一样的片段不算（照着他们自己说过的话说，不算泄露）。"""
    return bool(quoted_cards(text, cards, said=said, first=True))


def touches_card(text: str, cards: Iterable[str], *, said: Iterable[str] | Said = ()) -> bool:
    """text 里有没有一段（连着 QUOTE_GRAM 个词）和 cards 里某张卡一样（不算 said 里的）。比 quotes_card 严得多：
    一张卡已经有很大一块在长期记忆里了，再往里记它的原话，哪怕一小段也不行。"""
    grams = set(_grams(quote_tokens(text)))
    if not grams:
        return False
    public: set[int] | None = None
    for card in dict.fromkeys(cards):
        n, own = _card_grams(card)
        if n < QUOTE_MIN or own.keys().isdisjoint(grams):
            continue
        if public is None:
            public = said.grams() if isinstance(said, Said) else {g for s in said for g in _grams(quote_tokens(s))}
        if any(g in own and g not in public for g in grams):
            return True
    return False


def quoted_cards(text: str, cards: Iterable[str], *, said: Iterable[str] | Said = (), first: bool = False) -> list[str]:
    """text 照念了 cards 里的哪几张（规则同 quotes_card）。text 只切一次词。first：找到一张就停。"""
    grams = _grams(quote_tokens(text))
    if not grams:
        return []
    public: set[int] | None = None
    found: list[str] = []
    for card in dict.fromkeys(cards):
        n, own = _card_grams(card)
        if n < QUOTE_MIN or own.keys().isdisjoint(grams):
            continue
        if public is None:
            public = said.grams() if isinstance(said, Said) else {g for s in said for g in _grams(quote_tokens(s))}
        streak = best = 0  # 连着几段都是照抄的
        covered: set[int] = set()  # 卡上被抄到的位置（同一句说几遍只算一次）
        for g in grams:
            p = own.get(g)
            if p is None or g in public:
                streak = 0
                continue
            streak += 1
            best = max(best, streak)
            covered.update(range(p, p + QUOTE_GRAM))
        enough = min(QUOTE_TOTAL, max(QUOTE_RUN, n * 3 // 4))  # 卡不到 QUOTE_TOTAL 个词时，固定的门槛永远到不了
        if (best and best + QUOTE_GRAM - 1 >= min(QUOTE_RUN, n)) or len(covered) >= enough:
            found.append(card)
            if first:
                break
    return found
