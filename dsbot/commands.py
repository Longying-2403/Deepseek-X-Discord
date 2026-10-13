"""斜杠命令。

/ds …       服务器管理员（默认需要“管理服务器”权限）：区域、话量、档位、人设、表情包库、状态。
/dsowner …  只注册在 CONTROL_GUILD_ID 那个服务器里，并且只认 OWNER_ID：预算、思考强度、联网搜索等。
            和 /ds 一样默认只给有“管理服务器”权限的人看（bot 放在朋友的服务器、你是那里的管理也看得到）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import PurePath
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.app_commands import Choice

from .config import ADMIN_KEYS, OWNER_KEYS, PRESETS, RESET_WORDS, SPECS, format_value
from .convert import safe_category_id
from .prompts import DEFAULT_PERSONA, PERSONA_MAX_CHARS

if TYPE_CHECKING:
    from .discord_app import DSBot

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
LINK_MODES = {
    "all": "读链接：开（推文走 FxTwitter，网页走 Tavily）",
    "x": "读链接：只读推文（没填 TAVILY_API_KEY，别的网站只看 Discord 的预览）",
    "off": "读链接：关",
}
NO_CATEGORY = "这个频道不在任何类别里，范围请选“本频道”或“全服务器”。"
# 区域、主动开关、人设、记忆共享有专门的命令；其余管理员参数走 /ds 设置
ADMIN_SET_KEYS = [k for k in ADMIN_KEYS if k not in ("enabled", "proactive", "persona", "share_memory")]
OWNER_SET_KEYS = OWNER_KEYS + [k for k in ADMIN_KEYS if k != "persona"]


async def _reply(interaction: discord.Interaction, text: str) -> None:
    text = text if len(text) <= 1900 else text[:1900] + "…"
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


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
    names = "、".join(f"<#{cid}>" for cid, _ in sources[:15]) or "暂时没有（别的频道开了共享、并且跟它聊过之后才会出现）"
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
    if st["memes_on"] and st["media_blocked_for"] > 0:
        minutes = max(1, round(st["media_blocked_for"] / 60))
        lines.append(f"⚠️ 这里最近连着发不出图（多半是 bot 没有“附加文件”权限），约 {minutes} 分钟内先不发图，之后自动再试")
    if st["last_decision"]:
        lines.append(f"上次：{st['last_decision']}")
    lines.append(
        f"今天本频道：调用 {channel_stats['calls']} 次（说话 {outcomes.get('reply', 0)}、表情 {outcomes.get('react', 0)}、"
        f"沉默 {outcomes.get('pass', 0)}、出错 {outcomes.get('error', 0)}）"
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
        target = _target(interaction, scope)
        if target is None:
            await _reply(interaction, NO_CATEGORY)
            return
        scope_name, scope_id, where = target
        spec = SPECS[key.value]
        if value.strip() in RESET_WORDS:
            bot.settings.unset(scope_name, scope_id, key.value)
            await _reply(interaction, f"{where}：「{spec.label}」已恢复默认。")
            return
        try:
            parsed = bot.settings.set(scope_name, scope_id, key.value, value)
        except ValueError as exc:
            await _reply(interaction, f"没改成：{exc}")
            return
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

    @bot.tree.error
    async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.error("斜杠命令出错：%s", error, exc_info=error)
        try:
            await _reply(interaction, f"出错了：{error}")
        except discord.HTTPException:
            pass

    if not bot.secrets.control_guild_id:
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

    @owner.command(name="设置", description="调整参数：全局默认，或者指定某个服务器")
    @app_commands.rename(key="参数", value="值", target="服务器id")
    @app_commands.describe(
        key="要改哪个参数", value="新的值；填“默认”恢复", target="只改某个服务器就填它的 ID；不填 = 全局默认"
    )
    @app_commands.choices(key=[Choice(name=SPECS[k].label, value=k) for k in OWNER_SET_KEYS])
    async def owner_set(
        interaction: discord.Interaction, key: Choice[str], value: str, target: Optional[str] = None
    ) -> None:
        if not await is_owner(interaction):
            return
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
            await _reply(interaction, f"{where}：「{spec.label}」已恢复 config.toml 里的默认值。")
            return
        try:
            parsed = bot.settings.set(scope_name, scope_id, key.value, value)
        except ValueError as exc:
            await _reply(interaction, f"没改成：{exc}")
            return
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
            lines.append(
                f"{guild.name}（{guild.id}）：${spend.get(guild.id, 0.0):.4f} / ${budget:g}"
                f" · 调用 {stats['calls']} 次" + (f" · 命中率 {hit:.0%}" if hit is not None else "")
            )
        lines.append(f"合计：${sum(spend.values()):.4f}")
        if now < bot.engine.paused_until:
            lines.append("⚠️ DeepSeek 调用暂停中（余额不足或 key 无效，详见日志）")
        await _reply(interaction, "\n".join(lines))

    bot.tree.add_command(owner, guild=discord.Object(id=bot.secrets.control_guild_id))
