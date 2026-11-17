"""把它说的话里的 @名字、#频道名 换成 Discord 真正的 @ 和频道链接（<@用户ID>、<#频道ID>）。

模型只知道名字，写出来的 @阿明 在 Discord 里只是普通文字。发出去之前按名字找到人和频道：
名字以聊天记录里显示的为准，从最长的开始对；名字后面要隔开（空格、标点或者结尾）：
“@阿明 快上线”会换，“@阿明快上线”“@大家”不换（免得有人叫“大”就把“@大家”当成 @ 他），“@Tom”也不会对上“@Tommy”。
对不上、或者同一个名字有好几个人的，保持原样。代码块、网址里的不动。@everyone、@here、身份组永远不换。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from .render import neutralize
from .textutil import one_line

# 代码、网址（youtube.com/@xxx 这种不带 https:// 的也算）
_SKIP = re.compile(r"```[\s\S]*?```|`[^`\n]*`|https?://\S+|www\.\S+|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}/\S*", re.I)
_WORD = re.compile(r"[A-Za-z0-9_]")
_NEVER = {"everyone", "here"}  # 就算有人叫这个名字也不换
MAX_NAME = 100  # Discord 的名字、频道名最长 100 字
# 写明了是哪个服务器的频道：“#大厅（「别的服」服务器）”“「别的服」服务器的 #大厅”，
# 还有别的服务器的帖子写成的“帖子「攻略」（在 #大厅）（「别的服」服务器）”
_SERVER_AFTER = re.compile(r"\s*(?:[）)]\s*)?[（(]「(?P<server>[^」\n]{1,100})」服务器[）)]")
# （近况里别的服务器的帖子写成“「别的服」服务器的 帖子「攻略」（在 #大厅）”）
_SERVER_BEFORE = re.compile(r"「(?P<server>[^」\n]{1,100})」服务器的\s*(?:帖子「[^」\n]*」\s*[（(]在\s*)?$")


def _other_server(text: str, start: int, size: int, other_server: Callable[[str], bool] | None) -> bool:
    """text[start] 是 #，后面 size 个字是频道名：前后写明了是哪个服务器的，other_server 说那不能当成这里（是 bot 知道的
    别的服务器，或者认不出是这里，见 Engine._other_server_named），就不换成这里的频道链接（会指到这个服务器同名的频道）。"""
    if other_server is None:
        return False
    found = _SERVER_AFTER.match(text, start + 1 + size) or _SERVER_BEFORE.search(text, 0, start)
    return found is not None and other_server(found["server"])


def name_forms(name: str) -> set[str]:
    """一个人的名字在聊天记录里可能的写法：原样，以及给模型看时中和、截短（32 字）之后的样子。"""
    name = " ".join(name.split())
    if not name:
        return set()
    return {name, neutralize(one_line(name))}


def _longest(rest: str, names: Mapping[str, frozenset[int]]) -> tuple[str, frozenset[int]] | None:
    """rest 开头能对上的最长的名字。"""
    best: tuple[str, frozenset[int]] | None = None
    for name, ids in names.items():
        size = len(name)
        if not size or size > len(rest) or (best is not None and size <= len(best[0])):
            continue
        if rest[:size].casefold() != name.casefold():
            continue
        if size < len(rest) and (rest[size].isalnum() or rest[size] == "_"):
            continue  # 名字后面紧跟着字、字母或数字：说的可能是别的（@Tom 对 @Tommy、“大”对 @大家）
        best = (name, ids)
    return best


def merge_tiers(tiers: list[Mapping[str, set[int]]]) -> dict[str, frozenset[int]]:
    """几层候选合成一份：同一个名字以靠前的那层为准（刚说过话的人 > 有人设卡的人 > 在服务器里找到的人）。"""
    merged: dict[str, frozenset[int]] = {}
    for tier in tiers:
        for name, ids in tier.items():
            if name and ids and name not in merged:
                merged[name] = frozenset(ids)
    return merged


def link(
    text: str,
    people: Mapping[str, frozenset[int]],
    channels: Mapping[str, frozenset[int]],
    *,
    exclude: frozenset[int] = frozenset(),
    other_server: Callable[[str], bool] | None = None,
) -> tuple[str, list[int], list[str]]:
    """换掉 text 里的 @名字 和 #频道名。返回 (换好的文字, 按出现顺序 @ 到的人, 没对上的 @ 后面那一段字)。

    exclude：不能 @ 的人（它自己）。没对上的那几段给按名字在服务器里找人用。
    other_server：写明的“「X」服务器”不能当成这里吗（是的话，那个频道不换成这里的链接；None = 都照常换）。"""
    out: list[str] = []
    users: list[int] = []
    unknown: list[str] = []
    last = 0
    for code in _SKIP.finditer(text):
        out.append(_link_plain(text[last : code.start()], people, channels, exclude, users, unknown, other_server))
        out.append(code.group(0))
        last = code.end()
    out.append(_link_plain(text[last:], people, channels, exclude, users, unknown, other_server))
    return "".join(out), users, unknown


def _link_plain(
    text: str,
    people: Mapping[str, frozenset[int]],
    channels: Mapping[str, frozenset[int]],
    exclude: frozenset[int],
    users: list[int],
    unknown: list[str],
    other_server: Callable[[str], bool] | None = None,
) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        # 前面紧挨着字母数字的不算（邮箱 a@b.com、C#），紧跟在“/”后面的也不算（网址的一部分）
        if ch in "@＠#＃" and (i == 0 or not (_WORD.match(text[i - 1]) or text[i - 1] == "/")):
            rest = text[i + 1 : i + 1 + MAX_NAME]
            if ch in "@＠":
                found = _longest(rest, people)
                if found is not None:
                    name, ids = found
                    if len(ids) == 1 and name.casefold() not in _NEVER and next(iter(ids)) not in exclude:
                        uid = next(iter(ids))
                        out.append(f"<@{uid}>")
                        users.append(uid)
                        i += 1 + len(name)
                        continue
                elif rest[:1] and not rest[:1].isspace() and rest.split(maxsplit=1)[0].casefold() not in _NEVER:
                    unknown.append(rest)
            else:
                found = _longest(rest, channels)
                # 纯数字的不换：#12 是它自己看的消息编号
                if found is not None and len(found[1]) == 1 and not found[0].isdigit() and not _other_server(text, i, len(found[0]), other_server):
                    out.append(f"<#{next(iter(found[1]))}>")
                    i += 1 + len(found[0])
                    continue
        out.append(ch)
        i += 1
    return "".join(out)
