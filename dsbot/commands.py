"""斜杠命令。

/ds …       服务器管理员（默认需要“管理服务器”权限）：话量、档位、人设、状态。
/dsowner …  只注册在 CONTROL_GUILD_ID 那个私人服务器里，并且只认 OWNER_ID：预算、思考强度等。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.app_commands import Choice

from .config import ADMIN_KEYS, OWNER_KEYS, PRESETS, RESET_WORDS, SPECS, format_value
from .prompts import DEFAULT_PERSONA

if TYPE_CHECKING:
    from .discord_app import DSBot

log = logging.getLogger(__name__)

SCOPE_CHOICES = [Choice(name="本频道", value="channel"), Choice(name="全服务器", value="guild")]
# 主动开关、人设有专门的命令；其余管理员参数走 /ds 设置
ADMIN_SET_KEYS = [k for k in ADMIN_KEYS if k not in ("proactive", "persona")]
OWNER_SET_KEYS = OWNER_KEYS + [k for k in ADMIN_KEYS if k != "persona"]


async def _reply(interaction: discord.Interaction, text: str) -> None:
    text = text if len(text) <= 1900 else text[:1900] + "…"
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def _where(interaction: discord.Interaction, scope: Optional[Choice[str]], default: str = "channel"):
    value = scope.value if scope is not None else default
    if value == "guild":
        return "guild", interaction.guild_id, "全服务器（默认）"
    return "channel", interaction.channel_id, f"<#{interaction.channel_id}>"


def _parent_id(interaction: discord.Interaction) -> int | None:
    channel = interaction.channel
    return channel.parent_id if isinstance(channel, discord.Thread) else None


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
            default=current[:4000] or None,
            required=False,
            max_length=4000,
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
        if not value or value == self.inherited.strip():
            # 没改（和上一级一样）就不单独存，以后上一级改了这里也会跟着变
            self.bot.settings.unset(self.scope, self.scope_id, "persona")
            done = "已改为沿用上一级的人设" if self.scope == "channel" else "已恢复默认人设"
        else:
            self.bot.settings.set(self.scope, self.scope_id, "persona", value)
            done = "人设已更新"
        await _reply(
            interaction,
            f"{self.where}：{done}，下一轮对话生效。\n改动比较大的话，可以用 `/ds 清空记忆` 让它不受之前说话风格的影响。",
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("保存人设失败", exc_info=error)
        await _reply(interaction, f"保存失败：{error}")


def status_text(bot: "DSBot", interaction: discord.Interaction) -> str:
    guild_id, channel_id, parent_id = interaction.guild_id, interaction.channel_id, _parent_id(interaction)
    view = bot.settings.view(guild_id, channel_id, parent_id)
    st = bot.engine.status(guild_id, channel_id, parent_id)
    channel_stats = bot.store.day_stats(st["day"], channel_id=channel_id)
    guild_stats = bot.store.day_stats(st["day"], guild_id=guild_id)
    outcomes = channel_stats["outcomes"]
    preset = next(
        (name for name, values in PRESETS.items() if all(getattr(view, k) == v for k, v in values.items())),
        "自定义",
    )
    lines = [
        f"**<#{channel_id}> 的状态**",
        f"主动发言：{'开' if view.proactive else '关'}（来自{view.source('proactive')}）· 档位：{preset}",
        f"主动额度：{st['tokens']:.1f} / {view.quota_capacity}（每 {view.refill_turns} 轮恢复 1 次，"
        f"已攒 {st['turns_since_refill']} 轮）",
        f"发言间隔 ≥ {view.min_speak_gap}s · 冷场 {view.quiet_seconds}s · 最长等待 {view.max_wait_seconds}s"
        f" · 每人每分钟点名 ≤ {view.mention_rate_limit} 次",
        f"别名：{view.aliases or '（无）'} · 人设：{format_value(SPECS['persona'], view.persona)}",
        f"待处理消息 {st['pending']} 条 · 上次发言：{_ago(st['last_speak_ago'])}",
    ]
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


def register_commands(bot: "DSBot") -> None:
    admin = app_commands.Group(
        name="ds",
        description="DeepSeek 群友：本服务器的设置",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @admin.command(name="主动", description="开关主动发言（没被叫到时，自己判断要不要说话）")
    @app_commands.rename(state="开关", scope="范围")
    @app_commands.describe(state="开还是关", scope="只改本频道，还是作为全服务器默认（不选 = 本频道）")
    @app_commands.choices(state=[Choice(name="开", value="on"), Choice(name="关", value="off")], scope=SCOPE_CHOICES)
    async def proactive_cmd(
        interaction: discord.Interaction, state: Choice[str], scope: Optional[Choice[str]] = None
    ) -> None:
        scope_name, scope_id, where = _where(interaction, scope)
        bot.settings.set(scope_name, scope_id, "proactive", state.value)
        await _reply(interaction, f"{where}：主动发言已{'打开' if state.value == 'on' else '关闭'}。")

    @admin.command(name="档位", description="一键套用发言频率：安静 / 普通 / 话痨")
    @app_commands.rename(preset="档位", scope="范围")
    @app_commands.describe(preset="选一个档位", scope="只改本频道，还是作为全服务器默认（不选 = 本频道）")
    @app_commands.choices(
        preset=[Choice(name=f"{name}：{_preset_text(values)}", value=name) for name, values in PRESETS.items()],
        scope=SCOPE_CHOICES,
    )
    async def preset_cmd(
        interaction: discord.Interaction, preset: Choice[str], scope: Optional[Choice[str]] = None
    ) -> None:
        scope_name, scope_id, where = _where(interaction, scope)
        for key, value in PRESETS[preset.value].items():
            bot.settings.set(scope_name, scope_id, key, value)
        await _reply(interaction, f"{where}：已切到「{preset.value}」档（{_preset_text(PRESETS[preset.value])}）。")

    @admin.command(name="设置", description="调整单项参数（值填“默认”恢复）")
    @app_commands.rename(key="参数", value="值", scope="范围")
    @app_commands.describe(
        key="要改哪个参数", value="新的值；填“默认”恢复成上一级的设置", scope="只改本频道，还是作为全服务器默认（不选 = 本频道）"
    )
    @app_commands.choices(key=[Choice(name=SPECS[k].label, value=k) for k in ADMIN_SET_KEYS], scope=SCOPE_CHOICES)
    async def set_cmd(
        interaction: discord.Interaction, key: Choice[str], value: str, scope: Optional[Choice[str]] = None
    ) -> None:
        scope_name, scope_id, where = _where(interaction, scope)
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
    @app_commands.describe(scope="改全服务器的人设，还是只改本频道（不选 = 全服务器）")
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def persona_cmd(interaction: discord.Interaction, scope: Optional[Choice[str]] = None) -> None:
        scope_name, scope_id, where = _where(interaction, scope, default="guild")
        # “上一级”：服务器的上一级是全局默认；频道的上一级是帖子所在频道 → 服务器 → 全局
        if scope_name == "guild":
            inherited = bot.settings.get("persona")
        else:
            inherited = bot.settings.get("persona", guild_id=interaction.guild_id, channel_id=_parent_id(interaction))
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

    owner = app_commands.Group(
        name="dsowner",
        description="DeepSeek 群友：主人专用",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
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
            if not target.strip().isdigit():
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
