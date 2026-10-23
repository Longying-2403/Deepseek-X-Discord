"""Discord 接入层：把 discord.py 的事件转成引擎的数据结构，把引擎的动作发回 Discord。"""

from __future__ import annotations

import asyncio
import io
import logging
import signal
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, Callable, Sequence

import discord
from discord import app_commands

from . import __version__
from .commands import discord_safe
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
from .images import MAX_RAW_BYTES
from .ledger import Ledger
from .links import LinkReader
from .llm import DeepSeekClient
from .memes import MemeLibrary
from .memory import CardStore, MemoryStore
from .mentions import name_forms
from .models import ChannelInfo, ChannelPlace, IncomingMessage
from .music import BiliSource, MusicError, MusicPlayer, VoiceNotReady, missing_parts
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
    connect=True,  # 点歌：进语音频道
    speak=True,  # 点歌：在语音频道里放声音
)
MUSIC_BITRATE = 128  # 放歌的码率（kbps）


def _log_close_error(task: asyncio.Task[Any]) -> None:
    """收到 SIGTERM 后的关闭出错了：写进日志（没人 await 这个任务，不然错误就悄悄没了）。"""
    if not task.cancelled() and task.exception() is not None:
        log.error("关闭时出错", exc_info=task.exception())


def _off(value: Any) -> bool:
    """config.toml 里的开关是不是关着（写成字符串的 "false" 也算关）。"""
    return value is False or str(value).strip().lower() in ("false", "0", "no", "off", "")


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
    def _base_kwargs(channel_id: int, reply_to: int | None, ping: bool, mentions: Sequence[int] = ()) -> dict[str, Any]:
        users: list[discord.Object] | bool = [discord.Object(id=uid) for uid in mentions] or False
        kwargs: dict[str, Any] = {
            # 只提醒被回复的人和引擎挑出来的那几个人（mentions）；@everyone、@身份组、话里别的 @ 都不会真的 ping
            "allowed_mentions": discord.AllowedMentions(everyone=False, users=users, roles=False, replied_user=ping),
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

    async def send(
        self, channel_id: int, text: str, *, reply_to: int | None, ping: bool, mentions: Sequence[int] = ()
    ) -> int | None:
        channel = await self._resolve(channel_id)
        if channel is None:
            return None
        converted = render_custom_emoji(text, emoji_lookup(getattr(channel, "guild", None)))  # :名字: → 服务器表情
        if len(converted) <= DISCORD_LIMIT:  # 换成 <:名字:id> 会变长；超过上限就原样发，别整条发不出去
            text = converted
        kwargs = self._base_kwargs(channel_id, reply_to, ping, mentions)
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

    def channel_names(self, guild_id: int) -> dict[str, set[int]]:
        """服务器里它看得到的频道和帖子：名字 -> 频道（它在话里写 #频道名 时换成真的频道链接）。"""
        guild = self.client.get_guild(guild_id)
        if guild is None:
            return {}
        me = guild.me
        out: dict[str, set[int]] = {}
        for channel in [*guild.channels, *guild.threads]:
            if isinstance(channel, discord.CategoryChannel):
                continue
            try:
                if me is not None and not channel.permissions_for(me).view_channel:
                    continue
            except Exception:
                continue
            for form in name_forms(channel.name):
                out.setdefault(form, set()).add(channel.id)
        return out

    async def find_members(self, guild_id: int, starts: list[str]) -> dict[str, set[int]]:
        """按名字开头在服务器成员里找人（Discord 按用户名、服务器昵称的开头找）。starts 是话里 @ 后面那一段，
        用开头两个字去查，找到的人连同他的几种名字交给引擎，由它按最长的名字对。bot 不算。"""
        guild = self.client.get_guild(guild_id)
        if guild is None:
            return {}
        out: dict[str, set[int]] = {}
        for rest in starts[:3]:
            query = rest.strip()[:2]
            if not query:
                continue
            try:
                members = await asyncio.wait_for(guild.query_members(query=query, limit=50, cache=False), 3)
            except Exception as exc:
                log.info("按名字找人（%s）失败：%r", query, exc)
                continue
            for member in members:
                if member.bot:
                    continue
                for name in {member.display_name, member.global_name, member.nick, member.name}:
                    for form in name_forms(name or ""):
                        out.setdefault(form, set()).add(member.id)
        return out


class DiscordVoice:
    """点歌 → Discord 语音：进出语音频道、放音频（MusicPlayer 只管逻辑）。"""

    def __init__(self, client: "DSBot"):
        self.client = client

    def _voice_client(self, guild_id: int) -> Any:
        guild = self.client.get_guild(guild_id)
        return guild.voice_client if guild is not None else None

    def user_channel(self, guild_id: int, user_id: int) -> int | None:
        guild = self.client.get_guild(guild_id)
        if guild is None:
            return None
        for channel in (*guild.voice_channels, *guild.stage_channels):
            if user_id in channel.voice_states:
                return channel.id
        return None

    def bot_channel(self, guild_id: int) -> int | None:
        channel = getattr(self._voice_client(guild_id), "channel", None)
        return channel.id if channel is not None else None

    def connected_guilds(self) -> list[int]:
        out = []
        for vc in self.client.voice_clients:
            guild = getattr(vc, "guild", None)
            if guild is not None:
                out.append(guild.id)
        return out

    def listeners(self, guild_id: int) -> int:
        """它所在的语音频道里有几个真人（别的 bot 不算）。"""
        guild = self.client.get_guild(guild_id)
        channel = getattr(self._voice_client(guild_id), "channel", None)
        if guild is None or channel is None:
            return 0
        me = self.client.user.id if self.client.user is not None else None
        count = 0
        for uid in channel.voice_states:
            if uid == me:
                continue
            member = guild.get_member(uid)
            if member is not None and member.bot:
                continue
            count += 1
        return count

    def channel_name(self, channel_id: int) -> str:
        return getattr(self.client.get_channel(channel_id), "name", None) or "语音频道"

    async def connect(self, guild_id: int, channel_id: int) -> None:
        guild = self.client.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        if isinstance(channel, discord.StageChannel):
            raise MusicError(f"「{channel.name}」是舞台频道，那里放不了歌，换个普通的语音频道")
        if guild is None or not isinstance(channel, discord.VoiceChannel):
            raise MusicError("找不到那个语音频道")
        perms = channel.permissions_for(guild.me)
        if not perms.view_channel or not perms.connect:
            raise MusicError(f"我没有进「{channel.name}」的权限（要给 bot「连接」权限）")
        if not perms.speak:
            raise MusicError(f"我在「{channel.name}」里没有「说话」权限，放不出声音")
        vc = guild.voice_client
        if vc is not None:
            if getattr(vc.channel, "id", None) == channel_id:
                return
            await vc.move_to(channel, timeout=20)  # 超时的话 discord.py 只记一笔日志，不报错：下面自己看换过去没有
            if getattr(vc.channel, "id", None) != channel_id or not vc.is_connected():
                raise MusicError("换语音频道没成功，再试一次")
            return
        if channel.user_limit and len(channel.voice_states) >= channel.user_limit and not perms.move_members:
            raise MusicError(f"「{channel.name}」满员了，我进不去")
        try:
            await channel.connect(timeout=20, reconnect=True, self_deaf=True)
        except asyncio.TimeoutError:
            await self.disconnect(guild_id)
            raise MusicError("连语音频道超时了（可能是网络问题），再试一次") from None
        except discord.ClientException:
            if guild.voice_client is None:
                raise MusicError("进不了那个语音频道") from None
        except Exception as exc:
            log.warning("进语音频道 %s 失败：%r", channel_id, exc)
            await self.disconnect(guild_id)
            raise MusicError("进不了那个语音频道") from None

    def play(self, guild_id: int, path: str, on_end: Callable[[Exception | None], None]) -> None:
        vc = self._voice_client(guild_id)
        if vc is None or not vc.is_connected():
            raise VoiceNotReady("语音还没连好")  # 先留在队列里：连好了再放，断了由定时检查清掉
        if vc.is_playing() or vc.is_paused():
            vc.stop()  # 保险：照理这时候不会还在放
        vc.play(discord.FFmpegOpusAudio(path, bitrate=MUSIC_BITRATE), after=on_end)

    def pause(self, guild_id: int) -> None:
        vc = self._voice_client(guild_id)
        if vc is not None:
            vc.pause()

    def resume(self, guild_id: int) -> None:
        vc = self._voice_client(guild_id)
        if vc is not None:
            vc.resume()

    def stop(self, guild_id: int) -> None:
        vc = self._voice_client(guild_id)
        if vc is not None:
            vc.stop()

    async def disconnect(self, guild_id: int) -> None:
        vc = self._voice_client(guild_id)
        if vc is not None:
            await vc.disconnect(force=True)


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
        self.tavily = tavily
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
        self._closing: asyncio.Task[None] | None = None  # 收到 SIGTERM 后开始的关闭
        self._cdn: Any = None  # 下载预览图、贴图用的 aiohttp 会话（第一次用时才建）
        self.music: MusicPlayer | None = None  # 点歌：登录时才启用（--check 不会去动下载目录）
        self.music_problem = "还没启动"  # 点歌用不了的原因

        from .commands import register_commands

        register_commands(self)

    # ---------------------------------------------------------------- 生命周期

    async def setup_hook(self) -> None:
        # docker stop 发的是 SIGTERM；Python 在容器里当 1 号进程时默认会忽略它，这里接住并正常关闭。
        # 最先装上：后面同步斜杠命令可能被限速、等很久，这期间 docker stop 也要能马上停
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self._on_sigterm)
        except (NotImplementedError, RuntimeError):
            pass  # Windows 不支持
        self._start_music()
        if self.cfg.bot.sync_commands_on_start:
            try:
                synced = await self.tree.sync()
                log.info("已注册 %d 个全局斜杠命令：%s", len(synced), "、".join(f"/{c.name}" for c in synced))
            except discord.HTTPException as exc:
                log.error("注册全局斜杠命令失败：%s", exc)
            if self.secrets.control_guild_id:
                try:
                    synced = await self.tree.sync(guild=discord.Object(id=self.secrets.control_guild_id))
                    log.info("已在私人服务器注册 %d 个主人命令：%s", len(synced), "、".join(f"/{c.name}" for c in synced))
                except discord.HTTPException as exc:
                    log.error("在 CONTROL_GUILD_ID 注册主人命令失败（bot 在那个服务器里吗？）：%s", exc)
        self._ticker = asyncio.create_task(self._tick_loop())

    def _on_sigterm(self) -> None:
        # 任务要留着引用：事件循环只弱引用任务，没人引用的关闭任务可能关到一半被回收。连着收到几次只关一次
        if self._closing is None:
            self._closing = asyncio.get_running_loop().create_task(self.close())
            self._closing.add_done_callback(_log_close_error)

    def _start_music(self) -> None:
        if _off(self.cfg.bot.music):
            self.music_problem = "config.toml 里关掉了（[bot] music = false）"
            log.info("点歌：关")
            return
        problem = missing_parts()
        if problem:
            self.music_problem = problem
            log.warning("点歌用不了：%s", problem)
            return
        try:
            source = BiliSource(Path(tempfile.gettempdir()) / "dsbot-music", self.tavily, self.web)
            source.reset_cache()
        except OSError as exc:
            self.music_problem = f"建不了下载目录（{exc}）"
            log.warning("点歌用不了：%s", self.music_problem)
            return
        self.music = MusicPlayer(source, DiscordVoice(self), announce=self._music_say)
        self.engine.music = self.music
        self.music_problem = ""
        log.info("点歌：开（B站%s）", "，按歌名找歌用 Tavily" if self.tavily is not None else "，按歌名找歌用 B站自己的搜索")

    async def _music_say(self, channel_id: int, text: str) -> None:
        """点歌的提示（自动放下一首、下载失败、离开语音频道）。"""
        await self.transport.send(channel_id, discord_safe(text), reply_to=None, ping=False)

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
        try:
            if self._ticker is not None:
                self._ticker.cancel()
            if self.music is not None:
                self.music.closing()  # 先停掉下载、不再接新的点歌（下载线程不停的话进程退不出去）
            self.engine.closing = True  # 整理记忆问完手上这一段就停，不再接着问
            try:
                await asyncio.wait_for(self.engine.drain(), timeout=20)
            except asyncio.TimeoutError:
                log.warning("关闭时还有没跑完的轮次，直接退出")
            if self.music is not None:
                try:
                    await asyncio.wait_for(self.music.close(), timeout=5)  # 离开语音频道
                except Exception as exc:
                    log.warning("关闭点歌出错：%r", exc)
            await self.llm.close()
            await self.searcher.close()
            await self.web.close()
            if self._cdn is not None:
                await self._cdn.close()
            for db in (self.cards, self.memory):
                if db is not None:
                    db.close()
        finally:
            await super().close()  # 前面哪一步出错也要断开和 Discord 的连接，不然进程退不出去

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
        message = getattr(payload, "message", None)
        if message is None or message.guild is None or message.author.id == self.user.id:
            return
        if message.type not in CHAT_MESSAGE_TYPES:
            return
        try:
            # edited_at 只有本人改了内容才有；只是补上预览的更新没有
            self.engine.handle_edit(self.to_incoming(message), edited=message.edited_at is not None)
        except Exception:
            log.exception("更新消息 %s 失败", payload.message_id)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is not None:
            self.engine.handle_delete(payload.guild_id, payload.channel_id, [payload.message_id])

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        """管理员一次删了很多条。"""
        if payload.guild_id is not None:
            self.engine.handle_delete(payload.guild_id, payload.channel_id, payload.message_ids)

    async def fetch_url(self, url: str) -> bytes:
        """下载链接预览、贴图的图片（只会是 Discord 自己的图片代理 / CDN，见 convert._embed_media）。
        代理设置和 discord.py 一致。这种图事先不知道多大：边下边数，超过 MAX_RAW_BYTES 就放弃，不整张读进内存。"""
        import aiohttp

        if self._cdn is None or self._cdn.closed:
            self._cdn = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        kwargs: dict[str, Any] = {}
        if self.http.proxy is not None:
            kwargs["proxy"] = self.http.proxy
        if self.http.proxy_auth is not None:
            kwargs["proxy_auth"] = self.http.proxy_auth
        async with self._cdn.get(url, **kwargs) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            if resp.content_length is not None and resp.content_length > MAX_RAW_BYTES:
                raise ValueError(f"图太大（{resp.content_length} 字节）")
            data = bytearray()
            async for chunk in resp.content.iter_chunked(1 << 16):
                data += chunk
                if len(data) > MAX_RAW_BYTES:
                    raise ValueError("图太大")
        return bytes(data)

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
