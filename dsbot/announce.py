"""更新公告：bot 主人用 /dsowner 公告 发，程序原样发出去（不问模型）。

三种范围：
- server（服务器）：发到设了公告频道的服务器（管理员用 /dsadmin 公告频道 设为；没设就是不收）。
- dm（私信）：私信给最近 DM_DAYS 天私信过它、没拒收的人（/ds-dm 更新公告 值:关 拒收）。
- all（全部）：两边都发，同一份内容。

发出去的公告不是模型写的：在发到的频道（私信）里记一条说明（Store.add_channel_note），下一轮带给它，有人问起它答得上来。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import __version__
from .whatsnew import NOTES

SCOPES = {"server": "服务器", "dm": "私信", "all": "全部"}
MAX_TITLE_CHARS = 80
MAX_CHARS = 1700  # 正文最多几个字（加上标题、拒收的那一行，不超过 Discord 一条消息的 2000 字）
DM_DAYS = 30  # 私信只发给最近这么多天私信过它的人
GAP_SECONDS = 1.5  # 一条一条发，中间隔一会儿（短时间内私信很多人，Discord 会限速）
NOTE_KEEP_SECONDS = 14 * 86400.0  # 记下的说明一直没轮到说话：留这么久
NOTE_CHARS = 1500

DM_FOOT = "-# 不想再收到更新公告：`/ds-dm 更新公告 值:关`"
NOTE = "（程序刚才替 bot 主人在这里发了一条更新公告，不是你写的，原文：{text}。有人问起就照这个答，没人问不用提。）"


def default_title() -> str:
    return f"更新公告 · v{__version__}"


def draft(scope: str, version: str = __version__) -> str:
    """编辑框里先填好的草稿：这一版里和这个范围有关的那几条（whatsnew.py）。"""
    wanted = {"server": ("server", "both"), "dm": ("dm", "both")}.get(scope, ("server", "dm", "both"))
    lines = [f"- {line}" for where, line in NOTES.get(version, ()) if where in wanted]
    return "\n".join(lines)[:MAX_CHARS]


def clean_title(title: str) -> str:
    return " ".join(title.split())[:MAX_TITLE_CHARS] or default_title()


def compose(title: str, text: str, *, dm: bool = False) -> str:
    """发出去的那条消息。dm：私信的那份，末尾多一行怎么拒收。"""
    out = f"📢 **{clean_title(title)}**\n{text.strip()[:MAX_CHARS]}"
    return f"{out}\n{DM_FOOT}" if dm else out


@dataclass
class Plan:
    """照现在的情况会发到哪。"""

    scope: str
    channels: list[tuple[int, int]] = field(default_factory=list)  # (服务器, 公告频道)
    unset: list[int] = field(default_factory=list)  # 没设公告频道的服务器（不收）
    gone: list[int] = field(default_factory=list)  # 设过，但那个频道已经没了
    people: list[tuple[int, int]] = field(default_factory=list)  # (人, 和他的私信频道)
    refused: int = 0  # 拒收的
    stale: int = 0  # 很久没私信的
    closed: int = 0  # 私信现在用不了的（挂的服务器没了、关掉了私信）

    @property
    def empty(self) -> bool:
        return not self.channels and not self.people


@dataclass
class Result:
    channels: list[tuple[int, int]] = field(default_factory=list)
    failed_channels: list[tuple[int, int]] = field(default_factory=list)
    dms: int = 0
    dm_failed: int = 0
