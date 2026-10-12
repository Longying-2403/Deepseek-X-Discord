"""Discord layer: turns discord.py events into the engine's data structures and sends the engine's actions back to Discord."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections import deque
from typing import Any

import discord
from discord import app_commands

from .config import AppConfig, Secrets
from .convert import convert_message, parse_aliases, safe_category_id
from .engine import Engine
from .ledger import Ledger
from .llm import DeepSeekClient
from .models import ChannelInfo, ChannelPlace, IncomingMessage
from .search import WebSearch
from .settings import Settings
from .store import Store

log = logging.getLogger(__name__)

CHAT_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)

# permissions needed when inviting the bot
BOT_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    send_messages_in_threads=True,
    read_message_history=True,
    add_reactions=True,
    embed_links=True,
)


class _SafeTyping:
    """If "typing…" fails (e.g. missing permission), ignore it silently so sending messages afterwards isn't affected."""

    def __init__(self, channel: Any):
        self._cm = channel.typing() if channel is not None and hasattr(channel, "typing") else None
        self._entered = False

    async def __aenter__(self) -> None:
        if self._cm is None:
            return
        try:
            await self._cm.__aenter__()
            self._entered = True
        except Exception as exc:
            log.debug('Showing "typing…" failed: %r', exc)

    async def __aexit__(self, *exc: Any) -> bool:
        if self._entered:
            try:
                await self._cm.__aexit__(*exc)
            except Exception:
                pass
        return False


class DiscordTransport:
    """Engine → Discord."""

    def __init__(self, client: "DSBot"):
        self.client = client
        self._sent: deque[int] = deque(maxlen=2000)
        self._sent_set: set[int] = set()

    def remember_sent(self, message_id: int) -> None:
        if len(self._sent) == self._sent.maxlen:
            self._sent_set.discard(self._sent[0])
        self._sent.append(message_id)
        self._sent_set.add(message_id)

    def is_ours(self, message_id: int) -> bool:
        return message_id in self._sent_set

    def channel_info(self, channel_id: int) -> ChannelInfo:
        channel = self.client.get_channel(channel_id)
        guild = getattr(channel, "guild", None)
        me = guild.me if guild is not None else None
        if me is not None:
            bot_name = me.display_name
        elif self.client.user is not None:
            bot_name = self.client.user.display_name
        else:
            bot_name = "bot"
        return ChannelInfo(
            guild_name=guild.name if guild is not None else "unknown server",
            channel_name=getattr(channel, "name", None) or str(channel_id),
            bot_name=bot_name,
        )

    def locate(self, channel_id: int) -> ChannelPlace | None:
        """Which server and category the channel is in, and whether everyone can see it (for cross-channel memory). Returns None if it isn't cached."""
        channel = self.client.get_channel(channel_id)
        guild = getattr(channel, "guild", None)
        if channel is None or guild is None:
            return None
        is_thread = isinstance(channel, discord.Thread)
        private_thread = is_thread and channel.is_private()
        try:
            # age-restricted (NSFW) channels count as private too: their content shouldn't flow into regular channels
            nsfw = bool(channel.is_nsfw()) if hasattr(channel, "is_nsfw") else False
            public = not private_thread and not nsfw and bool(channel.permissions_for(guild.default_role).view_channel)
        except Exception:  # e.g. the thread's parent channel isn't cached: when unsure, treat it as private
            public = False
        return ChannelPlace(
            guild_id=guild.id,
            name=getattr(channel, "name", None) or str(channel_id),
            parent_id=channel.parent_id if is_thread else None,
            category_id=safe_category_id(channel),
            public=public,
            private_thread=private_thread,
        )

    async def _resolve(self, channel_id: int) -> Any:
        channel = self.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except Exception as exc:
                log.warning("Can't find channel %s: %r", channel_id, exc)
                return None
        return channel

    async def send(self, channel_id: int, text: str, *, reply_to: int | None, ping: bool) -> int | None:
        channel = await self._resolve(channel_id)
        if channel is None:
            return None
        kwargs: dict[str, Any] = {
            # only "notify the person being replied to" is allowed; @everyone / @someone / @role in the model's output never actually pings
            "allowed_mentions": discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=ping),
            "suppress_embeds": True,  # source links in answers don't expand into previews, so they don't flood the channel
        }
        if reply_to is not None:
            kwargs["reference"] = discord.MessageReference(
                message_id=reply_to, channel_id=channel_id, fail_if_not_exists=False
            )
        try:
            sent = await channel.send(text, **kwargs)
        except Exception as exc:  # HTTPException, plus OSError / aiohttp errors when the network drops
            log.warning("Sending a message failed (channel %s): %r", channel_id, exc)
            return None
        self.remember_sent(sent.id)
        return sent.id

    async def react(self, channel_id: int, message_id: int, emoji: str) -> bool:
        channel = await self._resolve(channel_id)
        if channel is None or not hasattr(channel, "get_partial_message"):
            return False
        try:
            await channel.get_partial_message(message_id).add_reaction(emoji)
            return True
        except Exception as exc:
            log.info("Adding reaction %s failed: %r", emoji, exc)
            return False

    def typing(self, channel_id: int) -> _SafeTyping:
        return _SafeTyping(self.client.get_channel(channel_id))


class DSBot(discord.Client):
    def __init__(
        self,
        cfg: AppConfig,
        secrets: Secrets,
        store: Store,
        settings: Settings,
        ledger: Ledger,
        llm: DeepSeekClient,
    ):
        intents = discord.Intents.default()  # the regular intents, including messages and typing
        intents.message_content = True  # privileged intent: has to be turned on in the Developer Portal
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            proxy=secrets.discord_proxy,
        )
        self.cfg = cfg
        self.secrets = secrets
        self.store = store
        self.settings = settings
        self.ledger = ledger
        self.llm = llm
        self.tree = app_commands.CommandTree(self)
        self.transport = DiscordTransport(self)
        self.searcher = WebSearch(
            api_key=secrets.deepseek_api_key,
            url=cfg.deepseek.anthropic_url,
            model=cfg.deepseek.model,
            timeout=cfg.deepseek.search_timeout_seconds,
            max_uses=cfg.deepseek.search_max_uses,
            tavily_key=secrets.tavily_api_key,
        )
        self.engine = Engine(cfg, settings, store, llm, ledger, self.transport, searcher=self.searcher)
        self._ticker: asyncio.Task[None] | None = None

        from .commands import register_commands

        register_commands(self)

    # ---------------------------------------------------------------- lifecycle

    async def setup_hook(self) -> None:
        if self.cfg.bot.sync_commands_on_start:
            try:
                synced = await self.tree.sync()
                log.info("Registered %d global slash commands", len(synced))
            except discord.HTTPException as exc:
                log.error("Registering global slash commands failed: %s", exc)
            if self.secrets.control_guild_id:
                try:
                    synced = await self.tree.sync(guild=discord.Object(id=self.secrets.control_guild_id))
                    log.info("Registered %d owner commands in the control server", len(synced))
                except discord.HTTPException as exc:
                    log.error("Registering owner commands in CONTROL_GUILD_ID failed (is the bot in that server?): %s", exc)
        # docker stop sends SIGTERM; Python running as PID 1 in a container ignores it by default, so catch it here and shut down cleanly
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self.close()))
        except (NotImplementedError, RuntimeError):
            pass  # not supported on Windows
        self._ticker = asyncio.create_task(self._tick_loop())

    async def on_ready(self) -> None:
        assert self.user is not None
        url = discord.utils.oauth_url(self.user.id, permissions=BOT_PERMISSIONS, scopes=("bot", "applications.commands"))
        log.info("Logged in as %s (ID %s), in %d servers", self.user, self.user.id, len(self.guilds))
        log.info("Invite link: %s", url)

    async def _tick_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.engine.tick()
            except Exception:
                log.exception("Error in the periodic check")
            await asyncio.sleep(self.cfg.bot.tick_seconds)

    async def close(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
        try:
            await asyncio.wait_for(self.engine.drain(), timeout=20)
        except asyncio.TimeoutError:
            log.warning("Some rounds were still running at shutdown; exiting anyway")
        await self.llm.close()
        await self.searcher.close()
        await super().close()

    # ---------------------------------------------------------------- events

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or message.guild is None:
            return  # DMs aren't handled
        if message.author.id == self.user.id:
            return
        if message.type not in CHAT_MESSAGE_TYPES:
            return  # system messages such as join notices and pin notifications
        try:
            incoming = self.to_incoming(message)
        except Exception:
            log.exception("Converting message %s failed", message.id)
            return
        await self.engine.handle_message(incoming)

    async def on_raw_typing(self, payload: discord.RawTypingEvent) -> None:
        if self.user is None or payload.guild_id is None or payload.user_id == self.user.id:
            return
        if payload.user is not None and payload.user.bot:
            return
        self.engine.handle_typing(payload.channel_id)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """A message was edited, or Discord added the link preview (that's how GIFs from the GIF picker arrive)."""
        if self.user is None or payload.guild_id is None:
            return
        if self.engine.find_message(payload.channel_id, payload.message_id) is None:
            return  # already handled (or not in an allowed area); nothing to do
        message = getattr(payload, "message", None)
        if message is None or message.guild is None or message.author.id == self.user.id:
            return
        try:
            self.engine.refresh_message(self.to_incoming(message))
        except Exception:
            log.exception("Updating message %s failed", payload.message_id)

    async def fetch_url(self, url: str) -> bytes:
        """Download link-preview and sticker images (through discord.py's own HTTP session, so the proxy settings match)."""
        return await self.http.get_from_cdn(url)

    def to_incoming(self, message: discord.Message) -> IncomingMessage:
        assert self.user is not None and message.guild is not None
        channel = message.channel
        parent_id = channel.parent_id if isinstance(channel, discord.Thread) else None
        aliases = self.settings.get(
            "aliases",
            guild_id=message.guild.id,
            channel_id=channel.id,
            parent_id=parent_id,
            category_id=safe_category_id(channel),
        )
        return convert_message(
            message,
            me_id=self.user.id,
            aliases=parse_aliases(aliases),
            is_ours=self.transport.is_ours,
            fetch_url=self.fetch_url,
        )
