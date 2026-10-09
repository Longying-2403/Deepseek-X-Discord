"""Discord 接入层：把 discord.py 的事件转成引擎的数据结构，把引擎的动作发回 Discord。"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections import deque
from typing import Any

import discord
from discord import app_commands

from .config import AppConfig, Secrets
from .convert import convert_message, parse_aliases
from .engine import Engine
from .ledger import Ledger
from .llm import DeepSeekClient
from .models import ChannelInfo, IncomingMessage
from .settings import Settings
from .store import Store

log = logging.getLogger(__name__)

CHAT_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)

# 邀请 bot 时需要的权限
BOT_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    send_messages_in_threads=True,
    read_message_history=True,
    add_reactions=True,
    embed_links=True,
)


class _SafeTyping:
    """“正在输入”失败（比如缺权限）时静默忽略，不影响后面发消息。"""

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
            log.debug("显示“正在输入”失败：%r", exc)

    async def __aexit__(self, *exc: Any) -> bool:
        if self._entered:
            try:
                await self._cm.__aexit__(*exc)
            except Exception:
                pass
        return False


class DiscordTransport:
    """引擎 → Discord。"""

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
            guild_name=guild.name if guild is not None else "未知服务器",
            channel_name=getattr(channel, "name", None) or str(channel_id),
            bot_name=bot_name,
        )

    async def _resolve(self, channel_id: int) -> Any:
        channel = self.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except Exception as exc:
                log.warning("找不到频道 %s：%r", channel_id, exc)
                return None
        return channel

    async def send(self, channel_id: int, text: str, *, reply_to: int | None, ping: bool) -> int | None:
        channel = await self._resolve(channel_id)
        if channel is None:
            return None
        kwargs: dict[str, Any] = {
            # 只允许“提醒被回复的人”，模型输出里的 @everyone / @某人 / @身份组 都不会真的 ping
            "allowed_mentions": discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=ping),
        }
        if reply_to is not None:
            kwargs["reference"] = discord.MessageReference(
                message_id=reply_to, channel_id=channel_id, fail_if_not_exists=False
            )
        try:
            sent = await channel.send(text, **kwargs)
        except Exception as exc:  # HTTPException，以及网络断开时的 OSError / aiohttp 错误
            log.warning("发消息失败（频道 %s）：%r", channel_id, exc)
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
            log.info("加表情 %s 失败：%r", emoji, exc)
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
        intents = discord.Intents.default()  # 含消息、打字等普通 intent
        intents.message_content = True  # 特权 intent：需要在开发者后台打开
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
        self.engine = Engine(cfg, settings, store, llm, ledger, self.transport)
        self._ticker: asyncio.Task[None] | None = None

        from .commands import register_commands

        register_commands(self)

    # ---------------------------------------------------------------- 生命周期

    async def setup_hook(self) -> None:
        if self.cfg.bot.sync_commands_on_start:
            try:
                synced = await self.tree.sync()
                log.info("已注册 %d 个全局斜杠命令", len(synced))
            except discord.HTTPException as exc:
                log.error("注册全局斜杠命令失败：%s", exc)
            if self.secrets.control_guild_id:
                try:
                    synced = await self.tree.sync(guild=discord.Object(id=self.secrets.control_guild_id))
                    log.info("已在私人服务器注册 %d 个主人命令", len(synced))
                except discord.HTTPException as exc:
                    log.error("在 CONTROL_GUILD_ID 注册主人命令失败（bot 在那个服务器里吗？）：%s", exc)
        # docker stop 发的是 SIGTERM；Python 在容器里当 1 号进程时默认会忽略它，这里接住并正常关闭
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self.close()))
        except (NotImplementedError, RuntimeError):
            pass  # Windows 不支持
        self._ticker = asyncio.create_task(self._tick_loop())

    async def on_ready(self) -> None:
        assert self.user is not None
        url = discord.utils.oauth_url(self.user.id, permissions=BOT_PERMISSIONS, scopes=("bot", "applications.commands"))
        log.info("已登录：%s（ID %s），在 %d 个服务器里", self.user, self.user.id, len(self.guilds))
        log.info("邀请链接：%s", url)

    async def _tick_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.engine.tick()
            except Exception:
                log.exception("定时检查出错")
            await asyncio.sleep(self.cfg.bot.tick_seconds)

    async def close(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
        try:
            await asyncio.wait_for(self.engine.drain(), timeout=20)
        except asyncio.TimeoutError:
            log.warning("关闭时还有没跑完的轮次，直接退出")
        await self.llm.close()
        await super().close()

    # ---------------------------------------------------------------- 事件

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or message.guild is None:
            return  # 不处理私信
        if message.author.id == self.user.id:
            return
        if message.type not in CHAT_MESSAGE_TYPES:
            return  # 加入提示、置顶通知之类的系统消息
        try:
            incoming = self.to_incoming(message)
        except Exception:
            log.exception("解析消息 %s 失败", message.id)
            return
        await self.engine.handle_message(incoming)

    async def on_raw_typing(self, payload: discord.RawTypingEvent) -> None:
        if self.user is None or payload.guild_id is None or payload.user_id == self.user.id:
            return
        if payload.user is not None and payload.user.bot:
            return
        self.engine.handle_typing(payload.channel_id)

    def to_incoming(self, message: discord.Message) -> IncomingMessage:
        assert self.user is not None and message.guild is not None
        channel = message.channel
        parent_id = channel.parent_id if isinstance(channel, discord.Thread) else None
        aliases = self.settings.get("aliases", guild_id=message.guild.id, channel_id=channel.id, parent_id=parent_id)
        return convert_message(
            message, me_id=self.user.id, aliases=parse_aliases(aliases), is_ours=self.transport.is_ours
        )
