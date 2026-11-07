"""斜杠命令。

/ds …       服务器管理员（默认需要“管理服务器”权限）：区域、话量、档位、人设、表情包库、长期记忆、状态。
/ds-rp      所有人：查看、修改自己在这个服务器的人设卡（只有自己看得到；管理员看不到别人的卡）。私信里用是私信挂的那个服务器的卡。
/ds-dm …    服务器里和私信里都能用：私信挂在哪个服务器（用那里的人设、记忆和人设卡）、看今天的私信额度、清空私信记录。
/ds-music … 所有人：点歌（B站）、切歌、暂停、继续、停止、看队列、从队列里删歌、清空队列。切歌这些要在它放歌的语音频道里。
/dsowner …  只注册在 CONTROL_GUILD_ID 里的服务器（可以填几个），并且只认 OWNER_ID：预算、思考强度、联网搜索、删人设卡、服务器组等。
            和 /ds 一样默认只给有“管理服务器”权限的人看（bot 放在朋友的服务器、你是那里的管理也看得到）。

连成一组的服务器（/dsowner 服务器组）共用一份人设卡和长期记忆：/ds 长期记忆、/ds-rp 看的、改的都是组里那份。
config.toml 里 [bot] shared_cards = true 时，每个人在所有服务器共用一张人设卡（/ds-rp 改的就是那张），连组只管长期记忆。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Callable, Optional

import discord
from discord import app_commands
from discord.app_commands import Choice

from .config import ADMIN_KEYS, OWNER_KEYS, PRESETS, RESET_WORDS, SPECS, format_value
from .convert import safe_category_id
from .groups import (
    GroupError,
    LinkPlan,
    Linked,
    Unlinked,
    UnlinkPlan,
    durable,
    group_name,
    leftovers,
    link,
    plan_link,
    plan_unlink,
    recover,
    unlink,
)
from .memory import SHARED
from .prompts import DEFAULT_PERSONA, PERSONA_MAX_CHARS
from .textutil import one_line

if TYPE_CHECKING:
    from .discord_app import DSBot


def discord_safe(text: str) -> str:
    """外面来的文字（歌名、UP主的名字之类）发到 Discord 之前：Markdown 和 @ 都转义掉（不会变成加粗、剧透、@所有人）。"""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))

log = logging.getLogger(__name__)

SCOPE_CHOICES = [
    Choice(name="本频道", value="channel"),
    Choice(name="本类别", value="category"),
    Choice(name="全服务器", value="guild"),
]
AREA_ACTIONS = [
    Choice(name="允许", value="on"),
    Choice(name="禁止", value="off"),
    Choice(name="跟随上级", value="inherit"),
]
SHARE_ACTIONS = [
    Choice(name="开", value="on"),
    Choice(name="关", value="off"),
    Choice(name="跟随上级", value="inherit"),
]
MEME_ACTIONS = [
    Choice(name="上传", value="add"),
    Choice(name="删除", value="remove"),
    Choice(name="列表", value="list"),
]
CARD_ACTIONS = [
    Choice(name="编辑", value="edit"),
    Choice(name="改回上一版", value="undo"),
    Choice(name="清空", value="clear"),
]
MEMORY_ACTIONS = [
    Choice(name="查看", value="list"),
    Choice(name="添加", value="add"),
    Choice(name="删除", value="delete"),
    Choice(name="清空", value="clear"),
]
GROUP_ACTIONS = [
    Choice(name="查看", value="list"),
    Choice(name="连起来", value="link"),
    Choice(name="退出", value="leave"),
]
LINK_MODES = {
    "all": "读链接：开（推文走 FxTwitter，网页由 DeepSeek 读，Tavily 备用）",
    "x": "读链接：只读推文（别的网站只看 Discord 的预览）",
    "off": "读链接：关",
}
NO_CATEGORY = "这个频道不在任何类别里，范围请选“本频道”或“全服务器”。"
# 区域、主动开关、人设、记忆共享有专门的命令；其余管理员参数走 /ds 设置
ADMIN_SET_KEYS = [k for k in ADMIN_KEYS if k not in ("enabled", "proactive", "persona", "share_memory")]
GUILD_ONLY_KEYS = ("dm", "dm_memory", "cross_server")  # 只看全服务器这一级的参数
OWNER_SET_KEYS = OWNER_KEYS + [k for k in ADMIN_KEYS if k != "persona"]


def owner_key(raw: str) -> str | None:
    """/dsowner 设置 的参数：选单里选的是参数名，手打的也认中文名。"""
    raw = raw.strip()
    if raw in OWNER_SET_KEYS:
        return raw
    return next((k for k in OWNER_SET_KEYS if SPECS[k].label == raw), None)


def owner_key_choices(current: str) -> list[Choice[str]]:
    """输入时弹出的参数列表（Discord 最多显示 25 个）：名字或说明里有输入的字的。"""
    words = current.strip().casefold()
    keys = [k for k in OWNER_SET_KEYS if not words or words in SPECS[k].label.casefold() or words in k or words in SPECS[k].help]
    return [Choice(name=SPECS[k].label, value=k) for k in keys[:25]]


async def _reply(interaction: discord.Interaction, text: str) -> None:
    text = text if len(text) <= 1900 else text[:1900] + "…"
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def split_message(text: str, limit: int = 1900) -> list[str]:
    """长文字按行分成几条（每条不超过 limit 字）。"""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts or [""]


REPLY_PARTS = 10  # _reply_long 最多发几条


async def _reply_long(interaction: discord.Interaction, text: str) -> None:
    """可能超过一条消息长度的回复：分几条发（都只有自己看得到），最多 REPLY_PARTS 条。"""
    parts = split_message(text)
    if len(parts) > REPLY_PARTS:
        parts = parts[: REPLY_PARTS - 1] + [f"……太长了，后面还有 {len(parts) - REPLY_PARTS + 1} 段没发出来。"]
    for part in parts:
        await _reply(interaction, part)


def _parent_id(interaction: discord.Interaction) -> int | None:
    channel = interaction.channel
    return channel.parent_id if isinstance(channel, discord.Thread) else None


def _category(interaction: discord.Interaction) -> tuple[int | None, str | None]:
    """当前频道所在的类别（帖子取它所在频道的类别）。"""
    channel = interaction.channel
    try:
        category = getattr(channel, "category", None)
    except Exception:  # 帖子的所在频道没缓存时 discord.py 会抛异常
        category = None
    if category is not None:
        return category.id, category.name
    return safe_category_id(channel), None


def _ids(interaction: discord.Interaction) -> tuple[int, int, int | None, int | None]:
    return interaction.guild_id, interaction.channel_id, _parent_id(interaction), _category(interaction)[0]


def _target(
    interaction: discord.Interaction, scope: Optional[Choice[str]], default: str = "channel"
) -> tuple[str, int, str] | None:
    """范围选项 → (存储范围, id, 给人看的名字)。选了“本类别”但频道不在类别里时返回 None。"""
    value = scope.value if scope is not None else default
    if value == "guild":
        return "guild", interaction.guild_id, "全服务器"
    if value == "category":
        category_id, name = _category(interaction)
        if category_id is None:
            return None
        return "category", category_id, f"类别「{name or category_id}」"
    return "channel", interaction.channel_id, f"<#{interaction.channel_id}>"


def _preset_text(values: dict[str, int]) -> str:
    gap = values["min_speak_gap"]
    gap_text = f"{gap // 60} 分钟" if gap >= 60 else f"{gap} 秒"
    return f"每 {values['refill_turns']} 轮恢复 1 次，最多攒 {values['quota_capacity']} 次，间隔 {gap_text}"


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "还没说过"
    if seconds < 60:
        return f"{seconds:.0f} 秒前"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟前"
    return f"{seconds / 3600:.1f} 小时前"


class PersonaModal(discord.ui.Modal):
    def __init__(self, bot: "DSBot", scope: str, scope_id: int, where: str, current: str, inherited: str):
        super().__init__(title="修改人设", timeout=900)
        self.bot = bot
        self.scope = scope
        self.scope_id = scope_id
        self.where = where
        self.inherited = inherited  # 这个范围不单独设置时会用到的人设
        self.text = discord.ui.TextInput(
            style=discord.TextStyle.long,
            default=current[:PERSONA_MAX_CHARS] or None,
            required=False,
            max_length=PERSONA_MAX_CHARS,
            placeholder="留空 = 恢复默认人设",
        )
        self.add_item(
            discord.ui.Label(
                text="人设",
                description="写它是谁、什么性格、和群友是什么关系。清空就是默认人设。",
                component=self.text,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.text.value.strip()
        old = self.bot.settings.overrides(self.scope, self.scope_id).get("persona")
        if not value or value == self.inherited.strip():
            # 没改（和上一级一样）就不单独存，以后上一级改了这里也会跟着变
            self.bot.settings.unset(self.scope, self.scope_id, "persona")
            new = None
            done = "已恢复默认人设" if self.scope == "guild" else "已改为沿用上一级的人设"
        else:
            self.bot.settings.set(self.scope, self.scope_id, "persona", value)
            new = value
            done = "人设已更新"
        if new != old:
            # 记进人设历史：dsowner 在聊天里说“改回去”时能撤销
            self.bot.store.add_persona_change(
                ts=time.time(),
                scope=self.scope,
                scope_id=self.scope_id,
                old=old,
                new=new,
                by_user=interaction.user.id,
                via="command",
            )
        await _reply(
            interaction,
            f"{self.where}：{done}，下一轮对话生效。\n改动比较大的话，可以用 `/ds 清空记忆` 让它不受之前说话风格的影响。",
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("保存人设失败", exc_info=error)
        await _reply(interaction, f"保存失败：{error}")


def server_label(bot: "DSBot", guild_id: int) -> str:
    """「服务器名」；bot 已经不在那个服务器了就写 ID。"""
    guild = bot.get_guild(guild_id)
    return f"「{discord_safe(guild.name)}」" if guild is not None else f"「{guild_id}」（bot 已经不在那里了）"


def group_others(bot: "DSBot", guild_id: int) -> list[str]:
    """和这个服务器连成一组的别的服务器（「名字」，按加入的先后）；不在组里是空的。"""
    group = bot.store.group_of(guild_id)
    if group is None:
        return []
    return [server_label(bot, g) for g in bot.store.group_members(group) if g != guild_id]


def shared_where(bot: "DSBot", guild_id: int) -> str:
    """人设卡、长期记忆是谁的：“这个服务器”，连成一组的是“这个服务器组（和「B」共用）”。"""
    others = group_others(bot, guild_id)
    return f"这个服务器组（和{'、'.join(others)}共用）" if others else "这个服务器"


def cards_shared(bot: "DSBot") -> bool:
    """所有服务器共用一张人设卡（config.toml 的 [bot] shared_cards，启动时换好了的）。"""
    cards = getattr(bot, "cards", None)
    return cards is not None and bool(getattr(cards, "shared", False))


def card_scope(bot: "DSBot", guild_id: int) -> int:
    """这个服务器的人设卡记在哪：所有服务器共用一张时是 SHARED，不然和长期记忆一样（连成一组的记在组里）。"""
    return SHARED if cards_shared(bot) else bot.store.memory_scope(guild_id)


def card_where(bot: "DSBot", guild_id: int) -> str:
    """人设卡是谁的：“所有服务器（每人一张）”，不然同 shared_where。"""
    return "所有服务器（每人一张）" if cards_shared(bot) else shared_where(bot, guild_id)


def group_cards(bot: "DSBot") -> Any:
    """连组、退组时交给 groups.py 的人设卡：所有服务器共用一张时卡不用搬，只搬删卡的记号（CardStore.for_groups）。"""
    cards = getattr(bot, "cards", None)
    return cards.for_groups() if cards is not None and hasattr(cards, "for_groups") else cards


def card_written(bot: "DSBot", guild_id: int, user_id: int, name: str, in_dm: bool) -> str:
    """用 /ds-rp 改了卡：卡里有别的服务器的人时（连成一组、所有服务器共用一张）记下他的卡在这个服务器用过（退组时
    只带走在那里出现过的人的卡），返回卡上该记的名字（组里只跟着第一个服务器的昵称改，共用一张时、私信里不改：免得卡上的名字
    来回变）。"""
    scope = card_scope(bot, guild_id)
    cards = bot.cards
    if scope == guild_id or cards is None:
        return name
    cards.note_seen(scope, user_id, guild_id, "" if in_dm else name)
    first = bot.store.group_members(scope)[:1] == [guild_id]
    current = cards.get(scope, user_id)
    return name if current is None or (first and not in_dm) else current.name


class CardModal(discord.ui.Modal):
    """/ds-rp：编辑自己的人设卡。卡的主人就是填表的人，改不了别人的。
    卡记在 guild_id 的人设卡里（连成一组的话是组里那份：交表的时候再看，填表期间连组、退组了也写对地方）。"""

    def __init__(self, bot: "DSBot", guild_id: int, current: str):
        super().__init__(title="我的人设卡", timeout=900)
        self.bot = bot
        self.guild_id = guild_id
        limit = max(1, min(4000, bot.cfg.engine.card_max_chars))
        self.text = discord.ui.TextInput(
            style=discord.TextStyle.long,
            default=current[:limit] or None,
            required=False,
            max_length=limit,
            placeholder="怎么称呼你、你的设定（比如自称猫娘）、喜好和雷点……留空 = 删掉这张卡（还能改回上一版）",
        )
        self.add_item(
            discord.ui.Label(
                text="人设卡",
                description="它会记住这些，和你聊天时用上。原文只有你自己能看、能改；别人问起时它可能用自己的话大概说说。",
                component=self.text,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert self.bot.cards is not None
        value = self.text.value
        name = getattr(interaction.user, "display_name", None) or interaction.user.name
        scope = card_scope(self.bot, self.guild_id)
        if value.strip():
            name = card_written(self.bot, self.guild_id, interaction.user.id, name, interaction.guild_id is None)
        changed = self.bot.cards.put(scope, interaction.user.id, name, value, via="command")
        where = card_where(self.bot, self.guild_id)
        if not value.strip():
            text = (
                f"已删掉你在{where}的人设卡（想找回来用 `/ds-rp 操作:改回上一版`；连旧版本一起删用 `/ds-rp 操作:清空`）。"
                if changed
                else f"你在{where}还没有人设卡。"
            )
        else:
            text = "人设卡已保存，下次你说话时它就会用上。" if changed else "人设卡没有改动。"
        ids = _ids(interaction) if interaction.guild_id is not None else (self.guild_id, None, None, None)
        if value.strip() and not self.bot.settings.view(*ids).cards:
            if cards_shared(self.bot):
                text += "\n（这里关掉了人设卡：在这里不会用，开着人设卡的服务器照常用。）"
            else:
                text += "\n（这里关掉了人设卡，它暂时不会用。）"
        await _reply(interaction, text)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("保存人设卡失败", exc_info=error)
        await _reply(interaction, f"保存失败：{error}")


class ConfirmClear(discord.ui.View):
    """清空长期记忆前再确认一次。"""

    def __init__(self, bot: "DSBot", guild_id: int, user_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.scope = bot.store.memory_scope(guild_id)  # 问的时候说的是这一份
        self.members = self._members()  # 组里有哪些服务器（期间有服务器连进来，它的记忆也并进来了，不能一起清）

    def _members(self) -> tuple[int, ...]:
        group = self.bot.store.group_of(self.guild_id)
        return tuple(self.bot.store.group_members(group)) if group is not None else ()

    @discord.ui.button(label="确认清空", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await _reply(interaction, "只有发起的人能确认。")
            return
        assert self.bot.memory is not None
        self.stop()
        if self.bot.store.memory_scope(self.guild_id) != self.scope or self._members() != self.members:
            # 问完之后连进了服务器组（或者退了出来、组里进出了别的服务器）：现在这份不是刚才问的那份，不清
            await interaction.response.edit_message(
                content="服务器组刚变过，现在的长期记忆不是刚才那份：没清空。要清空的话再执行一次 `/ds 长期记忆 操作:清空`。", view=None
            )
            return
        n = self.bot.memory.clear(self.scope)
        await interaction.response.edit_message(content=f"已清空{shared_where(self.bot, self.guild_id)}的长期记忆（{n} 条）。", view=None)


def _memory_labels(bot: "DSBot", guild_id: int, entries: list[Any], *, raw: bool = False) -> dict[int, str]:
    """连成一组时：组里别的服务器记的长期记忆 {编号: 「那个服务器」}（列表里标出来）。
    raw：不转义 Discord 的格式符号（自动补全的选项不渲染格式，转义了反而多出反斜杠）。"""
    group = bot.store.group_of(guild_id)
    if group is None:
        return {}
    others = set(bot.store.group_members(group)) - {guild_id}

    def name(g: int) -> str:
        guild = bot.get_guild(g)
        # 自动补全的选项最多 100 字：服务器名太长就截短，后面的内容才看得到
        return f"「{one_line(guild.name, 20)}」" if raw and guild is not None else server_label(bot, g)

    return {m.no: name(m.origin) for m in entries if getattr(m, "origin", None) in others}


def memory_list_text(bot: "DSBot", guild_id: int) -> str:
    assert bot.memory is not None
    entries = bot.memory.entries(bot.store.memory_scope(guild_id))
    limit = bot.cfg.engine.memory_max_entries
    head = [f"**长期记忆**（{len(entries)} / {limit} 条）"]
    others = group_others(bot, guild_id)
    if others:
        head.append(f"这个服务器和{'、'.join(others)}连成了一组，长期记忆是组里共用的：在这里改，那边也跟着变。")
    if len(entries) > limit:
        if others:
            tidy = "整理记忆时它不会为了腾地方删别的服务器记的，最好在这里删几条"
        else:
            tidy = "整理记忆时它会合并、删掉旧的，也可以在这里删几条"
        head.append(f"⚠️ 超过上限了：删到 {limit - 1} 条以内才能再记新的。{tidy}。")
    tail = []
    if not bot.settings.get("long_memory", guild_id=guild_id):
        tail.append("⚠️ 这个服务器关掉了长期记忆：`/ds 设置 参数:长期记忆 值:开 范围:全服务器` 打开")
    if not entries:
        return "\n".join(head + ["还没有。聊天时有人叫它记住群里的事，或者它整理聊天时觉得值得记，就会记在这里。"] + tail)
    labels = _memory_labels(bot, guild_id, entries)

    def body(cap: int | None) -> list[str]:
        out = []
        for m in entries:
            text = m.text if cap is None or len(m.text) <= cap else m.text[: cap - 1] + "…"
            out.append(f"({m.no}) {text}" + (f"（{labels[m.no]}记的）" if m.no in labels else ""))
        return out

    def fits(lines: list[str]) -> bool:
        return len(split_message("\n".join(lines))) <= REPLY_PARTS

    lines = body(None)
    if fits(head + lines + tail):
        return "\n".join(head + lines + tail)
    # 发不完（连成一组时并进来很多条）：每条截短，编号都列出来，管理员照编号删
    for cap in (120, 80, 50, 30, 20):
        note = [f"条目太多，每条只显示前 {cap} 个字（删除时在「编号」一栏输入几个字能筛选）。"]
        lines = body(cap)
        if fits(head + note + lines + tail):
            return "\n".join(head + note + lines + tail)
    shown = len(lines)
    while shown and not fits(head + note + lines[:shown] + [f"……还有 {len(lines) - shown} 条没列出。"] + tail):
        shown -= 1
    return "\n".join(head + note + lines[:shown] + [f"……还有 {len(lines) - shown} 条没列出。"] + tail)


def memory_number_choices(bot: "DSBot", guild_id: int | None, current: str) -> list[Choice[int]]:
    """/ds 长期记忆 的编号：列出“(编号) 内容”（输入的字能筛选），删之前看得到是哪一条（连组以后编号会变）。"""
    memory = getattr(bot, "memory", None)
    if memory is None or guild_id is None:
        return []
    entries = memory.entries(bot.store.memory_scope(guild_id))
    labels = _memory_labels(bot, guild_id, entries, raw=True)
    words = current.strip().casefold()
    out: list[Choice[int]] = []
    for m in entries:
        label = f"({m.no})" + (f"〔{labels[m.no]}记的〕" if m.no in labels else " ") + m.text  # 来源写在前面：长的截掉了也看得到
        if words and words not in label.casefold():
            continue
        out.append(Choice(name=label if len(label) <= 100 else label[:99] + "…", value=m.no))
    return out[:25]


def share_text(bot: "DSBot", interaction: discord.Interaction) -> str:
    """这个频道的记忆共享情况：能不能看别的频道、自己的内容会不会分享出去、现在和哪些频道互通。"""
    guild_id, channel_id, parent_id, category_id = _ids(interaction)
    view = bot.settings.view(guild_id, channel_id, parent_id, category_id)
    if not view.share_memory:
        return f"记忆共享：关（来自{view.source('share_memory')}）"
    place = bot.transport.locate(channel_id)
    shares_out = place is not None and bot.engine.shares_out(channel_id, place)
    if shares_out:
        out_text = "这里的内容也会分享给别的共享频道"
    elif place is not None and not place.public:
        out_text = "🔒 这是私密频道，这里的内容不会分享出去（要分享就在这里单独执行 `/ds 记忆共享 操作:开`）"
    else:
        out_text = "这里的内容不会分享出去"
    sources = bot.engine.shared_sources(guild_id, channel_id)
    names = "、".join(
        f"<#{cid}>" + (server_label(bot, p.guild_id) if p.guild_id != guild_id else "") for cid, p in sources[:15]
    ) or "暂时没有（别的频道开了共享、并且跟它聊过之后才会出现）"
    return f"记忆共享：开（来自{view.source('share_memory')}）· {out_text}\n能翻记录的频道：{names}"


def status_text(bot: "DSBot", interaction: discord.Interaction) -> str:
    guild_id, channel_id, parent_id, category_id = _ids(interaction)
    view = bot.settings.view(guild_id, channel_id, parent_id, category_id)
    st = bot.engine.status(guild_id, channel_id, parent_id, category_id)
    channel_stats = bot.store.day_stats(st["day"], channel_id=channel_id)
    guild_stats = bot.store.day_stats(st["day"], guild_id=guild_id)
    outcomes = channel_stats["outcomes"]
    preset = next(
        (name for name, values in PRESETS.items() if all(getattr(view, k) == v for k, v in values.items())),
        "自定义",
    )
    lines = [f"**<#{channel_id}> 的状态**"]
    if not view.enabled:
        lines.append(f"🚫 这里不在允许的区域（来自{view.source('enabled')}），它不会看也不会回这里的消息")
    lines += [
        f"主动发言：{'开' if view.proactive else '关'}（来自{view.source('proactive')}）· 档位：{preset}",
        f"主动额度：{st['tokens']:.1f} / {view.quota_capacity}（每 {view.refill_turns} 轮恢复 1 次，"
        f"已攒 {st['turns_since_refill']} 轮）",
        f"发言间隔 ≥ {view.min_speak_gap}s · 冷场 {view.quiet_seconds}s · 最长等待 {view.max_wait_seconds}s"
        f" · 每人每分钟点名 ≤ {view.mention_rate_limit} 次",
        f"别名：{view.aliases or '（无）'} · 人设：{format_value(SPECS['persona'], view.persona)}",
        f"待处理消息 {st['pending']} 条 · 上次发言：{_ago(st['last_speak_ago'])}",
    ]
    lines.append(share_text(bot, interaction))
    if st["can_search"]:
        lines.append(f"联网搜索：开 · 今天本服务器已搜 {st['searches']} / {view.daily_search_limit} 次")
    else:
        lines.append("联网搜索：关")
    link_line = LINK_MODES.get(st["links_mode"], LINK_MODES["off"])
    if st["links_mode"] != "off":
        link_line += f" · 今天本服务器打开了 {st['links_opened']} 个"
    lines.append(link_line)
    if st["memes_on"]:
        own, shared = st["meme_counts"]
        if st["can_find_images"]:
            found = f"上网找图：今天本服务器 {st['images_found']} / {view.daily_image_limit} 次"
        else:
            found = "上网找图：关（没填 TAVILY_API_KEY，或者每日找图上限是 0）"
        lines.append(f"发图：开 · 表情包库 {own + shared} 张（本服务器 {own}、通用 {shared}）· {found}")
    else:
        lines.append(f"发图：关（来自{view.source('images')}）")
    grouped = bool(st.get("group"))
    shared = bool(st.get("cards_shared"))
    if grouped:
        others = "、".join(server_label(bot, g) for g in st["group"])
        what = "长期记忆共用" if shared else "人设卡、长期记忆共用"
        lines.append(f"服务器组（Beta）：和{others}连成了一组（{what}；开了记忆共享的频道能互相翻记录）")
    if st["cards_on"] and shared:
        lines.append(f"人设卡：开 · 所有服务器共用，每人一张，一共 {st['card_count']} 张（群友用 /ds-rp 管自己的卡）")
    elif st["cards_on"]:
        lines.append(f"人设卡：开 · 这个服务器{'组' if grouped else ''}有 {st['card_count']} 张（群友用 /ds-rp 管自己的卡）")
    else:
        lines.append(f"人设卡：关（来自{view.source('cards')}）")
    if st["memory_on"]:
        lines.append(f"长期记忆：开 · {st['memory_count']} / {st['memory_max']} 条{'（组里共用）' if grouped else ''}（/ds 长期记忆 查看）")
    else:
        lines.append(f"长期记忆：关（来自{view.source('long_memory')}）")
    if (st["cards_on"] or st["memory_on"]) and not st["review_here"]:
        lines.append("🔒 私密频道：这里聊的不会被自动整理进人设卡和长期记忆（有人明确叫它记住的除外）")
    if st["memes_on"] and st["media_blocked_for"] > 0:
        minutes = max(1, round(st["media_blocked_for"] / 60))
        lines.append(f"⚠️ 这里最近连着发不出图（多半是 bot 没有“附加文件”权限），约 {minutes} 分钟内先不发图，之后自动再试")
    if bot.settings.get("dm", guild_id=interaction.guild_id):
        lines.append(
            f"私信：开（成员可以私信它，用这里的人设和记忆；私信里聊的可能被记进来、在他叫它的频道里被提起；"
            f"也算进本服务器的预算，每人每天 ≤ ${view.dm_daily_budget_usd:g}）"
        )
    else:
        lines.append("私信：关")
    if bot.settings.get("dm_memory", guild_id=interaction.guild_id):
        lines.append("私信记忆：开（私信挂在这里的人，私信里聊的记进他自己的私信记忆；他在这里叫它时带上不敏感的那些）")
    else:
        lines.append("私信记忆：关")
    if bot.settings.get("cross_server", guild_id=interaction.guild_id):
        lines.append("跨服务器：开（和别的也开了的服务器之间，同一个人最近跟它聊的、私信记忆里不敏感的会跟着他走）")
    else:
        lines.append("跨服务器：关")
    if st["music_on"]:
        playing = f" · 现在{st['playing']}" if st["playing"] else ""
        lines.append(f"点歌：开（/ds-music，只能放 B站的，按歌名找算一次联网搜索）{discord_safe(playing)}")
    else:
        lines.append(f"点歌：没启用（{bot.music_problem}）")
    if st["last_decision"]:
        lines.append(f"上次：{st['last_decision']}")
    lines.append(
        f"今天本频道：调用 {channel_stats['calls']} 次（说话 {outcomes.get('reply', 0)}、表情 {outcomes.get('react', 0)}、"
        f"沉默 {outcomes.get('pass', 0)}、出错 {outcomes.get('error', 0)}"
        + (f"、照念人设卡被拦下 {outcomes['recited']}" if outcomes.get("recited") else "")
        + "）"
    )
    hit = guild_stats["hit_rate"]
    lines.append(
        f"今天本服务器花费：${st['guild_spent']:.4f} / ${view.daily_budget_usd:g}"
        + (f" · 缓存命中率 {hit:.0%}" if hit is not None else "")
    )
    if st["guild_spent"] >= view.daily_budget_usd:
        lines.append("⚠️ 今天已超预算：只回应点名，不主动说话")
    if st["paused"]:
        lines.append("⚠️ DeepSeek 调用暂停中（余额不足或 key 无效，详见日志）")
    if st["send_blocked"]:
        lines.append("⚠️ 最近几轮在这里发不出消息（检查 bot 的发言权限），主动发言暂停 30 分钟")
    return "\n".join(lines)


def meme_list_text(bot: "DSBot", guild_id: int) -> str:
    entries = bot.memes.entries(guild_id)
    own = sorted(name for name, meme in entries.items() if not meme.shared)
    shared = sorted(name for name, meme in entries.items() if meme.shared)
    lines = [f"**表情包库**（本服务器 {len(own)} 张 · 通用 {len(shared)} 张）"]
    lines.append("本服务器：" + ("、".join(own) if own else "还没有（用 `/ds 表情包 操作:上传` 加）"))
    lines.append("通用：" + ("、".join(shared) if shared else f"没有（bot 主人可以在 NAS 上的 `{bot.cfg.bot.meme_dir}/` 里放图）"))
    if not bot.settings.get("images", guild_id=guild_id):
        lines.append("⚠️ 这个服务器关掉了发图：`/ds 设置 参数:发图 值:开 范围:全服务器` 打开")
    return "\n".join(lines)


def area_list_text(bot: "DSBot", guild: discord.Guild) -> str:
    def mark(value: bool) -> str:
        return "✅ 允许" if value else "🚫 禁止"

    guild_enabled = bot.settings.get("enabled", guild_id=guild.id)
    source = "单独设置" if "enabled" in bot.settings.overrides("guild", guild.id) else "默认"
    lines = ["**它会在哪里回应**", f"全服务器：{mark(guild_enabled)}（{source}）"]
    rows = []
    for category_id, value in bot.settings.scoped("category", "enabled").items():
        category = guild.get_channel(category_id)
        if category is not None:
            rows.append((category.position, f"类别「{category.name}」：{mark(value)}"))
    for channel_id, value in bot.settings.scoped("channel", "enabled").items():
        if guild.get_channel_or_thread(channel_id) is not None:
            rows.append((10_000, f"<#{channel_id}>：{mark(value)}"))
    lines += [text for _, text in sorted(rows)] or ["（没有单独设置过的类别或频道）"]

    proactive = []
    for scope in ("category", "channel"):
        for scope_id, value in bot.settings.scoped(scope, "proactive").items():
            obj = guild.get_channel(scope_id) if scope == "category" else guild.get_channel_or_thread(scope_id)
            if value and obj is not None:
                proactive.append(f"类别「{obj.name}」" if scope == "category" else f"<#{scope_id}>")
    if bot.settings.get("proactive", guild_id=guild.id):
        lines.append("主动发言：全服务器默认开")
    elif proactive:
        lines.append("主动发言开着的地方：" + "、".join(proactive))
    lines.append("（帖子跟着所在频道走；频道的设置优先于类别，类别优先于全服务器）")
    return "\n".join(lines)


async def _say_public(interaction: discord.Interaction, text: str, footer: str = "") -> None:
    """大家都看得到的回复（点歌的结果）。歌名是外面来的：转义掉 Markdown 和 @。footer：自己写的提示（不转义）。"""
    text = discord_safe(text)
    text = (text if len(text) <= 1900 else text[:1900] + "…") + footer
    none = discord.AllowedMentions.none()
    if interaction.response.is_done():
        await interaction.followup.send(text, allowed_mentions=none)
    else:
        await interaction.response.send_message(text, allowed_mentions=none)


def dm_channel_of(bot: "DSBot", interaction: discord.Interaction) -> int | None:
    """这个人和 bot 的私信频道：在私信里用命令就是这里；在服务器里用，是记下的那个（还没私信过是 None）。"""
    if interaction.guild_id is None:
        return interaction.channel_id
    home = bot.store.dm_home(interaction.user.id)
    return home[1] if home is not None else None


DM_WARNING = (
    "⚠️ 私信里聊的事可能会记进那边的长期记忆和你的人设卡；你在那边的频道里叫它时，它也可能提起刚在私信里聊的，"
    "频道里的人都看得到。不想让别人知道的，跟它说一声“保密”。\n"
    "📒 你们之间的事会记进你的私信记忆：标着敏感的只在私信里用；不敏感的，你在服务器里叫它时也会用上，可能被提起。"
    "敏感不敏感是它自己判断的，可能判错：用 `/ds-dm 记忆` 查看、删掉或者改成敏感。"
)
# 所有服务器共用一张人设卡时：私信里写进人设卡的，在别的服务器也会用上
DM_CARD_REACH = "你的人设卡是所有服务器共用的：私信里写进人设卡的，在别的服务器也会用上。\n"


def dm_group_note(bot: "DSBot", guild_id: int, *, me: str = "它") -> str:
    """私信挂的服务器连成了一组时说一声（没连组是空的）：私信里记下的、聊的，组里的服务器都可能用上、提起。
    me：话里怎么称呼 bot（命令的回复里是“它”，bot 自己私信时说“我”）。"""
    others = group_others(bot, guild_id)
    if not others:
        return ""
    guild = bot.get_guild(guild_id)
    name = discord_safe(guild.name) if guild is not None else str(guild_id)
    return (
        f"「{name}」和{'、'.join(others)}连成了一组：长期记忆{'' if cards_shared(bot) else '、人设卡'}是组里共用的，"
        f"私信里记下的在那边也会用上，你在那边叫{me}时{me}也可能提起私信里聊的。"
    )


def dm_warning(bot: "DSBot") -> str:
    """私信的隐私提醒（/ds-dm 服务器、/ds-dm 状态）：所有服务器共用一张人设卡时多一句卡会用到哪。"""
    return DM_WARNING.replace("。\n📒", "。\n" + DM_CARD_REACH + "📒", 1) if cards_shared(bot) else DM_WARNING


def dm_memory_text(bot: "DSBot", user_id: int) -> str:
    """某个人的私信记忆（只给他自己看）。"""
    assert bot.memory is not None
    entries = bot.memory.dm_entries(user_id)
    cap = bot.cfg.engine.dm_memory_max_entries
    lines = [f"**你的私信记忆**（{len(entries)} / {cap} 条）"]
    lines += [f"({e.no}) {e.text}" + ("〔敏感〕" if e.sensitive else "") for e in entries] or [
        "还没有。私信里叫它记住你们之间的事，或者私信安静下来后它整理时觉得值得记，就会记在这里。"
    ]
    if entries:
        lines.append("标着〔敏感〕的只在私信里用；别的你在服务器里叫它时也会用上。")
        lines.append(
            "判错了就改：`/ds-dm 记忆 操作:设为不敏感 编号:3`（编号一栏输入几个字就能从你的记忆里选），"
            "或者私信里直接跟它说“第 3 条不算敏感”。"
        )
    return "\n".join(lines)


def withdraw_dm_memory(bot: "DSBot", user_id: int | None = None, *, guild_id: int | None = None) -> None:
    """频道记录里附过的私信记忆撤回（下次照新的样子、新的设置重新附）。"""
    withdraw = getattr(getattr(bot, "engine", None), "withdraw_dm_memory", None)
    if withdraw is not None:
        withdraw(user_id, guild_id=guild_id)


def settings_changed(bot: "DSBot", key: str, scope: str, scope_id: int) -> None:
    """改了私信记忆、跨服务器这两个参数：这个服务器（全局的话是所有服务器）频道里附过的私信记忆先撤回。"""
    if key in ("dm_memory", "cross_server"):
        withdraw_dm_memory(bot, guild_id=scope_id if scope == "guild" else None)


def dm_memory_choices(bot: "DSBot", user_id: int, current: str) -> list[Choice[int]]:
    """/ds-dm 记忆 的编号：从自己的私信记忆里选（输入的字能筛选；只有自己看得到）。"""
    memory = getattr(bot, "memory", None)
    if memory is None:
        return []
    words = current.strip().casefold()
    out: list[Choice[int]] = []
    for e in memory.dm_entries(user_id):
        label = f"({e.no}) {e.text}" + ("〔敏感〕" if e.sensitive else "")
        if words and words not in label.casefold():
            continue
        out.append(Choice(name=label if len(label) <= 100 else label[:99] + "…", value=e.no))
    return out[:25]


# ---------------------------------------------------------------------------
# /dsowner 服务器组
# ---------------------------------------------------------------------------


def server_choices(bot: "DSBot", current: str) -> list[Choice[str]]:
    """/dsowner 服务器组 的“服务器”“连到”两栏：bot 在的服务器（名字、ID 都能筛选），在组里的标上是哪个组。"""
    words = (current or "").strip().casefold()
    out: list[Choice[str]] = []
    for guild in sorted(bot.guilds, key=lambda g: (g.name.casefold(), g.id)):
        label = f"{guild.name}（{guild.id}）"
        group = bot.store.group_of(guild.id)
        if group is not None:
            label += f" · {group_name(group)}"
        if words and words not in label.casefold():
            continue
        out.append(Choice(name=label if len(label) <= 100 else label[:99] + "…", value=str(guild.id)))
        if len(out) >= 25:
            break
    return out


def owner_server_choices(bot: "DSBot", user_id: int, current: str) -> list[Choice[str]]:
    """服务器名单（bot 在哪些服务器、哪些连成了组）只给 bot 主人看：/dsowner 的命令别人看得到，但用不了。"""
    return server_choices(bot, current) if user_id == bot.secrets.owner_id else []


def _guild_arg(raw: str | None) -> int | None:
    """从弹出的列表里选的是服务器 ID；没选（只打了几个字）是 None。"""
    text = (raw or "").strip()
    return int(text) if text.isdecimal() and len(text) < 25 else None


def groups_text(bot: "DSBot") -> str:
    groups = bot.store.groups()
    lost = leftovers(bot.store, bot.cards, bot.memory)
    shared = cards_shared(bot)
    what = "长期记忆" if shared else "人设卡和长期记忆"
    lines: list[str] = []
    if not groups:
        lines.append(
            f"还没有服务器组（Beta 功能）。用 `/dsowner 服务器组 操作:连起来 服务器:… 连到:…` 把两个服务器连起来：之后它们共用{what}，"
            "开了记忆共享的频道能互相翻记录。"
        )
    else:
        lines.append("**服务器组**（Beta）")
        for group, members in groups.items():
            cards = bot.cards.count(group) if bot.cards is not None and not shared else 0
            memories = bot.memory.count(group) if bot.memory is not None else 0
            names = "、".join(server_label(bot, g) for g in members)
            counts = f"共用长期记忆 {memories} 条" if shared else f"共用人设卡 {cards} 张、长期记忆 {memories} 条"
            lines.append(f"{group_name(group)}：{names} · {counts}")
        lines.append(f"退出用 `/dsowner 服务器组 操作:退出 服务器:…`（那个服务器会带走一份组里的{what}）。")
    if shared:
        lines.append("人设卡是所有服务器共用的（config.toml 里 [bot] shared_cards = true）：连组、退组都不动人设卡。")
    for group, (cards, memories) in lost.items():
        lines.append(
            f"⚠️ {group_name(group)} 已经不在了，cards.sqlite3 / memory.sqlite3 里却还有它的 {cards} 个人的人设卡、{memories} 条长期记忆，"
            "现在没有服务器在用（没删）。多半是只还原了其中一两个文件的备份：data/ 里三个 sqlite3 文件要用同一时刻的备份一起还原。"
        )
    return "\n".join(lines)


def link_preview_text(bot: "DSBot", plan: LinkPlan) -> str:
    """连起来之前给 bot 主人看：会怎样。"""
    joining = server_label(bot, plan.joining)
    limit = bot.cfg.engine.memory_max_entries
    shared = cards_shared(bot)
    what = "长期记忆" if shared else "人设卡和长期记忆"
    if plan.group_id is None:
        base = server_label(bot, plan.base)
        lines = [f"把{joining}和{base}连成一组？", f"- 组里先用{base}现在的{what}。"]
        winner = base
    else:
        members = "、".join(server_label(bot, g) for g in plan.members)
        lines = [f"把{joining}加进{group_name(plan.group_id)}（{members}）？"]
        winner = "组里"
    lines.append(
        f"- 连起来以后当成一个服务器来记：共用一份{what}；开了记忆共享的频道也能翻组里别的服务器的记录"
        "（私密频道照样不外传）。"
    )
    if shared:
        lines.append("- 人设卡本来就是所有服务器共用的（config.toml 里 [bot] shared_cards = true），连组不动人设卡。")
    memory_line = f"- {joining}的长期记忆并进组里：新加 {plan.memories_added} 条（一模一样的不重复记），一共 {plan.memory_total} 条"
    if plan.memory_total > limit:
        memory_line += (
            f"，超过上限 {limit} 条：删到 {limit - 1} 条以内之前不会记新的。整理记忆时每个服务器只合并、删改自己服务器记的，"
            "别的服务器记的不动，所以最好用 `/ds 长期记忆` 删几条（连之前、连之后都行）。"
        )
    lines.append(memory_line + ("" if plan.memory_total > limit else "。"))
    if plan.renumbered:
        lines.append(f"- {joining}原来的长期记忆会接在组里原来的后面、重新编号：要删哪条，先重新 `/ds 长期记忆 操作:查看`。")
    both = plan.cards_kept - plan.cards_deleted
    if both:
        lines.append(
            f"- 有 {both} 个人两边都有人设卡：用{winner}那张，{joining}那张存成旧版本。"
            f"本人用 `/ds-rp 操作:改回上一版` 能换成{joining}那张，原来那张会存成旧版本，再改回上一版就换回去。"
        )
    if plan.cards_deleted:
        lines.append(
            f"- 有 {plan.cards_deleted} 个人在{winner}那边删掉了卡（删得比{joining}那张新）：{joining}那张只存成旧版本，不顶上去；"
            "本人想要的话用 `/ds-rp 操作:改回上一版` 拿回来。"
        )
    lines.append("- 设置、预算、人设还是各管各的。组里每个服务器的人都可能间接知道别的服务器的事，最好先跟大家说一声。")
    lines.append(f"- 以后退出组时，那个服务器会带走一份组里的{what}（不会分回连起来之前的样子）。")
    lines.append("- ⚠️ 服务器组还是 Beta 功能，可能还有没发现的问题。还没备份过的话，最好先备份 `data/` 文件夹再连（见 README 的“十一”）。")
    return "\n".join(lines)


def linked_text(bot: "DSBot", result: Linked) -> str:
    joining = server_label(bot, result.joining)
    members = "、".join(server_label(bot, g) for g in result.members)
    lines = [f"已把{joining}连进{group_name(result.group_id)}，现在组里有：{members}。"]
    if cards_shared(bot):
        cards = "人设卡是所有服务器共用的，没动"
    else:
        cards = f"人设卡搬过去 {result.cards_moved} 张"
        if result.cards_kept:
            cards += f"；{result.cards_kept} 个人的{joining}那张存成了旧版本（两边都有卡，或者组里删掉了他的卡）"
    lines.append(f"长期记忆并进来 {result.memories_added} 条，组里现在 {result.memory_total} 条；{cards}。")
    if result.renumbered:
        lines.append(f"{joining}原来的长期记忆接在组里原来的后面，编号变了：要删哪条，先重新 `/ds 长期记忆 操作:查看`。")
    limit = bot.cfg.engine.memory_max_entries
    if result.memory_total > limit:
        lines.append(
            f"⚠️ 长期记忆超过上限 {limit} 条了：删到 {limit - 1} 条以内之前不会记新的。整理记忆时每个服务器只合并、删改自己服务器记的，"
            "最好用 `/ds 长期记忆` 删几条。"
        )
    return "\n".join(lines)


def unlink_preview_text(bot: "DSBot", plan: UnlinkPlan, guild_id: int) -> str:
    label = server_label(bot, guild_id)
    shared = cards_shared(bot)
    what = "长期记忆" if shared else "人设卡和长期记忆"
    if shared:
        take = f"- {label}会带走一份组里现在的长期记忆（{plan.memories} 条）；人设卡是所有服务器共用的，不动。"
    else:
        take = f"- {label}会带走一份组里现在的长期记忆（{plan.memories} 条），和在{label}出现过的人的人设卡（{plan.cards_taken} 张）；"
    lines = [f"让{label}退出{group_name(plan.group_id)}？", take + "之后各记各的，也不能再翻组里别的服务器的记录。"]
    if plan.cards_moved_out:
        lines.append(f"- 其中 {plan.cards_moved_out} 个人只在{label}出现过：他们的卡跟着{label}走，从组里拿掉。")
    if len(plan.rest) == 1:
        lines.append(f"- 组里只剩{server_label(bot, plan.rest[0])}：组就散了，它留着组里剩下的{what}。")
    elif plan.rest:
        lines.append(f"- 组里还剩{'、'.join(server_label(bot, g) for g in plan.rest)}，照样共用。")
    return "\n".join(lines)


def unlinked_text(bot: "DSBot", result: Unlinked) -> str:
    label = server_label(bot, result.left)
    shared = cards_shared(bot)
    what = "长期记忆" if shared else "人设卡和长期记忆"
    if shared:
        text = f"{label}已经退出{group_name(result.group_id)}，带走了 {result.memories_copied} 条长期记忆（人设卡是所有服务器共用的，没动）。"
    else:
        text = f"{label}已经退出{group_name(result.group_id)}，带走了 {result.cards_copied} 张人设卡、{result.memories_copied} 条长期记忆。"
    if result.cards_moved_out:
        text += f"\n只在{label}出现过的 {result.cards_moved_out} 个人，卡跟着{label}走了（组里不留）。"
    if result.dissolved is not None:
        text += f"\n组里只剩{server_label(bot, result.dissolved)}，组散了：它留着组里剩下的{what}自己用。"
    elif result.members:
        text += f"\n组里还有：{'、'.join(server_label(bot, g) for g in result.members)}。"
    return text


def recover_groups(bot: "DSBot") -> None:
    """上次没做完的连组、退组接着做完（执行组命令前先做）。退出的那几个服务器，频道里附过的私信记忆照新的情况撤回。
    散掉的组在卡、记忆文件里还剩数据（文件之间对不上）：写警告，不删。
    所有服务器共用一张人设卡时不动人设卡（按卡文件里记的算：启动时先接着做完，再照 config.toml 换成共用或者分开）。"""
    done = recover(bot.store, group_cards(bot), bot.memory, now=time.time())
    engine = getattr(bot, "engine", None)
    if done and engine is not None:
        with durable(bot.store):  # 撤回也马上写进磁盘：退组的那几步已经落盘了，别让撤回赶上断电丢掉
            engine.groups_changed([g for guild, group in done for g in (guild, *bot.store.group_members(group))], left=True)
    for group, (cards, memories) in leftovers(bot.store, bot.cards, bot.memory).items():
        log.warning(
            "服务器组：%s 已经不在了，cards.sqlite3 / memory.sqlite3 里却还有它的 %d 个人的人设卡、%d 条长期记忆（没删）。"
            "多半是只还原了其中一两个文件的备份：data/ 里三个 sqlite3 文件要用同一时刻的备份一起还原。",
            group_name(group),
            cards,
            memories,
        )


def apply_card_mode(bot: "DSBot") -> None:
    """启动时照 config.toml 的 [bot] shared_cards 换成所有服务器共用一张人设卡，或者改回每个服务器一张（先接着做完上次
    没做完的连组、退组再调）。出错就还按卡文件里原来记的方式用（一次事务，没改成就像没改过），下次启动再试。"""
    cards = bot.cards
    if cards is None:
        return
    want = bool(bot.cfg.bot.shared_cards)
    try:
        alive = set(bot.store.groups()) | {group for _, group in bot.store.leaving()}
        with durable(cards):  # 换完马上写进磁盘：之后的连组、退组照新的方式记，断电也不能退回换之前
            change = cards.set_shared(want, scope_of=bot.store.memory_scope, alive=alive)
    except Exception:
        log.exception("人设卡：没能换成%s，还按原来的方式用", "所有服务器共用一张" if want else "每个服务器一张")
        return
    if change is None:
        return
    if change.shared:
        text = f"人设卡：所有服务器共用一张（[bot] shared_cards = true）：并好了 {change.people} 个人的卡"
        if change.merged:
            text += f"；其中 {change.merged} 个人在几个服务器各有一张，用最近改过的那张，别的存成旧版本（本人用 /ds-rp 改回上一版能换过去）"
    else:
        text = (
            f"人设卡：改回每个服务器一张（[bot] shared_cards = false）：{change.people} 个人的卡复制回了他们出现过的服务器"
            "（连成一组的放在组里）"
        )
        if change.stayed:
            text += f"；{change.stayed} 个人没在任何服务器出现过，卡还留在共用的那份里（再改成共用时还在）"
    log.info("%s", text)


def do_link(bot: "DSBot", guild_id: int, other_id: int) -> str:
    result = link(bot.store, group_cards(bot), bot.memory, guild_id, other_id, now=time.time())
    engine = getattr(bot, "engine", None)
    if engine is not None:
        engine.groups_changed(result.members, left=False)
    log.info("bot 主人把服务器 %s 连进了%s（%s）", result.joining, group_name(result.group_id), result.members)
    return linked_text(bot, result)


def do_unlink(bot: "DSBot", guild_id: int) -> str:
    group = bot.store.group_of(guild_id)
    if group is None:
        raise GroupError("这个服务器不在任何组里。")
    before = bot.store.group_members(group)
    try:
        result = unlink(bot.store, group_cards(bot), bot.memory, guild_id, now=time.time())
    finally:
        # 不管后面复制成没成，只要已经不算组里的了，就照新的情况撤回附过的私信记忆（附的时候是按原来的组算的）
        engine = getattr(bot, "engine", None)
        if engine is not None and bot.store.group_of(guild_id) is None:
            with durable(bot.store):  # 撤回也马上写进磁盘（退组的那几步已经落盘了）
                engine.groups_changed(before, left=True)
    log.info("bot 主人让服务器 %s 退出了%s（剩下 %s）", guild_id, group_name(result.group_id), result.members)
    return unlinked_text(bot, result)


class ConfirmGroup(discord.ui.View):
    """连成一组、退出组之前再确认一次（只有发起的人能点）。点的时候照当时的情况再算一次，和给他看的对不上就不办。"""

    def __init__(self, user_id: int, run: Callable[[], str], label: str, check: Callable[[], Any], expected: Any):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.run = run
        self.check = check
        self.expected = expected
        button: discord.ui.Button[Any] = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        button.callback = self.confirm  # type: ignore[method-assign]
        self.add_item(button)

    async def confirm(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await _reply(interaction, "只有发起的人能确认。")
            return
        self.stop()
        try:
            if self.check() != self.expected:
                text = "情况变了（刚才有别的连组、退组）：这次没办。再执行一次命令，照现在的情况看看会怎样。"
            else:
                text = self.run()
        except GroupError as exc:
            text = f"没办成：{exc}"
        except Exception as exc:  # 数据库出错之类：做到一半的，下次执行组命令时会接着做完
            log.exception("服务器组：连组、退组出错")
            text = f"出错了：{exc}\n做到一半的部分，下次执行 `/dsowner 服务器组`（随便哪个操作）时会接着做完。"
        await interaction.response.edit_message(content=text, view=None)


async def group_command(bot: "DSBot", interaction: discord.Interaction, op: str, server: str | None, other: str | None) -> None:
    """/dsowner 服务器组：查看；连起来、退出要先确认。先把上次没做完的接着做完。"""
    recover_groups(bot)
    if op == "list":
        await _reply_long(interaction, groups_text(bot))
        return
    guild_id = _guild_arg(server)
    if guild_id is None:
        await _reply(interaction, "在「服务器」一栏从弹出的列表里选一个服务器（输入几个字能筛选）。")
        return
    try:
        if op == "link":
            other_id = _guild_arg(other)
            if other_id is None:
                await _reply(interaction, "在「连到」一栏从弹出的列表里选要连到哪个服务器。")
                return
            missing = [g for g in (guild_id, other_id) if bot.get_guild(g) is None]
            if missing:
                await _reply(interaction, f"bot 不在 {'、'.join(str(g) for g in missing)} 这个服务器里，连不了。")
                return
            cards = group_cards(bot)  # 所有服务器共用一张人设卡时：连组不动卡
            plan = plan_link(bot.store, cards, bot.memory, guild_id, other_id)
            text = link_preview_text(bot, plan)
            run, label = (lambda: do_link(bot, guild_id, other_id)), "确认连起来"
            check: Callable[[], Any] = lambda: plan_link(bot.store, cards, bot.memory, guild_id, other_id).key()  # noqa: E731
            expected: Any = plan.key()
        else:
            cards = group_cards(bot)
            unplan = plan_unlink(bot.store, cards, bot.memory, guild_id)
            text = unlink_preview_text(bot, unplan, guild_id)
            run, label = (lambda: do_unlink(bot, guild_id)), "确认退出"
            check = lambda: plan_unlink(bot.store, cards, bot.memory, guild_id).key()  # noqa: E731
            expected = unplan.key()
    except GroupError as exc:
        await _reply(interaction, str(exc))
        return
    await interaction.response.send_message(
        text, view=ConfirmGroup(interaction.user.id, run, label, check, expected), ephemeral=True
    )


def register_dm_commands(bot: "DSBot") -> None:
    """/ds-dm：服务器里和私信里都能用（回复只有自己看得到）。"""
    dm = app_commands.Group(
        name="ds-dm",
        description="私信它：私信里用哪个服务器的人设和记忆、今天的额度（服务器里、私信里都能用）",
        allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=False),
        # 只跟着 bot 进服务器：装在自己账号上的话，它会出现在 bot 不在的服务器里
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
    )

    async def guild_choices(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
        words = current.strip().casefold()
        if interaction.guild_id is not None and bot.get_guild(interaction.guild_id) is not None:
            bot.note_member(interaction.guild_id, interaction.user.id)  # 在这里打命令：当然是这里的成员
        guilds = await bot.dm_candidates(interaction.user.id)
        return [Choice(name=g.name[:100], value=str(g.id)) for g in guilds if not words or words in g.name.casefold()][:25]

    @dm.command(
        name="服务器",
        description=(
            "私信里用哪个服务器的人设和长期记忆（换了会清空私信记录）"
            if cards_shared(bot)
            else "私信里用哪个服务器的人设、长期记忆和人设卡（换了会清空私信记录）"
        ),
    )
    @app_commands.rename(target="服务器")
    @app_commands.describe(target="你和它都在的服务器（输入几个字可以筛选）；在服务器里用、不填 = 这个服务器")
    @app_commands.autocomplete(target=guild_choices)
    async def dm_server(interaction: discord.Interaction, target: Optional[str] = None) -> None:
        user_id = interaction.user.id
        wanted = (target or "").strip()
        if not wanted and interaction.guild_id is None:
            await _reply(interaction, "选一个服务器（输入框会列出你和它都在的服务器）。")
            return
        await interaction.response.defer(ephemeral=True)  # 要去问 Discord 他在哪些服务器里，可能超过 3 秒
        here = bot.get_guild(interaction.guild_id) if interaction.guild_id is not None else None
        if here is not None:
            bot.note_member(here.id, user_id)  # 他在这里用命令，当然是这里的成员，不用再去查
        if not wanted or (here is not None and wanted in (str(here.id), here.name)):
            # 在服务器里用、不填（或者填的就是这里）：就是这个服务器
            guild = here
            if guild is None:
                await _reply(interaction, "我不在这个服务器里：换一个你和我都在的服务器再用这个命令。")
                return
            if not bot.dm_open(guild):
                await _reply(interaction, "这个服务器关掉了私信（管理员可以用 `/ds 设置 参数:私信` 打开）。")
                return
        else:
            named = bot.get_guild(int(wanted)) if wanted.isdecimal() else None  # 从列表里选的是服务器 ID
            if named is not None:
                if not bot.dm_open(named):
                    await _reply(interaction, f"「{discord_safe(named.name)}」关掉了私信。")
                    return
                member = await bot.is_member(named, user_id)
                if member is None:
                    await _reply(interaction, "Discord 一时没回答你在不在那个服务器里，过一会儿再试。")
                    return
                guild = named if member else None
            else:
                guilds = await bot.dm_candidates(user_id)
                guild = next((g for g in guilds if g.name == wanted), None)
            if guild is None:
                await _reply(interaction, "找不到这个服务器：要是你和它都在、那边也开着私信的服务器（从弹出的列表里选）。")
                return
        name = discord_safe(guild.name)
        dm_channel = interaction.channel_id if interaction.guild_id is None else None
        already, moved = await bot.set_dm_home(user_id, guild, dm_channel)
        if already:
            group_note = dm_group_note(bot, guild.id)
            await _reply(interaction, f"私信现在用的就是「{name}」。" + (f"\n{group_note}" if group_note else "") + f"\n{dm_warning(bot)}")
            return
        if cards_shared(bot):
            text = f"好了，私信里用「{name}」的人设、长期记忆，和你在所有服务器共用的那张人设卡来聊。"
        else:
            text = f"好了，私信里用「{name}」的人设、长期记忆和你在那里的人设卡来聊。"
        if moved:
            text += "之前的私信记录清空了（服务器那边的记忆不受影响）。"
        if interaction.guild_id is not None:
            text += "直接私信它就能聊。"
        group_note = dm_group_note(bot, guild.id)
        await _reply(interaction, text + (f"\n{group_note}" if group_note else "") + f"\n{dm_warning(bot)}")

    async def switch_note(user_id: int, home: tuple[int, int | None]) -> str:
        """私信原来用的服务器用不了了：下次私信会怎样。"""
        others = [g for g in await bot.dm_candidates(user_id) if g.id != home[0]]
        if not others:
            return "现在没有别的你和它都在、开着私信的服务器，私信暂时聊不了。"
        cleared = "，私信记录会清空" if home[1] is not None else ""
        return f"下次私信时会改用「{discord_safe(others[0].name)}」{cleared}（也可以用 `/ds-dm 服务器` 自己选）。"

    @dm.command(name="状态", description="私信现在用哪个服务器、今天私信花了多少")
    async def dm_status(interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        await interaction.response.defer(ephemeral=True)  # 可能要去问 Discord 他还在不在那个服务器
        if interaction.guild_id is not None and bot.get_guild(interaction.guild_id) is not None:
            bot.note_member(interaction.guild_id, user_id)
        home = bot.store.dm_home(user_id)
        if home is None:
            await _reply(
                interaction,
                "私信还没定用哪个服务器：直接私信它随便说句话就会定下来，或者用 `/ds-dm 服务器` 选一个。\n" + dm_warning(bot),
            )
            return
        guild = bot.get_guild(home[0])
        if guild is None:
            await _reply(interaction, "⚠️ 它已经不在私信原来用的那个服务器里了。" + await switch_note(user_id, home))
            return
        channel_id = dm_channel_of(bot, interaction)
        now = time.time()
        spent = bot.store.channel_spent(bot.ledger.day(now), channel_id) if channel_id is not None else 0.0
        cap = bot.settings.get("dm_daily_budget_usd", guild_id=guild.id)
        shared = cards_shared(bot)
        if shared:
            lines = [f"私信用的是「{discord_safe(guild.name)}」的人设、长期记忆，和你在所有服务器共用的那张人设卡。", dm_warning(bot)]
        else:
            lines = [f"私信用的是「{discord_safe(guild.name)}」的人设、长期记忆和人设卡。", DM_WARNING]
        group_note = dm_group_note(bot, guild.id)
        if group_note:
            lines.insert(1, group_note)
        memory = getattr(bot, "memory", None)
        if memory is not None:
            entries = memory.dm_entries(user_id)
            hidden = sum(1 for e in entries if e.sensitive)
            if not bot.settings.get("dm_memory", guild_id=guild.id):
                lines.append("私信记忆：那个服务器关掉了（已经记下的还在，用 `/ds-dm 记忆` 看）")
            else:
                lines.append(f"私信记忆：{len(entries)} 条（{hidden} 条敏感），用 `/ds-dm 记忆` 查看和管理")
        if user_id == bot.secrets.owner_id:
            lines.append(f"今天私信花了 ${spent:.4f}（你是 bot 的主人，不受每人上限限制）")
        else:
            lines.append(f"今天私信花了 ${spent:.4f} / ${cap:g}")
        if not bot.dm_open(guild):
            lines.append("⚠️ 那个服务器现在关掉了私信。" + await switch_note(user_id, home))
        elif interaction.guild_id != guild.id and await bot.is_member(guild, user_id) is False:
            lines.append("⚠️ 你已经不在那个服务器里了。" + await switch_note(user_id, home))
        await _reply(interaction, "\n".join(lines))

    async def memory_numbers(interaction: discord.Interaction, current: str) -> list[Choice[int]]:
        return dm_memory_choices(bot, interaction.user.id, current)

    @dm.command(name="记忆", description="你的私信记忆（它和你之间记下的事）：查看、删掉某条、改成敏感，或者全部清空")
    @app_commands.rename(op="操作", no="编号")
    @app_commands.describe(op="不选 = 查看", no="删除、改敏感标记时选第几条（输入几个字可以筛选）")
    @app_commands.choices(
        op=[
            Choice(name="查看", value="view"),
            Choice(name="删除", value="delete"),
            Choice(name="设为敏感（只在私信里用）", value="private"),
            Choice(name="设为不敏感", value="public"),
            Choice(name="清空", value="clear"),
        ]
    )
    @app_commands.autocomplete(no=memory_numbers)
    async def dm_memory(interaction: discord.Interaction, op: Optional[Choice[str]] = None, no: Optional[int] = None) -> None:
        if getattr(bot, "memory", None) is None:
            await _reply(interaction, "私信记忆没有启用。")
            return
        user_id = interaction.user.id
        what = op.value if op is not None else "view"
        if what == "view":
            await _reply_long(interaction, dm_memory_text(bot, user_id))
            return
        if what == "clear":
            n = bot.memory.dm_clear(user_id)
            withdraw_dm_memory(bot, user_id)
            await _reply(interaction, f"已清空你的私信记忆（{n} 条）。之后整理聊天时，也不会拿清空之前的聊天把它们重新记回来。")
            return
        if no is None:
            await _reply(interaction, "填上编号（先用 `/ds-dm 记忆` 查看，每条前面括号里的数字就是编号）。")
            return
        if what == "delete":
            current = bot.memory.dm_get(user_id, no)
            ok = bot.memory.dm_delete(user_id, no)
            if ok and current is not None and not current.sensitive:
                withdraw_dm_memory(bot, user_id)  # 频道里可能附过它
            await _reply(interaction, f"已删掉第 {no} 条。" if ok else f"没有第 {no} 条（用 `/ds-dm 记忆` 看看编号）。")
            return
        sensitive = what == "private"
        current = bot.memory.dm_get(user_id, no)
        if current is None:
            await _reply(interaction, f"没有第 {no} 条（用 `/ds-dm 记忆` 看看编号）。")
            return
        bot.memory.dm_edit(user_id, no, None, sensitive=sensitive, via="command")
        if sensitive and not current.sensitive:
            withdraw_dm_memory(bot, user_id)  # 改成敏感：频道里附过的撤回
        await _reply(
            interaction,
            f"第 {no} 条改成了" + ("敏感：只在私信里用。" if sensitive else "不敏感：你在服务器里叫它时也会用上。"),
        )

    @dm.command(name="清空", description="清空私信里的聊天记录（服务器的长期记忆和你的人设卡不受影响）")
    async def dm_reset(interaction: discord.Interaction) -> None:
        channel_id = dm_channel_of(bot, interaction)
        if channel_id is None:
            await _reply(interaction, "你还没私信过它，没有私信记录。")
            return
        bot.engine.reset_channel(channel_id)
        await _reply(
            interaction,
            "私信记录清空了，它从头开始（服务器的长期记忆、你的人设卡和私信记忆不受影响；私信记忆用 `/ds-dm 记忆` 管）。",
        )

    bot.tree.add_command(dm)


def register_music_commands(bot: "DSBot") -> None:
    """/ds-music：人人都能用。点歌的人要在语音频道里；切歌、暂停、继续、停止、删除、清空要在它放歌的那个语音频道里。"""
    group = app_commands.Group(name="ds-music", description="点歌：在语音频道里放 B站视频的声音", guild_only=True)

    async def ready(interaction: discord.Interaction) -> bool:
        if bot.music is None:
            await _reply(interaction, f"点歌没有启用：{bot.music_problem}")
            return False
        if not bot.engine.enabled(*_ids(interaction)):
            await _reply(interaction, "这个频道不在它回应的区域里，换个频道点歌。")
            return False
        return True

    def name_of(interaction: discord.Interaction) -> str:
        return getattr(interaction.user, "display_name", None) or interaction.user.name

    async def run(interaction: discord.Interaction, op: str, query: str = "") -> None:
        if not await ready(interaction):
            return
        if op in ("play", "stop"):
            await interaction.response.defer(thinking=True)  # 找歌、拿视频信息、离开语音频道要几秒
        text = await bot.engine.music_request(
            interaction.guild_id,
            interaction.channel_id,
            interaction.user.id,
            name_of(interaction),
            op,
            query,
            view=bot.settings.view(*_ids(interaction)),
        )
        footer = ""
        if op == "queue" and bot.music is not None and bot.music.queued(interaction.guild_id):
            footer = "\n-# 删掉某一首：`/ds-music 删除`（会列出队列里的歌）；清空后面排着的：`/ds-music 清空`"
        await _say_public(interaction, text, footer)

    @group.command(name="播放", description="点一首歌：写歌名（加上歌手更准），或者贴 B站视频链接")
    @app_commands.rename(query="歌")
    @app_commands.describe(query="比如“周杰伦 晴天”，或者 B站视频链接、BV 号（只能放 B站的，不放直播）")
    async def play_cmd(interaction: discord.Interaction, query: app_commands.Range[str, 1, 200]) -> None:
        await run(interaction, "play", query)

    @group.command(name="切歌", description="跳过正在放的这首（要在它放歌的语音频道里）")
    async def skip_cmd(interaction: discord.Interaction) -> None:
        await run(interaction, "skip")

    @group.command(name="暂停", description="暂停正在放的歌（要在它放歌的语音频道里）")
    async def pause_cmd(interaction: discord.Interaction) -> None:
        await run(interaction, "pause")

    @group.command(name="继续", description="接着放暂停的歌（要在它放歌的语音频道里）")
    async def resume_cmd(interaction: discord.Interaction) -> None:
        await run(interaction, "resume")

    @group.command(name="停止", description="停止放歌、清空队列，让它离开语音频道（要在它放歌的语音频道里）")
    async def stop_cmd(interaction: discord.Interaction) -> None:
        await run(interaction, "stop")

    @group.command(name="队列", description="看看正在放什么、后面还有哪些")
    async def queue_cmd(interaction: discord.Interaction) -> None:
        await run(interaction, "queue")

    @group.command(name="删除", description="从队列里删掉一首：输入时会列出队列里的歌；不填就删你自己最近点的那首")
    @app_commands.rename(target="歌")
    @app_commands.describe(target="选一首，或者填队列里的编号（看“队列”）、歌名；不填 = 你自己最近点的那首")
    async def remove_cmd(interaction: discord.Interaction, target: Optional[app_commands.Range[str, 1, 100]] = None) -> None:
        await run(interaction, "remove", target or "")

    @remove_cmd.autocomplete("target")
    async def remove_choices(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
        music = bot.music
        if music is None or interaction.guild_id is None:
            return []
        try:
            if not bot.engine.enabled(*_ids(interaction)):
                return []
            return [Choice(name=name, value=value) for name, value in music.choices(interaction.guild_id, current or "")]
        except Exception:
            log.exception("列出队列里的歌出错")
            return []

    @group.command(name="清空", description="清掉后面排着的歌，正在放的这首接着放完（要在它放歌的语音频道里）")
    async def clear_cmd(interaction: discord.Interaction) -> None:
        await run(interaction, "clear")

    bot.tree.add_command(group)


def register_commands(bot: "DSBot") -> None:
    admin = app_commands.Group(
        name="ds",
        description="DeepSeek 群友：本服务器的设置",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @admin.command(name="区域", description="限制它在哪些频道或类别里回应（不允许的地方完全不看消息）")
    @app_commands.rename(action="操作", scope="范围")
    @app_commands.describe(action="允许 / 禁止 / 跟随上级（删掉这一层的设置）", scope="本频道、本类别，还是全服务器（不选 = 本频道）")
    @app_commands.choices(action=AREA_ACTIONS, scope=SCOPE_CHOICES)
    async def area_cmd(interaction: discord.Interaction, action: Choice[str], scope: Optional[Choice[str]] = None) -> None:
        target = _target(interaction, scope)
        if target is None:
            await _reply(interaction, NO_CATEGORY)
            return
        scope_name, scope_id, where = target
        if action.value == "inherit":
            bot.settings.unset(scope_name, scope_id, "enabled")
        else:
            bot.settings.set(scope_name, scope_id, "enabled", action.value)
        view = bot.settings.view(*_ids(interaction))
        lines = [
            f"{where}：已设为「{action.name}」。",
            f"现在这个频道：{'会回应' if view.enabled else '不回应'}（来自{view.source('enabled')}）",
        ]
        if scope_name == "guild" and action.value == "off":
            lines.append("现在是白名单模式：在要用的频道里执行 `/ds 区域 操作:允许`（或选“本类别”放开整个区）。")
        await _reply(interaction, "\n".join(lines))

    @admin.command(name="记忆共享", description="让它在开了共享的频道之间互通记忆（私密频道要单独开才会分享出去）")
    @app_commands.rename(action="操作", scope="范围")
    @app_commands.describe(action="开 / 关 / 跟随上级（删掉这一层的设置）", scope="本频道、本类别，还是全服务器（不选 = 本频道）")
    @app_commands.choices(action=SHARE_ACTIONS, scope=SCOPE_CHOICES)
    async def share_cmd(interaction: discord.Interaction, action: Choice[str], scope: Optional[Choice[str]] = None) -> None:
        target = _target(interaction, scope)
        if target is None:
            await _reply(interaction, NO_CATEGORY)
            return
        scope_name, scope_id, where = target
        if action.value == "inherit":
            bot.settings.unset(scope_name, scope_id, "share_memory")
            done = "改为跟随上级"
        else:
            bot.settings.set(scope_name, scope_id, "share_memory", action.value)
            done = "打开" if action.value == "on" else "关闭"
        lines = [f"{where}：记忆共享已{done}。", share_text(bot, interaction)]
        if action.value == "on" and scope_name != "channel":
            lines.append("（私密频道就算在这个范围里也不会把内容分享出去，除非在那个频道里单独开。）")
        await _reply(interaction, "\n".join(lines))

    @admin.command(name="表情包", description="管理这个服务器的表情包库：上传、删除、查看（它会按名字挑着发）")
    @app_commands.rename(action="操作", name="名字", image="图片")
    @app_commands.describe(
        action="上传 / 删除 / 列表",
        name="表情包的名字，写清楚是什么意思，比如“熊猫头-无语”（它按名字挑图）；上传时不填就用文件名",
        image="上传时附上图片：PNG、JPG、GIF、WebP，8 MB 以内",
    )
    @app_commands.choices(action=MEME_ACTIONS)
    async def meme_cmd(
        interaction: discord.Interaction,
        action: Choice[str],
        name: Optional[str] = None,
        image: Optional[discord.Attachment] = None,
    ) -> None:
        guild_id = interaction.guild_id
        library = bot.memes
        if action.value == "list":
            await _reply(interaction, meme_list_text(bot, guild_id))
            return
        if action.value == "remove":
            if not name or not name.strip():
                await _reply(interaction, "要删哪一张？在「名字」里填上表情包的名字（`/ds 表情包 操作:列表` 能看到全部）。")
                return
            await interaction.response.defer(ephemeral=True, thinking=True)  # 可能要等正在上传的那张写完
            result = await asyncio.to_thread(library.remove, guild_id, name)
            if result == "removed":
                await _reply(interaction, f"已删掉表情包「{name.strip()}」。")
            elif result == "shared":
                await _reply(interaction, f"「{name.strip()}」是通用库里的，要在 NAS 上的 `{bot.cfg.bot.meme_dir}/` 文件夹里删。")
            else:
                await _reply(interaction, f"这个服务器没有叫「{name.strip()}」的表情包。")
            return
        if image is None:
            await _reply(interaction, "上传要在「图片」里附上一张图。")
            return
        if image.size > library.max_bytes:
            await _reply(interaction, f"图片太大了（最多 {library.max_bytes // 1_000_000} MB）。")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)  # 下载图片要一会儿
        try:
            data = await image.read()
        except discord.HTTPException as exc:
            await _reply(interaction, f"下载图片失败：{exc}")
            return
        try:
            meme = await asyncio.to_thread(library.add, guild_id, name or PurePath(image.filename).stem, data)
        except ValueError as exc:
            await _reply(interaction, f"没存成：{exc}")
            return
        except OSError as exc:
            log.exception("保存表情包失败")
            await _reply(interaction, f"没存成（写文件出错）：{exc}")
            return
        text = f"已加入这个服务器的表情包库：「{meme.name}」。它看名字决定什么时候发，名字写清楚是什么意思最好。"
        if not bot.settings.view(*_ids(interaction)).images:
            text += "\n⚠️ 但这里关掉了发图，它不会发：`/ds 设置 参数:发图 值:开`"
        await _reply(interaction, text)

    async def memory_entry_numbers(interaction: discord.Interaction, current: str) -> list[Choice[int]]:
        return memory_number_choices(bot, interaction.guild_id, current)

    @admin.command(name="长期记忆", description="查看、添加、删除这个服务器的长期记忆（它记下的约定、大事、梗和剧情）")
    @app_commands.rename(action="操作", text="内容", number="编号")
    @app_commands.describe(
        action="查看 / 添加 / 删除 / 清空",
        text="添加时填要记的事（一条只记一件事）",
        number="删除时选第几条（列表里有内容；输入几个字可以筛选）",
    )
    @app_commands.choices(action=MEMORY_ACTIONS)
    @app_commands.autocomplete(number=memory_entry_numbers)
    async def memory_cmd(
        interaction: discord.Interaction,
        action: Choice[str],
        text: Optional[str] = None,
        number: Optional[int] = None,
    ) -> None:
        guild_id = interaction.guild_id
        memory = bot.memory
        if memory is None:
            await _reply(interaction, "长期记忆没有启用。")
            return
        ec = bot.cfg.engine
        scope = bot.store.memory_scope(guild_id)  # 连成一组的服务器：组里共用的那份
        if action.value == "list":
            await _reply_long(interaction, memory_list_text(bot, guild_id))
        elif action.value == "add":
            entry = " ".join((text or "").split())
            if not entry:
                await _reply(interaction, "要记什么？在「内容」里填上。")
            elif len(entry) > ec.memory_entry_chars:
                await _reply(interaction, f"太长了（{len(entry)} 字），一条最多 {ec.memory_entry_chars} 字。")
            elif (n := memory.count(scope)) >= ec.memory_max_entries:
                await _reply(
                    interaction,
                    f"已经记满了（现在 {n} 条，最多 {ec.memory_max_entries} 条）：先删掉 {n - ec.memory_max_entries + 1} 条才能再记。",
                )
            else:
                no = memory.add(scope, entry, via="command", origin=guild_id)
                await _reply(interaction, f"已记下第 {no} 条：{entry}")
        elif action.value == "delete":
            current = memory.get(scope, number) if number is not None else None
            if number is None:
                await _reply(interaction, "要删哪一条？在「编号」里填上（`/ds 长期记忆 操作:查看` 能看到编号）。")
            elif current is not None and memory.delete(scope, number):
                whose = _memory_labels(bot, guild_id, [current]).get(number)
                # 写出来：编号记错了也看得出删的是哪条；连成一组时，删的是别的服务器记的也写明
                await _reply(interaction, f"已删掉第 {number} 条：{current.text}" + (f"（这条是{whose}记的）" if whose else ""))
            else:
                await _reply(interaction, f"没有第 {number} 条。")
        else:
            n = memory.count(scope)
            if not n:
                await _reply(interaction, "长期记忆本来就是空的。")
                return
            grouped = "（组里的服务器共用这一份，那边的也会一起清空）" if group_others(bot, guild_id) else ""
            await interaction.response.send_message(
                f"确定要清空{shared_where(bot, guild_id)}的 {n} 条长期记忆吗？清空后找不回来。{grouped}",
                view=ConfirmClear(bot, guild_id, interaction.user.id),
                ephemeral=True,
            )

    @admin.command(name="区域列表", description="看看它在这个服务器的哪些地方会回应")
    async def area_list_cmd(interaction: discord.Interaction) -> None:
        await _reply(interaction, area_list_text(bot, interaction.guild))

    @admin.command(name="主动", description="开关主动发言（没被叫到时，自己判断要不要说话）")
    @app_commands.rename(state="开关", scope="范围")
    @app_commands.describe(state="开还是关", scope="本频道、本类别，还是全服务器默认（不选 = 本频道）")
    @app_commands.choices(state=[Choice(name="开", value="on"), Choice(name="关", value="off")], scope=SCOPE_CHOICES)
    async def proactive_cmd(
        interaction: discord.Interaction, state: Choice[str], scope: Optional[Choice[str]] = None
    ) -> None:
        target = _target(interaction, scope)
        if target is None:
            await _reply(interaction, NO_CATEGORY)
            return
        scope_name, scope_id, where = target
        bot.settings.set(scope_name, scope_id, "proactive", state.value)
        text = f"{where}：主动发言已{'打开' if state.value == 'on' else '关闭'}。"
        if state.value == "on" and not bot.settings.view(*_ids(interaction)).enabled:
            text += "\n⚠️ 但这个频道不在允许的区域，它不会说话。先用 `/ds 区域 操作:允许`。"
        await _reply(interaction, text)

    @admin.command(name="档位", description="一键套用发言频率：安静 / 普通 / 话痨")
    @app_commands.rename(preset="档位", scope="范围")
    @app_commands.describe(preset="选一个档位", scope="本频道、本类别，还是全服务器默认（不选 = 本频道）")
    @app_commands.choices(
        preset=[Choice(name=f"{name}：{_preset_text(values)}", value=name) for name, values in PRESETS.items()],
        scope=SCOPE_CHOICES,
    )
    async def preset_cmd(
        interaction: discord.Interaction, preset: Choice[str], scope: Optional[Choice[str]] = None
    ) -> None:
        target = _target(interaction, scope)
        if target is None:
            await _reply(interaction, NO_CATEGORY)
            return
        scope_name, scope_id, where = target
        for key, value in PRESETS[preset.value].items():
            bot.settings.set(scope_name, scope_id, key, value)
        await _reply(interaction, f"{where}：已切到「{preset.value}」档（{_preset_text(PRESETS[preset.value])}）。")

    @admin.command(name="设置", description="调整单项参数（值填“默认”恢复）")
    @app_commands.rename(key="参数", value="值", scope="范围")
    @app_commands.describe(
        key="要改哪个参数", value="新的值；填“默认”恢复成上一级的设置", scope="本频道、本类别，还是全服务器默认（不选 = 本频道）"
    )
    @app_commands.choices(key=[Choice(name=SPECS[k].label, value=k) for k in ADMIN_SET_KEYS], scope=SCOPE_CHOICES)
    async def set_cmd(
        interaction: discord.Interaction, key: Choice[str], value: str, scope: Optional[Choice[str]] = None
    ) -> None:
        # 私信、私信记忆、跨服务器不在任何频道、类别里：只看全服务器这一级
        whole = key.value in GUILD_ONLY_KEYS
        target = _target(interaction, scope) if not whole else ("guild", interaction.guild_id, "全服务器")
        if target is None:
            await _reply(interaction, NO_CATEGORY)
            return
        scope_name, scope_id, where = target
        spec = SPECS[key.value]
        if value.strip() in RESET_WORDS:
            bot.settings.unset(scope_name, scope_id, key.value)
            settings_changed(bot, key.value, scope_name, scope_id)
            await _reply(interaction, f"{where}：「{spec.label}」已恢复默认。")
            return
        try:
            parsed = bot.settings.set(scope_name, scope_id, key.value, value)
        except ValueError as exc:
            await _reply(interaction, f"没改成：{exc}")
            return
        settings_changed(bot, key.value, scope_name, scope_id)
        await _reply(interaction, f"{where}：「{spec.label}」= {format_value(spec, parsed)}\n（{spec.help}）")

    @admin.command(name="人设", description="修改人设（会弹出编辑框）")
    @app_commands.rename(scope="范围")
    @app_commands.describe(scope="全服务器、本类别，还是只改本频道（不选 = 全服务器）")
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def persona_cmd(interaction: discord.Interaction, scope: Optional[Choice[str]] = None) -> None:
        target = _target(interaction, scope, default="guild")
        if target is None:
            await _reply(interaction, NO_CATEGORY)
            return
        scope_name, scope_id, where = target
        guild_id, _, parent_id, category_id = _ids(interaction)
        # “上一级”：服务器 → 全局；类别 → 服务器；频道 → 帖子所在频道 → 类别 → 服务器
        if scope_name == "guild":
            inherited = bot.settings.get("persona")
        elif scope_name == "category":
            inherited = bot.settings.get("persona", guild_id=guild_id)
        else:
            inherited = bot.settings.get("persona", guild_id=guild_id, channel_id=parent_id, category_id=category_id)
        inherited = inherited or DEFAULT_PERSONA
        current = bot.settings.overrides(scope_name, scope_id).get("persona") or inherited
        await interaction.response.send_modal(PersonaModal(bot, scope_name, scope_id, where, current, inherited))

    @admin.command(name="状态", description="查看本频道的设置、额度和今天的花费")
    async def status_cmd(interaction: discord.Interaction) -> None:
        await _reply(interaction, status_text(bot, interaction))

    @admin.command(name="清空记忆", description="清空本频道的对话记录和摘要，让它从头开始")
    async def reset_cmd(interaction: discord.Interaction) -> None:
        bot.engine.reset_channel(interaction.channel_id)
        await _reply(interaction, "已清空本频道的对话记录和摘要。")

    bot.tree.add_command(admin)

    # 人人都能用（不设 default_permissions）：只能管自己的卡。共用不共用启动时就定了（注册命令之前换好）
    rp_text = (
        "查看、修改你的人设卡（所有服务器共用一张，只有你自己看得到）"
        if cards_shared(bot)
        else "查看、修改你在这个服务器的人设卡（只有你自己看得到）"
    )

    @bot.tree.command(name="ds-rp", description=rp_text)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    @app_commands.rename(action="操作")
    @app_commands.describe(action="编辑（不选就是编辑）/ 改回上一版 / 清空")
    @app_commands.choices(action=CARD_ACTIONS)
    async def rp_cmd(interaction: discord.Interaction, action: Optional[Choice[str]] = None) -> None:
        cards = bot.cards
        if cards is None:
            await _reply(interaction, "人设卡没有启用。")
            return
        guild_id, user_id = interaction.guild_id, interaction.user.id
        if guild_id is None:  # 私信里：私信挂的那个服务器的卡
            home = bot.store.dm_home(user_id)
            if home is None or bot.get_guild(home[0]) is None:
                await _reply(interaction, "先私信我随便说句话，定下私信用哪个服务器，再来改人设卡。")
                return
            guild_id = home[0]
        value = action.value if action is not None else "edit"
        scope = card_scope(bot, guild_id)  # 连成一组的服务器：组里共用的那张；所有服务器共用一张时就是那张
        where = card_where(bot, guild_id)
        if value == "undo":
            name = getattr(interaction.user, "display_name", None) or interaction.user.name
            name = card_written(bot, guild_id, user_id, name, interaction.guild_id is None)
            swap = cards.swaps_on_restore(scope, user_id)  # 换成合并时从别的服务器并进来的那张：现在这张会存成旧版本
            restored = cards.restore_previous(scope, user_id, name=name)
            if restored is None:
                await _reply(interaction, "没有更早的版本了。")
            elif swap:
                await _reply(
                    interaction, f"已换成合并时从别的服务器并进来的那张（原来那张存成了旧版本，再改回上一版就换回去）：\n>>> {restored}"
                )
            else:
                await _reply(interaction, f"已改回上一版：\n>>> {restored}")
        elif value == "clear":
            if cards.delete(scope, user_id):
                await _reply(interaction, f"已删掉你在{where}的人设卡（连同旧版本）。")
            else:
                await _reply(interaction, f"你在{where}还没有人设卡。")
        else:
            card = cards.get(scope, user_id)
            await interaction.response.send_modal(CardModal(bot, guild_id, card.text if card else ""))

    register_music_commands(bot)
    register_dm_commands(bot)

    @bot.tree.error
    async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.error("斜杠命令出错：%s", error, exc_info=error)
        try:
            await _reply(interaction, f"出错了：{error}")
        except discord.HTTPException:
            pass

    if not bot.secrets.control_guild_ids:
        log.info("没有设置 CONTROL_GUILD_ID，不注册主人命令；预算等参数请改 config.toml")
        return

    # 能不能用由 is_owner 决定；default_permissions 只是让普通成员看不到。用“管理服务器”而不是“管理员”：
    # bot 放在朋友的服务器时，你在那里多半只是管理（能用 /ds），不一定有“管理员”权限
    owner = app_commands.Group(
        name="dsowner",
        description="DeepSeek 群友：主人专用",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    async def is_owner(interaction: discord.Interaction) -> bool:
        if interaction.user.id != bot.secrets.owner_id:
            await _reply(interaction, "只有 bot 的主人能用这个命令。")
            return False
        return True

    async def owner_keys(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
        return owner_key_choices(current)

    @owner.command(name="设置", description="调整参数：全局默认，或者指定某个服务器")
    @app_commands.rename(key="参数", value="值", target="服务器id")
    @app_commands.describe(
        key="要改哪个参数（输入几个字可以筛选）", value="新的值；填“默认”恢复", target="只改某个服务器就填它的 ID；不填 = 全局默认"
    )
    @app_commands.autocomplete(key=owner_keys)
    async def owner_set(interaction: discord.Interaction, key: str, value: str, target: Optional[str] = None) -> None:
        if not await is_owner(interaction):
            return
        found = owner_key(key)
        if found is None:
            await _reply(interaction, f"没有叫「{discord_safe(key)}」的参数：从输入框弹出的列表里选一个。")
            return
        key = Choice(name=SPECS[found].label, value=found)
        if target and target.strip():
            if not target.strip().isdecimal():
                await _reply(interaction, "服务器 ID 应该是一串数字（右键服务器 → 复制服务器 ID）。")
                return
            scope_name, scope_id = "guild", int(target.strip())
            guild = bot.get_guild(scope_id)
            where = f"服务器「{guild.name if guild else scope_id}」"
        else:
            scope_name, scope_id, where = "global", 0, "全局默认"
        spec = SPECS[key.value]
        if value.strip() in RESET_WORDS:
            bot.settings.unset(scope_name, scope_id, key.value)
            settings_changed(bot, key.value, scope_name, scope_id)
            await _reply(interaction, f"{where}：「{spec.label}」已恢复 config.toml 里的默认值。")
            return
        try:
            parsed = bot.settings.set(scope_name, scope_id, key.value, value)
        except ValueError as exc:
            await _reply(interaction, f"没改成：{exc}")
            return
        settings_changed(bot, key.value, scope_name, scope_id)
        await _reply(interaction, f"{where}：「{spec.label}」= {format_value(spec, parsed)}\n（{spec.help}）")

    @owner.command(name="总览", description="今天各服务器的花费和调用情况")
    async def owner_overview(interaction: discord.Interaction) -> None:
        if not await is_owner(interaction):
            return
        now = time.time()
        day = bot.ledger.day(now)
        spend = bot.ledger.all_guilds(now)
        global_view = bot.settings.view(None)
        lines = [f"**{day} 花费总览**（全局上限 ${global_view.global_daily_budget_usd:g}）"]
        for guild in sorted(bot.guilds, key=lambda g: -spend.get(g.id, 0.0)):
            stats = bot.store.day_stats(day, guild_id=guild.id)
            budget = bot.settings.get("daily_budget_usd", guild_id=guild.id)
            hit = stats["hit_rate"]
            group = bot.store.group_of(guild.id)
            lines.append(
                f"{guild.name}（{guild.id}）：${spend.get(guild.id, 0.0):.4f} / ${budget:g}"
                f" · 调用 {stats['calls']} 次" + (f" · 命中率 {hit:.0%}" if hit is not None else "")
                + (f" · {group_name(group)}" if group is not None else "")
            )
        lines.append(f"合计：${sum(spend.values()):.4f}")
        if now < bot.engine.paused_until:
            lines.append("⚠️ DeepSeek 调用暂停中（余额不足或 key 无效，详见日志）")
        await _reply(interaction, "\n".join(lines))

    @owner.command(name="删除人设卡", description="按用户 ID 删掉一个人在所有服务器的人设卡和他的私信记忆（看不到内容）")
    @app_commands.rename(user="用户id")
    @app_commands.describe(user="那个人的用户 ID（开发者模式下右键他的头像 → 复制用户 ID）")
    async def owner_delete_card(interaction: discord.Interaction, user: str) -> None:
        if not await is_owner(interaction):
            return
        if bot.cards is None:
            await _reply(interaction, "人设卡没有启用。")
            return
        if not user.strip().isdecimal():
            await _reply(interaction, "用户 ID 应该是一串数字（开发者模式下右键头像 → 复制用户 ID）。")
            return
        cards_deleted, versions_deleted = bot.cards.delete_user(int(user.strip()))
        memory = getattr(bot, "memory", None)
        notes = memory.dm_clear(int(user.strip())) if memory is not None else 0
        withdraw_dm_memory(bot, int(user.strip()))
        log.info(
            "bot 主人删掉了用户 %s 的人设卡（%d 张，旧版本 %d 个）和私信记忆（%d 条）", user.strip(), cards_deleted, versions_deleted, notes
        )
        if cards_deleted:
            text = f"已删掉这个人的 {cards_deleted} 张人设卡（连同旧版本）。"
        elif versions_deleted:
            text = "这个人现在没有人设卡，已删掉留着的旧版本。"
        else:
            text = "这个人没有人设卡。"
        text += f"\n私信记忆：删掉了 {notes} 条。" if notes else "\n这个人没有私信记忆。"
        await _reply(interaction, text + "\n之后整理聊天时，也不会拿删掉之前的聊天把它们重新写回来。")

    async def server_options(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
        return owner_server_choices(bot, interaction.user.id, current)

    group_text = (
        "（Beta）把几个服务器连成一组：共用长期记忆，开了记忆共享的频道能互相翻记录（人设卡本来就共用）"
        if cards_shared(bot)
        else "（Beta）把几个服务器连成一组：共用人设卡和长期记忆，开了记忆共享的频道能互相翻记录"
    )

    @owner.command(name="服务器组", description=group_text)
    @app_commands.rename(action="操作", server="服务器", other="连到")
    @app_commands.describe(
        action="查看 / 连起来 / 退出",
        server="连起来、退出时：哪个服务器（输入几个字能筛选）",
        other="连起来时：连到哪个服务器（它已经在组里就加进那个组）",
    )
    @app_commands.choices(action=GROUP_ACTIONS)
    @app_commands.autocomplete(server=server_options, other=server_options)
    async def owner_groups(
        interaction: discord.Interaction, action: Choice[str], server: Optional[str] = None, other: Optional[str] = None
    ) -> None:
        if not await is_owner(interaction):
            return
        await group_command(bot, interaction, action.value, server, other)

    # CONTROL_GUILD_ID 可以填几个服务器：每个里面都放一份（是同一个命令，在哪个服务器用都一样）
    bot.tree.add_command(owner, guilds=[discord.Object(id=g) for g in bot.secrets.control_guild_ids])
