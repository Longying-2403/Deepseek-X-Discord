"""提醒：到点了它来说一声（2.14.0）。这里只有不碰 Discord、不碰模型的部分：认时间、写给人看的那几句话。

提醒是群友被叫到时口头说的（"明天九点提醒我开会"），模型写一个 remind 动作，程序把时间换算好、发一条带按钮的
确认消息，本人点了“确认”才真的设上。到点由程序直接发（不再问模型），所以到点的那句话是设的时候就写好的。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

MAX_TEXT_CHARS = 200  # 提醒的内容最多多少字
MAX_SAY_CHARS = 300  # 到点时说的那句话最多多少字
MIN_AHEAD = 30.0  # 至少是这么多秒以后的事
MAX_AHEAD = 366 * 86400.0  # 最远设到一年以后
PENDING_SECONDS = 600.0  # 确认消息多久没人点就作废
KEEP_SECONDS = 3 * 86400.0  # 错过了的在列表里留几天
BUTTON = "dsr"  # 确认按钮的 custom_id：dsr:ok:编号 / dsr:no:编号

_FULL = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?[ T]*(\d{1,2})[:：点时](\d{1,2})?分?(?::\d{1,2})?$")
_SHORT = re.compile(r"^(\d{1,2})[-/.月](\d{1,2})日?[ T]*(\d{1,2})[:：点时](\d{1,2})?分?(?::\d{1,2})?$")
_CLOCK = re.compile(r"^(\d{1,2})[:：点时](\d{1,2})?分?(?::\d{1,2})?$")
_WEEK = "一二三四五六日"


@dataclass
class Reminder:
    id: int
    owner_id: int  # 谁让它提醒的（只有他能确认、取消）
    owner_name: str
    guild_id: int  # 在哪个服务器说的（私信里说的：私信挂在的那个服务器）
    channel_id: int  # 在哪个频道说的；不是私信提醒的就发在这里
    dm: bool  # 到点了发私信（只给一个人）
    private: bool  # 是在私信里说的（只提醒他自己）
    targets: list[tuple[int, str]] = field(default_factory=list)  # 要提醒的人 [(ID, 名字)]
    text: str = ""  # 提醒什么
    say: str | None = None  # 到点时说的那句话（模型设的时候写的；没写就用“提醒：内容”）
    due_at: float = 0.0
    created_at: float = 0.0
    status: str = "pending"  # pending（等确认）/ set / done / missed / failed / cancelled


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_when(at: Any, minutes: Any, now: float, tz: str) -> float | None:
    """模型写的时间 → 时间戳；认不出来是 None（在不在允许的范围里由 check_when 管）。
    minutes：多少分钟以后（"30 分钟后"这类用它，不用模型自己算钟点）。at：具体的时间，照 tz 的钟点算，
    "2026-11-21 09:00"；没写年份的（"11-21 09:00"）、只写了钟点的（"09:00"）取今后最近的那一次。"""
    ahead = _number(minutes)
    if ahead is not None:
        return now + ahead * 60.0 if 0 < ahead <= MAX_AHEAD / 60.0 + 1 else None
    if not isinstance(at, str):
        return None
    raw = " ".join(at.strip().split())
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(now, zone)
    try:
        if m := _FULL.match(raw):
            year, month, day, hour, minute = (int(g) if g else 0 for g in m.groups())
            return datetime(year, month, day, hour, minute, tzinfo=zone).timestamp()
        if m := _SHORT.match(raw):
            month, day, hour, minute = (int(g) if g else 0 for g in m.groups())
            when = datetime(local.year, month, day, hour, minute, tzinfo=zone)
            if when.timestamp() <= now:
                when = datetime(local.year + 1, month, day, hour, minute, tzinfo=zone)
            return when.timestamp()
        if m := _CLOCK.match(raw):
            hour, minute = (int(g) if g else 0 for g in m.groups())
            when = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when.timestamp() <= now:
                when += timedelta(days=1)
            return when.timestamp()
        return datetime.fromisoformat(raw).replace(tzinfo=zone).timestamp() if "T" in raw or "-" in raw else None
    except (ValueError, OverflowError):
        return None  # 13 月、25 点、2 月 30 日之类


def check_when(due: float | None, now: float) -> str | None:
    """这个时间能不能设：能是 None，不能返回原因。"""
    if due is None:
        return "时间没看懂"
    if due < now + MIN_AHEAD:
        return "这个时间已经过了"
    if due > now + MAX_AHEAD:
        return "最远只能设到一年以后"
    return None


def span_text(seconds: float) -> str:
    """多久：45 秒、12 分钟、3 小时 5 分钟、2 天 4 小时。"""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)} 秒"
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days} 天" + (f" {hours} 小时" if hours else "")


def when_text(due: float, now: float, tz: str) -> str:
    """给人核对的时间：2026-11-21 周六 09:00（3 小时 5 分钟后）。今年的不写年份。"""
    zone = ZoneInfo(tz)
    when = datetime.fromtimestamp(due, zone)
    year = "" if when.year == datetime.fromtimestamp(now, zone).year else f"{when.year}-"
    text = f"{year}{when:%m-%d} 周{_WEEK[when.weekday()]} {when:%H:%M}"
    if due > now:
        text += f"（{span_text(due - now)}后）"
    return text


def clean(text: Any, limit: int) -> str:
    """模型写的内容：压成一行、去掉会真的 @ 到人的写法、截到上限。"""
    if not isinstance(text, str):
        return ""
    one = " ".join(text.split()).replace("@everyone", "@​everyone").replace("@here", "@​here")
    one = re.sub(r"<@[!&]?\d+>", "", one).strip()
    return one[:limit]


def whom_text(r: Reminder, *, you: int | None = None) -> str:
    """提醒谁：“你”、名字、“你、阿明”。you：看这句话的人（是他就写“你”）。"""
    names = ["你" if uid == you else name for uid, name in r.targets]
    return "、".join(names) or "你"


def where_text(r: Reminder) -> str:
    if r.private:
        return "私信"
    return "私信" if r.dm else f"<#{r.channel_id}>"


def describe(r: Reminder, now: float, tz: str, *, you: int | None = None) -> str:
    """一条提醒写成一行（列表、确认消息用）。"""
    who = whom_text(r, you=you)
    where = "发私信" if r.dm or r.private else "在这个频道"
    return f"{when_text(r.due_at, now, tz)} {where}提醒{who}：{r.text}"


def fire_text(r: Reminder, now: float, *, by_dm: bool, guild_name: str = "", fallback: bool = False) -> str:
    """到点时发出去的那条。by_dm：发的是私信（不用 @）；fallback：私信发不出去、改在频道里说的。"""
    body = r.say or f"提醒：{r.text}"
    owner_only = [uid for uid, _ in r.targets] == [r.owner_id]
    tails: list[str] = []
    if by_dm:
        head = "⏰ "
        if not r.private:
            server = f"在「{guild_name}」" if guild_name else ""
            tails.append(f"你{server}让我提醒的" if owner_only else f"{r.owner_name} {server}让我提醒你的")
    else:
        head = "⏰ " + " ".join(f"<@{uid}>" for uid, _ in r.targets) + " "
        if not owner_only:
            tails.append(f"{r.owner_name} 让我提醒的")
        if fallback:
            tails.append("私信发不出去，在这里说")
    late = now - r.due_at
    if late >= 120.0:
        tails.append(f"晚了 {span_text(late)}，刚才我不在线")
    return head + body + ("".join(f"（{t}）" for t in tails))


def button_id(ok: bool, rid: int) -> str:
    return f"{BUTTON}:{'ok' if ok else 'no'}:{rid}"


def parse_button(custom_id: Any) -> tuple[bool, int] | None:
    """按钮的 custom_id → (是不是确认, 提醒编号)；不是提醒的按钮是 None。"""
    if not isinstance(custom_id, str):
        return None
    parts = custom_id.split(":")
    if len(parts) != 3 or parts[0] != BUTTON or parts[1] not in ("ok", "no") or not parts[2].isascii() or not parts[2].isdigit():
        return None
    return parts[1] == "ok", int(parts[2])
