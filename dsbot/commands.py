"""Slash commands.

/ds …       Server admins (need the "Manage Server" permission by default): areas, how much it talks, presets, persona, meme library, status.
/dsowner …  Registered only in the CONTROL_GUILD_ID server, and only accepts OWNER_ID: budget, thinking level, web search, etc.
            Like /ds, by default only people with "Manage Server" can see it (if the bot is in a friend's server where you're a moderator, you'll see it too).
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
    Choice(name="This channel", value="channel"),
    Choice(name="This category", value="category"),
    Choice(name="Whole server", value="guild"),
]
AREA_ACTIONS = [
    Choice(name="Allow", value="on"),
    Choice(name="Block", value="off"),
    Choice(name="Inherit", value="inherit"),
]
SHARE_ACTIONS = [
    Choice(name="On", value="on"),
    Choice(name="Off", value="off"),
    Choice(name="Inherit", value="inherit"),
]
MEME_ACTIONS = [
    Choice(name="Upload", value="add"),
    Choice(name="Remove", value="remove"),
    Choice(name="List", value="list"),
]
LINK_MODES = {
    "all": "Reading links: on (posts on X via FxTwitter, web pages via Tavily)",
    "x": "Reading links: posts on X only (TAVILY_API_KEY isn't set, so other sites only get Discord's preview)",
    "off": "Reading links: off",
}
NO_CATEGORY = 'This channel isn\'t in any category, so pick "This channel" or "Whole server" as the scope.'
SCOPE_HELP = "This channel, this category, or the whole server (default: this channel)"
SCOPE_DEFAULT_HELP = "This channel, this category, or the server-wide default (default: this channel)"
# Areas, proactive on/off, persona and memory sharing have their own commands; the other admin settings go through /ds set
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
    """The category the current channel is in (for a thread, its parent channel's category)."""
    channel = interaction.channel
    try:
        category = getattr(channel, "category", None)
    except Exception:  # discord.py raises when a thread's parent channel isn't cached
        category = None
    if category is not None:
        return category.id, category.name
    return safe_category_id(channel), None


def _ids(interaction: discord.Interaction) -> tuple[int, int, int | None, int | None]:
    return interaction.guild_id, interaction.channel_id, _parent_id(interaction), _category(interaction)[0]


def _target(
    interaction: discord.Interaction, scope: Optional[Choice[str]], default: str = "channel"
) -> tuple[str, int, str] | None:
    """Scope option → (storage scope, id, name to show people). Returns None if "This category" was picked but the channel isn't in one."""
    value = scope.value if scope is not None else default
    if value == "guild":
        return "guild", interaction.guild_id, "Whole server"
    if value == "category":
        category_id, name = _category(interaction)
        if category_id is None:
            return None
        return "category", category_id, f'Category "{name or category_id}"'
    return "channel", interaction.channel_id, f"<#{interaction.channel_id}>"


def _preset_text(values: dict[str, int]) -> str:
    gap = values["min_speak_gap"]
    gap_text = f"{gap // 60} min" if gap >= 60 else f"{gap} s"
    return f"+1 quota every {values['refill_turns']} turns, stores up to {values['quota_capacity']}, at least {gap_text} apart"


def _n(count: int, word: str, plural: str | None = None) -> str:
    """'1 call', '3 calls', '2 replies'."""
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


class PersonaModal(discord.ui.Modal):
    def __init__(self, bot: "DSBot", scope: str, scope_id: int, where: str, current: str, inherited: str):
        super().__init__(title="Edit persona", timeout=900)
        self.bot = bot
        self.scope = scope
        self.scope_id = scope_id
        self.where = where
        self.inherited = inherited  # the persona used when this scope has no setting of its own
        self.text = discord.ui.TextInput(
            style=discord.TextStyle.long,
            default=current[:PERSONA_MAX_CHARS] or None,
            required=False,
            max_length=PERSONA_MAX_CHARS,
            placeholder="Leave empty to use the persona from the level above",
        )
        self.add_item(
            discord.ui.Label(
                text="Persona",
                description="Who it is, its personality, how it relates to the group. Clear it to inherit from the level above.",
                component=self.text,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.text.value.strip()
        old = self.bot.settings.overrides(self.scope, self.scope_id).get("persona")
        if not value or value == self.inherited.strip():
            # unchanged (same as the level above): don't store it separately, so later changes to the level above apply here too
            self.bot.settings.unset(self.scope, self.scope_id, "persona")
            new = None
            done = "back to the default persona" if self.scope == "guild" else "now uses the persona from the level above"
        else:
            self.bot.settings.set(self.scope, self.scope_id, "persona", value)
            new = value
            done = "persona updated"
        if new != old:
            # recorded in the persona history, so dsowner saying "change it back" in chat can undo it
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
            f"{self.where}: {done}. It takes effect from the next round.\n"
            "If it's a big change, `/ds clear-memory` keeps it from being influenced by how it talked before.",
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Saving the persona failed", exc_info=error)
        await _reply(interaction, f"Couldn't save it: {error}")


def share_text(bot: "DSBot", interaction: discord.Interaction) -> str:
    """This channel's memory sharing: whether it can see other channels, whether its own content is shared out, and which channels it's connected with now."""
    guild_id, channel_id, parent_id, category_id = _ids(interaction)
    view = bot.settings.view(guild_id, channel_id, parent_id, category_id)
    if not view.share_memory:
        return f"Memory sharing: off (from {view.source('share_memory')})"
    place = bot.transport.locate(channel_id)
    shares_out = place is not None and bot.engine.shares_out(channel_id, place)
    if shares_out:
        out_text = "this channel's content is shared with the other sharing channels too"
    elif place is not None and not place.public:
        out_text = "🔒 this is a private channel, so its content isn't shared out (to share it, run `/ds share-memory action:On` here)"
    else:
        out_text = "this channel's content isn't shared out"
    sources = bot.engine.shared_sources(guild_id, channel_id)
    names = ", ".join(f"<#{cid}>" for cid, _ in sources[:15]) or "none yet (channels show up once they have sharing on and have chatted with it)"
    return f"Memory sharing: on (from {view.source('share_memory')}) · {out_text}\nChannels it can look through: {names}"


def status_text(bot: "DSBot", interaction: discord.Interaction) -> str:
    guild_id, channel_id, parent_id, category_id = _ids(interaction)
    view = bot.settings.view(guild_id, channel_id, parent_id, category_id)
    st = bot.engine.status(guild_id, channel_id, parent_id, category_id)
    channel_stats = bot.store.day_stats(st["day"], channel_id=channel_id)
    guild_stats = bot.store.day_stats(st["day"], guild_id=guild_id)
    outcomes = channel_stats["outcomes"]
    preset = next(
        (name for name, values in PRESETS.items() if all(getattr(view, k) == v for k, v in values.items())),
        "custom",
    )
    lines = [f"**Status of <#{channel_id}>**"]
    if not view.enabled:
        lines.append(f"🚫 This channel isn't in an allowed area (from {view.source('enabled')}), so it won't read or reply to messages here")
    lines += [
        f"Proactive chat: {'on' if view.proactive else 'off'} (from {view.source('proactive')}) · preset: {preset}",
        f"Proactive quota: {st['tokens']:.1f} / {view.quota_capacity} (+1 every {view.refill_turns} turns, "
        f"{st['turns_since_refill']} so far)",
        f"Gap between messages ≥ {view.min_speak_gap}s · lull {view.quiet_seconds}s · max wait {view.max_wait_seconds}s"
        f" · mentions per person per minute ≤ {view.mention_rate_limit}",
        f"Aliases: {view.aliases or '(none)'} · persona: {format_value(SPECS['persona'], view.persona)}",
        f"Pending messages: {st['pending']} · last spoke: {_ago(st['last_speak_ago'])}",
    ]
    lines.append(share_text(bot, interaction))
    if st["can_search"]:
        lines.append(f"Web search: on · searches on this server today: {st['searches']} / {view.daily_search_limit}")
    else:
        lines.append("Web search: off")
    link_line = LINK_MODES.get(st["links_mode"], LINK_MODES["off"])
    if st["links_mode"] != "off":
        link_line += f" · links opened on this server today: {st['links_opened']}"
    lines.append(link_line)
    if st["memes_on"]:
        own, shared = st["meme_counts"]
        if st["can_find_images"]:
            found = f"image searches on this server today: {st['images_found']} / {view.daily_image_limit}"
        else:
            found = "finding images online: off (TAVILY_API_KEY isn't set, or the daily image search limit is 0)"
        lines.append(f"Sending pictures: on · meme library: {own + shared} ({own} from this server, {shared} shared) · {found}")
    else:
        lines.append(f"Sending pictures: off (from {view.source('images')})")
    if st["memes_on"] and st["media_blocked_for"] > 0:
        minutes = max(1, round(st["media_blocked_for"] / 60))
        lines.append(
            '⚠️ Pictures have kept failing to send here lately (most likely the bot lacks the "Attach Files" permission), '
            f"so no pictures for about {minutes} min; it will try again automatically after that"
        )
    if st["last_decision"]:
        lines.append(f"Last round: {st['last_decision']}")
    lines.append(
        f"Today in this channel: {_n(channel_stats['calls'], 'call')} ({_n(outcomes.get('reply', 0), 'reply', 'replies')}, "
        f"{_n(outcomes.get('react', 0), 'reaction')}, {outcomes.get('pass', 0)} silent, {_n(outcomes.get('error', 0), 'error')})"
    )
    hit = guild_stats["hit_rate"]
    lines.append(
        f"Spent on this server today: ${st['guild_spent']:.4f} / ${view.daily_budget_usd:g}"
        + (f" · cache hit rate {hit:.0%}" if hit is not None else "")
    )
    if st["guild_spent"] >= view.daily_budget_usd:
        lines.append("⚠️ Over today's budget: it only answers mentions and doesn't speak up on its own")
    if st["paused"]:
        lines.append("⚠️ DeepSeek calls are paused (insufficient balance or invalid key; see the logs)")
    if st["send_blocked"]:
        lines.append(
            "⚠️ It couldn't send messages here for the last few rounds (check the bot's permission to send messages), "
            "so proactive chat is paused for 30 minutes"
        )
    return "\n".join(lines)


def meme_list_text(bot: "DSBot", guild_id: int) -> str:
    entries = bot.memes.entries(guild_id)
    own = sorted(name for name, meme in entries.items() if not meme.shared)
    shared = sorted(name for name, meme in entries.items() if meme.shared)
    lines = [f"**Meme library** ({len(own)} from this server · {len(shared)} shared)"]
    lines.append("This server's: " + (", ".join(own) if own else "none yet (add some with `/ds meme action:Upload`)"))
    lines.append("Shared: " + (", ".join(shared) if shared else f"none (the bot's owner can put pictures in `{bot.cfg.bot.meme_dir}/` on the NAS)"))
    if not bot.settings.get("images", guild_id=guild_id):
        lines.append("⚠️ Sending pictures is turned off on this server: turn it on with `/ds set setting:Send pictures value:on scope:Whole server`")
    return "\n".join(lines)


def area_list_text(bot: "DSBot", guild: discord.Guild) -> str:
    def mark(value: bool) -> str:
        return "✅ allowed" if value else "🚫 blocked"

    guild_enabled = bot.settings.get("enabled", guild_id=guild.id)
    source = "set explicitly" if "enabled" in bot.settings.overrides("guild", guild.id) else "default"
    lines = ["**Where it responds**", f"Whole server: {mark(guild_enabled)} ({source})"]
    rows = []
    for category_id, value in bot.settings.scoped("category", "enabled").items():
        category = guild.get_channel(category_id)
        if category is not None:
            rows.append((category.position, f'Category "{category.name}": {mark(value)}'))
    for channel_id, value in bot.settings.scoped("channel", "enabled").items():
        if guild.get_channel_or_thread(channel_id) is not None:
            rows.append((10_000, f"<#{channel_id}>: {mark(value)}"))
    lines += [text for _, text in sorted(rows)] or ["(no category or channel has a setting of its own)"]

    proactive = []
    for scope in ("category", "channel"):
        for scope_id, value in bot.settings.scoped(scope, "proactive").items():
            obj = guild.get_channel(scope_id) if scope == "category" else guild.get_channel_or_thread(scope_id)
            if value and obj is not None:
                proactive.append(f'category "{obj.name}"' if scope == "category" else f"<#{scope_id}>")
    if bot.settings.get("proactive", guild_id=guild.id):
        lines.append("Proactive chat: on by default for the whole server")
    elif proactive:
        lines.append("Proactive chat is on in: " + ", ".join(proactive))
    lines.append("(Threads follow their parent channel; a channel's setting beats its category's, and a category's beats the server's)")
    return "\n".join(lines)


def register_commands(bot: "DSBot") -> None:
    admin = app_commands.Group(
        name="ds",
        description="DeepSeek Buddy: settings for this server",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @admin.command(name="area", description="Limit which channels or categories it responds in (it ignores messages everywhere else)")
    @app_commands.describe(action="Allow / Block / Inherit (remove the setting at this level)", scope=SCOPE_HELP)
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
        state = "responds" if view.enabled else "doesn't respond"
        lines = [
            f'{where}: set to "{action.name}".',
            f"Right now it {state} in this channel (from {view.source('enabled')})",
        ]
        if scope_name == "guild" and action.value == "off":
            lines.append(
                "It's in allowlist mode now: run `/ds area action:Allow` in the channels where you want it "
                '(or pick "This category" to open up a whole category).'
            )
        await _reply(interaction, "\n".join(lines))

    @admin.command(name="share-memory", description="Share memory between channels that have sharing on (private channels must opt in themselves)")
    @app_commands.describe(action="On / Off / Inherit (remove the setting at this level)", scope=SCOPE_HELP)
    @app_commands.choices(action=SHARE_ACTIONS, scope=SCOPE_CHOICES)
    async def share_cmd(interaction: discord.Interaction, action: Choice[str], scope: Optional[Choice[str]] = None) -> None:
        target = _target(interaction, scope)
        if target is None:
            await _reply(interaction, NO_CATEGORY)
            return
        scope_name, scope_id, where = target
        if action.value == "inherit":
            bot.settings.unset(scope_name, scope_id, "share_memory")
            done = "now follows the level above"
        else:
            bot.settings.set(scope_name, scope_id, "share_memory", action.value)
            done = "turned on" if action.value == "on" else "turned off"
        lines = [f"{where}: memory sharing {done}.", share_text(bot, interaction)]
        if action.value == "on" and scope_name != "channel":
            lines.append("(Private channels in this scope still don't share their content out unless it's turned on in that channel itself.)")
        await _reply(interaction, "\n".join(lines))

    @admin.command(name="meme", description="Manage this server's meme library: upload, remove, list (it picks which one to send by name)")
    @app_commands.describe(
        action="Upload / Remove / List",
        name='Name it by what it means, e.g. "facepalm-cat" (it picks by name); empty on upload = the file name',
        image="The picture to upload: PNG, JPG, GIF or WebP, up to 8 MB",
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
                await _reply(interaction, 'Which one? Put the meme\'s name in "name" (`/ds meme action:List` shows them all).')
                return
            await interaction.response.defer(ephemeral=True, thinking=True)  # may have to wait for an upload in progress to finish writing
            result = await asyncio.to_thread(library.remove, guild_id, name)
            if result == "removed":
                await _reply(interaction, f'Removed the meme "{name.strip()}".')
            elif result == "shared":
                await _reply(interaction, f'"{name.strip()}" is in the shared library; delete it from the `{bot.cfg.bot.meme_dir}/` folder on the NAS.')
            else:
                await _reply(interaction, f'This server has no meme called "{name.strip()}".')
            return
        if image is None:
            await _reply(interaction, 'To upload, attach a picture in "image".')
            return
        if image.size > library.max_bytes:
            await _reply(interaction, f"The picture is too big (the limit is {library.max_bytes // 1_000_000} MB).")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)  # downloading the picture takes a moment
        try:
            data = await image.read()
        except discord.HTTPException as exc:
            await _reply(interaction, f"Downloading the picture failed: {exc}")
            return
        try:
            meme = await asyncio.to_thread(library.add, guild_id, name or PurePath(image.filename).stem, data)
        except ValueError as exc:
            await _reply(interaction, f"Couldn't save it: {exc}")
            return
        except OSError as exc:
            log.exception("Saving the meme failed")
            await _reply(interaction, f"Couldn't save it (error writing the file): {exc}")
            return
        text = f'Added to this server\'s meme library: "{meme.name}". It decides when to send a meme by its name, so a name that clearly says what it means works best.'
        if not bot.settings.view(*_ids(interaction)).images:
            text += "\n⚠️ But sending pictures is turned off here, so it won't send it: `/ds set setting:Send pictures value:on`"
        await _reply(interaction, text)

    @admin.command(name="area-list", description="See where on this server it responds")
    async def area_list_cmd(interaction: discord.Interaction) -> None:
        await _reply(interaction, area_list_text(bot, interaction.guild))

    @admin.command(name="proactive", description="Turn proactive chat on or off (when nobody calls it, it decides by itself whether to speak)")
    @app_commands.describe(state="On or off", scope=SCOPE_DEFAULT_HELP)
    @app_commands.choices(state=[Choice(name="On", value="on"), Choice(name="Off", value="off")], scope=SCOPE_CHOICES)
    async def proactive_cmd(
        interaction: discord.Interaction, state: Choice[str], scope: Optional[Choice[str]] = None
    ) -> None:
        target = _target(interaction, scope)
        if target is None:
            await _reply(interaction, NO_CATEGORY)
            return
        scope_name, scope_id, where = target
        bot.settings.set(scope_name, scope_id, "proactive", state.value)
        text = f"{where}: proactive chat turned {state.value}."
        if state.value == "on" and not bot.settings.view(*_ids(interaction)).enabled:
            text += "\n⚠️ But this channel isn't in an allowed area, so it won't speak. Run `/ds area action:Allow` first."
        await _reply(interaction, text)

    @admin.command(name="preset", description="Apply a talkativeness preset in one go: Quiet / Normal / Chatty")
    @app_commands.describe(preset="Pick a preset", scope=SCOPE_DEFAULT_HELP)
    @app_commands.choices(
        preset=[Choice(name=f"{name}: {_preset_text(values)}", value=name) for name, values in PRESETS.items()],
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
        await _reply(interaction, f'{where}: switched to the "{preset.value}" preset ({_preset_text(PRESETS[preset.value])}).')

    @admin.command(name="set", description='Adjust a single setting (enter "default" as the value to reset it)')
    @app_commands.rename(key="setting")
    @app_commands.describe(
        key="Which setting to change", value='New value; enter "default" to go back to the level above', scope=SCOPE_DEFAULT_HELP
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
        if value.strip().lower() in RESET_WORDS:
            bot.settings.unset(scope_name, scope_id, key.value)
            await _reply(interaction, f'{where}: "{spec.label}" now follows the level above.')
            return
        try:
            parsed = bot.settings.set(scope_name, scope_id, key.value, value)
        except ValueError as exc:
            await _reply(interaction, f"Not changed: {exc}")
            return
        await _reply(interaction, f'{where}: "{spec.label}" = {format_value(spec, parsed)}\n({spec.help})')

    @admin.command(name="persona", description="Edit the persona (opens an editor)")
    @app_commands.describe(scope="Whole server, this category, or only this channel (default: whole server)")
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def persona_cmd(interaction: discord.Interaction, scope: Optional[Choice[str]] = None) -> None:
        target = _target(interaction, scope, default="guild")
        if target is None:
            await _reply(interaction, NO_CATEGORY)
            return
        scope_name, scope_id, where = target
        guild_id, _, parent_id, category_id = _ids(interaction)
        # "the level above": server → global; category → server; channel → the thread's parent channel → category → server
        if scope_name == "guild":
            inherited = bot.settings.get("persona")
        elif scope_name == "category":
            inherited = bot.settings.get("persona", guild_id=guild_id)
        else:
            inherited = bot.settings.get("persona", guild_id=guild_id, channel_id=parent_id, category_id=category_id)
        inherited = inherited or DEFAULT_PERSONA
        current = bot.settings.overrides(scope_name, scope_id).get("persona") or inherited
        await interaction.response.send_modal(PersonaModal(bot, scope_name, scope_id, where, current, inherited))

    @admin.command(name="status", description="Show this channel's settings, quota and today's spending")
    async def status_cmd(interaction: discord.Interaction) -> None:
        await _reply(interaction, status_text(bot, interaction))

    @admin.command(name="clear-memory", description="Clear this channel's chat history and summary so it starts fresh")
    async def reset_cmd(interaction: discord.Interaction) -> None:
        bot.engine.reset_channel(interaction.channel_id)
        await _reply(interaction, "Cleared this channel's chat history and summary.")

    bot.tree.add_command(admin)

    @bot.tree.error
    async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.error("Slash command error: %s", error, exc_info=error)
        try:
            await _reply(interaction, f"Something went wrong: {error}")
        except discord.HTTPException:
            pass

    if not bot.secrets.control_guild_id:
        log.info("CONTROL_GUILD_ID isn't set, so the owner commands aren't registered; change the budget and such in config.toml")
        return

    # Who can use it is decided by is_owner; default_permissions only hides it from regular members. It's "Manage Server" rather than
    # "Administrator": when the bot is in a friend's server you're probably a moderator there (able to use /ds), not necessarily an Administrator
    owner = app_commands.Group(
        name="dsowner",
        description="DeepSeek Buddy: owner only",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    async def is_owner(interaction: discord.Interaction) -> bool:
        if interaction.user.id != bot.secrets.owner_id:
            await _reply(interaction, "Only the bot's owner can use this command.")
            return False
        return True

    @owner.command(name="set", description="Adjust a setting: the global default, or for one specific server")
    @app_commands.rename(key="setting", target="server-id")
    @app_commands.describe(
        key="Which setting to change", value='New value; enter "default" to reset it', target="To change only one server, enter its ID; leave empty for the global default"
    )
    @app_commands.choices(key=[Choice(name=SPECS[k].label, value=k) for k in OWNER_SET_KEYS])
    async def owner_set(
        interaction: discord.Interaction, key: Choice[str], value: str, target: Optional[str] = None
    ) -> None:
        if not await is_owner(interaction):
            return
        if target and target.strip():
            if not target.strip().isdecimal():
                await _reply(interaction, "A server ID should be a string of digits (right-click the server → Copy Server ID).")
                return
            scope_name, scope_id = "guild", int(target.strip())
            guild = bot.get_guild(scope_id)
            where = f'Server "{guild.name if guild else scope_id}"'
        else:
            scope_name, scope_id, where = "global", 0, "Global setting"
        spec = SPECS[key.value]
        if value.strip().lower() in RESET_WORDS:
            bot.settings.unset(scope_name, scope_id, key.value)
            back_to = "is back to the value in config.toml" if scope_name == "global" else "now follows the global setting"
            await _reply(interaction, f'{where}: "{spec.label}" {back_to}.')
            return
        try:
            parsed = bot.settings.set(scope_name, scope_id, key.value, value)
        except ValueError as exc:
            await _reply(interaction, f"Not changed: {exc}")
            return
        await _reply(interaction, f'{where}: "{spec.label}" = {format_value(spec, parsed)}\n({spec.help})')

    @owner.command(name="overview", description="Today's spending and calls for each server")
    async def owner_overview(interaction: discord.Interaction) -> None:
        if not await is_owner(interaction):
            return
        now = time.time()
        day = bot.ledger.day(now)
        spend = bot.ledger.all_guilds(now)
        global_view = bot.settings.view(None)
        lines = [f"**Spending overview for {day}** (global cap ${global_view.global_daily_budget_usd:g})"]
        for guild in sorted(bot.guilds, key=lambda g: -spend.get(g.id, 0.0)):
            stats = bot.store.day_stats(day, guild_id=guild.id)
            budget = bot.settings.get("daily_budget_usd", guild_id=guild.id)
            hit = stats["hit_rate"]
            lines.append(
                f"{guild.name} ({guild.id}): ${spend.get(guild.id, 0.0):.4f} / ${budget:g}"
                f" · {_n(stats['calls'], 'call')}" + (f" · hit rate {hit:.0%}" if hit is not None else "")
            )
        lines.append(f"Total: ${sum(spend.values()):.4f}")
        if now < bot.engine.paused_until:
            lines.append("⚠️ DeepSeek calls are paused (insufficient balance or invalid key; see the logs)")
        await _reply(interaction, "\n".join(lines))

    bot.tree.add_command(owner, guild=discord.Object(id=bot.secrets.control_guild_id))
