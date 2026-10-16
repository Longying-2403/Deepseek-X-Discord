"""Discord 接入层：把 discord.py 的事件转成引擎的数据结构，把引擎的动作发回 Discord。"""

from __future__ import annotations

import asyncio
import io
import logging
import signal
from collections import deque
from typing import Any

import discord
from discord import app_commands

from . import __version__
from .config import AppConfig, Secrets
from .convert import (
    convert_message,
    emoji_lookup,
    parse_aliases,
    resolve_reaction,
    safe_category_id,
    usable_emojis,
    usable_stickers,
)
from .engine import Engine
from .ledger import Ledger
from .links import LinkReader
from .llm import DeepSeekClient
from .memes import MemeLibrary
from .memory import CardStore, MemoryStore
from .models import ChannelInfo, ChannelPlace, IncomingMessage
from .net import HttpClient
from .pictures import PictureFinder
from .search import WebSearch
from .settings import Settings
from .store import Store
from .tavily import Tavily
from .textutil import DISCORD_LIMIT, render_custom_emoji

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
    attach_files=True,  # 发表情包、网上找的图
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
            # 排好序：这两份名单放在 system prompt 里，顺序一乱缓存就失效
            emojis=sorted({e.name for e in usable_emojis(guild)}),
            stickers=sorted({st.name for st in usable_stickers(guild)}),
        )

    def locate(self, channel_id: int) -> ChannelPlace | None:
        """频道在哪个服务器、哪个类别，以及是不是所有人都看得到（跨频道记忆用）。不在缓存里就返回 None。"""
        channel = self.client.get_channel(channel_id)
        guild = getattr(channel, "guild", None)
        if channel is None or guild is None:
            return None
        is_thread = isinstance(channel, discord.Thread)
        private_thread = is_thread and channel.is_private()
        try:
            # 成人内容（NSFW）频道也当私密：内容不该流到普通频道
            nsfw = bool(channel.is_nsfw()) if hasattr(channel, "is_nsfw") else False
            public = not private_thread and not nsfw and bool(channel.permissions_for(guild.default_role).view_channel)
        except Exception:  # 帖子所在的频道没缓存之类：拿不准就当私密
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
                log.warning("找不到频道 %s：%r", channel_id, exc)
                return None
        return channel

    @staticmethod
    def _base_kwargs(channel_id: int, reply_to: int | None, ping: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            # 只允许“提醒被回复的人”，模型输出里的 @everyone / @某人 / @身份组 都不会真的 ping
            "allowed_mentions": discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=ping),
        }
        if reply_to is not None:
            kwargs["reference"] = discord.MessageReference(
                message_id=reply_to, channel_id=channel_id, fail_if_not_exists=False
            )
        return kwargs

    async def send_sticker(self, channel_id: int, name: str, *, reply_to: int | None, ping: bool) -> int | None:
        """发一张这个服务器自己的贴图（name 是贴图名，引擎已经核对过）。"""
        channel = await self._resolve(channel_id)
        if channel is None:
            return None
        sticker = next((st for st in usable_stickers(getattr(channel, "guild", None)) if st.name == name), None)
        if sticker is None:
            log.info("频道 %s：找不到贴图「%s」（可能刚被删掉）", channel_id, name)
            return None
        try:
            sent = await channel.send(stickers=[sticker], **self._base_kwargs(channel_id, reply_to, ping))
        except Exception as exc:  # 没权限、贴图不能用之类
            log.warning("发贴图失败（频道 %s）：%r", channel_id, exc)
            return None
        self.remember_sent(sent.id)
        return sent.id

    async def send_file(
        self, channel_id: int, data: bytes, filename: str, *, reply_to: int | None, ping: bool
    ) -> int | None:
        """发一张图（表情包、网上找的图），作为附件上传。"""
        channel = await self._resolve(channel_id)
        if channel is None:
            return None
        try:
            sent = await channel.send(
                file=discord.File(io.BytesIO(data), filename=filename), **self._base_kwargs(channel_id, reply_to, ping)
            )
        except Exception as exc:  # 没有“附加文件”权限、图太大之类
            log.warning("发图失败（频道 %s）：%r", channel_id, exc)
            return None
        self.remember_sent(sent.id)
        return sent.id

    async def send(self, channel_id: int, text: str, *, reply_to: int | None, ping: bool) -> int | None:
        channel = await self._resolve(channel_id)
        if channel is None:
            return None
        converted = render_custom_emoji(text, emoji_lookup(getattr(channel, "guild", None)))  # :名字: → 服务器表情
        if len(converted) <= DISCORD_LIMIT:  # 换成 <:名字:id> 会变长；超过上限就原样发，别整条发不出去
            text = converted
        kwargs = self._base_kwargs(channel_id, reply_to, ping)
        kwargs["suppress_embeds"] = True  # 回答里带的来源链接不展开预览，免得刷屏
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
            await channel.get_partial_message(message_id).add_reaction(resolve_reaction(emoji, getattr(channel, "guild", None)))
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
        cards: CardStore | None = None,
        memory: MemoryStore | None = None,
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
        self.searcher = WebSearch(
            api_key=secrets.deepseek_api_key,
            url=cfg.deepseek.anthropic_url,
            model=cfg.deepseek.model,
            timeout=cfg.deepseek.search_timeout_seconds,
            max_uses=cfg.deepseek.search_max_uses,
            tavily_key=secrets.tavily_api_key,
        )
        # 读链接、找图：推文走 FxTwitter（不用 key），网页和找图要 Tavily。
        # 注意别叫 self.http：那是 discord.py 自己连 Discord 用的客户端，覆盖掉就登录不了
        self.web = HttpClient()
        tavily = Tavily(self.web, secrets.tavily_api_key) if secrets.tavily_api_key else None
        self.links = LinkReader(self.web, tavily, tz=cfg.bot.timezone)
        self.pictures = (
            PictureFinder(
                self.web,
                tavily,
                keep=cfg.engine.image_candidates,
                max_bytes=cfg.engine.image_max_bytes,
                view_side=cfg.engine.low_detail_max_side,
            )
            if tavily is not None
            else None
        )
        self.memes = MemeLibrary(cfg.bot.meme_dir, cfg.bot.server_meme_dir, max_bytes=cfg.engine.image_max_bytes)
        self.cards = cards  # 人设卡（data/cards.sqlite3）
        self.memory = memory  # 长期记忆（data/memory.sqlite3）
        self.engine = Engine(
            cfg,
            settings,
            store,
            llm,
            ledger,
            self.transport,
            searcher=self.searcher,
            owner_id=secrets.owner_id,
            link_reader=self.links,
            pictures=self.pictures,
            memes=self.memes,
            cards=cards,
            memory=memory,
        )
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
        log.info("已登录：%s（ID %s），在 %d 个服务器里（dsbot %s）", self.user, self.user.id, len(self.guilds), __version__)
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
        await self.searcher.close()
        await self.web.close()
        for db in (self.cards, self.memory):
            if db is not None:
                db.close()
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

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """消息被编辑，或 Discord 补上了链接预览（GIF 选单发的 GIF 就是这样）。"""
        if self.user is None or payload.guild_id is None:
            return
        if self.engine.find_message(payload.channel_id, payload.message_id) is None:
            return  # 已经处理过（或不在允许的区域），不用管
        message = getattr(payload, "message", None)
        if message is None or message.guild is None or message.author.id == self.user.id:
            return
        try:
            self.engine.refresh_message(self.to_incoming(message))
        except Exception:
            log.exception("更新消息 %s 失败", payload.message_id)

    async def fetch_url(self, url: str) -> bytes:
        """下载链接预览、贴图的图片（走 discord.py 自己的 HTTP 会话，代理设置也一致）。"""
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
