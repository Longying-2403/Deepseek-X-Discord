"""调度引擎：缓冲消息 → 判断时机 → 调用模型 → 执行动作。

两条路径共用同一份对话历史：
- 被叫到（@、回复、别名）：立刻跑一轮，不消耗主动额度，只受“每人每分钟”限速。
- 主动：频道开了主动发言，并且 有新消息 + (冷场 或 等太久) + 有额度 + 间隔够长 + 没超预算 时跑一轮。

每一轮固定往历史里追加 user（这批新消息）+ assistant（模型原样输出），从不改写旧内容，
所以下一轮的请求总是以上一轮的请求 + 输出开头，最大化 DeepSeek 的前缀缓存命中。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, AsyncContextManager, Awaitable, Callable, Protocol

from .actions import dump_actions, parse_actions
from .config import AppConfig
from .files import classify, max_bytes_for, read_file
from .images import MAX_RAW_BYTES, to_data_uri
from .ledger import Ledger
from .llm import LLMError
from .models import (
    Action,
    ChannelInfo,
    Completion,
    DocBlock,
    FileAttachment,
    FileText,
    ImageAttachment,
    IncomingMessage,
)
from .prompts import SUMMARY_SYSTEM, build_system_prompt
from .quota import QuotaBucket, SlidingWindow, TurnTracker
from .render import assistant_text, content_text, content_tokens, neutralize, render_read_result, render_round
from .settings import Settings, View
from .store import Conversation, Store
from .textutil import one_line, split_for_discord, typing_delay

log = logging.getLogger(__name__)

MAX_MESSAGES_PER_ROUND = 4  # 一轮最多发几条消息
SID_MAP_LIMIT = 4000
BAD_REQUEST = {400, 413, 422}  # 重试也没用的错误：这批消息本身有问题
ERROR_BACKOFF = 60.0  # 普通错误后，这个频道多久内不主动判断
SEND_FAIL_LIMIT = 3  # 连续几轮发不出去（比如没权限）就暂停这个频道
SEND_FAIL_BACKOFF = 1800.0
HOUSEKEEP_EVERY = 600.0
IDLE_EVICT = 3600.0  # 闲置这么久的频道，把内存里的对话记录先放掉（需要时再从数据库读）
EMBED_WAIT = 2.0  # 叫它的消息带链接、预览还没到时，最多等这么久再回
AUTO_READ_WINDOW = 120.0  # 同一个人发了文件、这么久之内又叫它，就直接读那个文件
RECENT_FILES_KEEP = 30  # 每个频道记住最近多少条带文件的消息（模型可以用 read 要求读）
MAX_READ_TARGETS = 3
MAX_DOC_IMAGES = 4  # 一轮里扫描版 PDF 最多附几张页面图
PASTE_NAME = "message.txt"  # Discord 把太长的消息自动转成的文件
PASTE_MAX_BYTES = 1_000_000
MAX_PASTE_PREVIEWS = 3
KEEP_RECENT_TURNS = 12  # 压缩记忆时，最近 6 轮（12 条）不动
DOWNLOAD_TIMEOUT = 60.0  # 下载一个附件最多等多久
FORCE_COMPACT_TOKENS = 100_000  # 上下文超过预算两倍、也超过这么多时，不等旧记录攒够就压缩


class Transport(Protocol):
    def channel_info(self, channel_id: int) -> ChannelInfo: ...

    async def send(self, channel_id: int, text: str, *, reply_to: int | None, ping: bool) -> int | None: ...

    async def react(self, channel_id: int, message_id: int, emoji: str) -> bool: ...

    def typing(self, channel_id: int) -> AsyncContextManager[Any]: ...


class LLM(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: str = "off",
        json_mode: bool = False,
        max_tokens: int | None = None,
        user_id: str | None = None,
    ) -> Completion: ...


def att_key(att: FileAttachment) -> tuple[str, int]:
    return (att.filename, att.size)


@dataclass
class FileEntry:
    """频道里最近一条带文件的消息。"""

    message: IncomingMessage
    read_turns: dict[tuple[str, int], int] = field(default_factory=dict)  # 读进对话的附件 → 内容所在那条记录的 id
    failed: set[tuple[str, int]] = field(default_factory=set)  # 确定读不了的附件：不再自动读，模型要求时才再试

    def unread(self) -> list[FileAttachment]:
        """还没完整读进对话、也不是确定读不了的附件（自动读只读这些）。"""
        return [a for a in self.message.files if att_key(a) not in self.read_turns and att_key(a) not in self.failed]


@dataclass
class _Planned:
    anchor: int  # 内容块放在哪条消息后面
    message_id: int  # 文件所在的消息
    att: FileAttachment
    max_chars: int
    preview: bool = False
    entry: FileEntry | None = None
    partial: bool = False  # 这一轮剩下的额度不够，没读完（或者少附了页面图）：之后还能再完整读


@dataclass
class _Budget:
    """一轮（包括看文件的第二步）里还能读多少。"""

    files: int
    chars: int
    images: int
    allow_images: bool


def _add_detail(detail: str, note: str) -> str:
    return f"{detail}；{note}" if detail else note


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def _block_without_images(block: DocBlock) -> DocBlock:
    if not block.result.images:
        return block
    return replace(block, result=replace(block.result, images=[], detail=_add_detail(block.result.detail, "页面图没能发给你")))


def _without_images(docs: dict[int, list[DocBlock]]) -> dict[int, list[DocBlock]]:
    return {k: [_block_without_images(b) for b in v] for k, v in docs.items()}


def plan_compaction(contents: list[Any], *, force: bool = False) -> int:
    """压缩记忆时要删掉最早的几条（偶数，成对删）。

    按估算的 token 数删掉大约一半，但最近 6 轮不动；旧的部分还不到一半时（比如刚读了一个大文件）先不压，
    等那个文件变旧了再一起压，免得每隔几轮就压一次、每次都让缓存失效。force 用于上下文真的太长的时候。
    """
    n = len(contents) - len(contents) % 2
    keep = 2 if force else KEEP_RECENT_TURNS
    eligible = n - keep
    eligible -= eligible % 2
    if eligible < 2:
        return 0
    weights = [content_tokens(c) for c in contents[:n]]
    total = sum(weights) or 1
    if not force and sum(weights[:eligible]) < total * 0.5:
        return 0
    removed = acc = 0
    for i in range(0, eligible, 2):
        acc += weights[i] + weights[i + 1]
        removed = i + 2
        if acc >= total / 2:
            break
    return removed


@dataclass
class ChannelState:
    channel_id: int
    guild_id: int
    parent_id: int | None
    bucket: QuotaBucket
    category_id: int | None = None
    pending: list[IncomingMessage] = field(default_factory=list)
    inflight: list[IncomingMessage] = field(default_factory=list)  # 正在处理的这一批
    omitted: int = 0
    first_human_at: float | None = None  # 缓冲区里第一条真人消息到达的时间
    last_activity_at: float = 0.0  # 最近一次有人说话或打字
    last_eval_at: float = 0.0
    last_speak_at: float = 0.0
    mention_due: bool = False
    scheduled: bool = False
    error_until: float = 0.0
    send_failures: int = 0
    generation: int = 0  # 每次“清空记忆”加一，跑到一半的轮次据此放弃写入
    turns: TurnTracker = field(default_factory=TurnTracker)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    conversation: Conversation | None = None
    sid_by_msg: dict[int, int] = field(default_factory=dict)
    msg_by_sid: dict[int, int] = field(default_factory=dict)
    recent_files: dict[int, FileEntry] = field(default_factory=dict)  # message_id -> 带文件的消息
    last_decision: str = ""


class Engine:
    def __init__(
        self,
        cfg: AppConfig,
        settings: Settings,
        store: Store,
        llm: LLM,
        ledger: Ledger,
        transport: Transport,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: random.Random | None = None,
        image_loader: Callable[[ImageAttachment, int], Awaitable[str | None]] | None = None,
        file_reader: Callable[[FileAttachment, int], Awaitable[FileText]] | None = None,
    ):
        self.cfg = cfg
        self.ec = cfg.engine
        self.settings = settings
        self.store = store
        self.llm = llm
        self.ledger = ledger
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.image_loader = image_loader or self._load_image
        self.file_reader = file_reader or self._read_file
        self._file_slots = asyncio.Semaphore(2)  # 同时最多解析 2 个文件
        self.tz = cfg.bot.timezone
        self.pricing = cfg.pricing
        self.states: dict[int, ChannelState] = {}
        self.limiter = SlidingWindow()
        self.paused_until = 0.0  # 余额不足 / key 错误时全局暂停
        self._last_housekeep = 0.0
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ 输入

    def state(
        self, channel_id: int, guild_id: int, parent_id: int | None = None, category_id: int | None = None
    ) -> ChannelState:
        st = self.states.get(channel_id)
        if st is None:
            view = self.settings.view(guild_id, channel_id, parent_id, category_id)
            st = ChannelState(channel_id, guild_id, parent_id, QuotaBucket(view.quota_capacity), category_id)
            self.states[channel_id] = st
            return st
        if st.parent_id is None and parent_id is not None:
            st.parent_id = parent_id
        if category_id is not None or parent_id is None:
            st.category_id = category_id  # 频道可能被挪到别的类别（帖子的类别取不到时保留旧值）
        return st

    def view(self, st: ChannelState) -> View:
        return self.settings.view(st.guild_id, st.channel_id, st.parent_id, st.category_id)

    def enabled(self, guild_id: int, channel_id: int, parent_id: int | None, category_id: int | None) -> bool:
        return bool(
            self.settings.get(
                "enabled", guild_id=guild_id, channel_id=channel_id, parent_id=parent_id, category_id=category_id
            )
        )

    async def handle_message(self, msg: IncomingMessage) -> None:
        now = self.clock()
        msg.received_at = now
        if not self.enabled(msg.guild_id, msg.channel_id, msg.parent_id, msg.category_id):
            # 不在允许的区域：完全不看，之前攒下的也一起丢掉
            st = self.states.get(msg.channel_id)
            if st is not None and st.pending:
                st.pending.clear()
                st.first_human_at = None
            return
        st = self.state(msg.channel_id, msg.guild_id, msg.parent_id, msg.category_id)
        view = self.view(st)

        st.pending.append(msg)
        if msg.files:
            st.recent_files[msg.message_id] = FileEntry(msg)
        self._prune(st, now)

        if msg.is_bot:
            return  # 别的 bot：只当上下文，不算轮次、不触发
        st.last_activity_at = now
        if st.first_human_at is None:
            st.first_human_at = now
        if st.turns.is_new_turn(msg.author_id, now, self.ec.turn_merge_seconds):
            st.bucket.on_turn(view.refill_turns, view.quota_capacity)

        if msg.addressed:
            if self.limiter.allow((msg.guild_id, msg.author_id), view.mention_rate_limit, 60.0, now):
                msg.trigger = True
                st.mention_due = True
                self._kick(st)
            else:
                log.info("频道 %s：%s 点名太频繁，这次不立刻回应", st.channel_id, msg.author_name)

    def handle_typing(self, channel_id: int) -> None:
        st = self.states.get(channel_id)
        if st is not None:
            st.last_activity_at = max(st.last_activity_at, self.clock())

    def find_message(self, channel_id: int, message_id: int) -> IncomingMessage | None:
        """还没处理完（缓冲中或正在处理）的消息。"""
        st = self.states.get(channel_id)
        if st is None:
            return None
        for m in (*st.pending, *st.inflight):
            if m.message_id == message_id:
                return m
        return None

    def refresh_message(self, updated: IncomingMessage) -> bool:
        """消息被编辑、或 Discord 补上了链接预览：更新还没处理的那条。点名状态保持不变。"""
        m = self.find_message(updated.channel_id, updated.message_id)
        if m is None:
            return False
        m.text = updated.text
        m.images = updated.images
        m.files = updated.files
        m.stickers = updated.stickers
        m.awaiting_embeds = updated.awaiting_embeds
        return True

    def _prune(self, st: ChannelState, now: float) -> None:
        """丢掉缓冲区里太旧的消息，以及超出上限的部分。"""
        cutoff = now - self.ec.pending_max_age_seconds
        fresh = [m for m in st.pending if m.received_at >= cutoff]
        overflow = len(fresh) - self.ec.max_pending_messages
        if overflow > 0:
            st.omitted += overflow
            fresh = fresh[overflow:]
        if len(fresh) != len(st.pending):
            st.pending = fresh
            st.first_human_at = next((m.received_at for m in st.pending if not m.is_bot), None)
        for mid in [mid for mid, e in st.recent_files.items() if e.message.received_at < cutoff]:
            del st.recent_files[mid]
        while len(st.recent_files) > RECENT_FILES_KEEP:
            del st.recent_files[next(iter(st.recent_files))]

    # ------------------------------------------------------------------ 调度

    async def tick(self) -> None:
        """由外部每隔几秒调用一次，检查哪些频道该跑一轮。"""
        now = self.clock()
        for st in list(self.states.values()):
            if st.scheduled or st.lock.locked() or not st.pending:
                continue
            if st.mention_due or self._proactive_ready(st, now):
                self._kick(st)
        if now - self._last_housekeep >= HOUSEKEEP_EVERY:
            self._last_housekeep = now
            self._housekeep(now)

    def _housekeep(self, now: float) -> None:
        self.limiter.prune(60.0, now)
        for st in self.states.values():
            idle = now - max(st.last_activity_at, st.last_eval_at)
            if st.conversation is not None and not st.lock.locked() and not st.scheduled and idle > IDLE_EVICT:
                st.conversation = None

    def _kick(self, st: ChannelState) -> None:
        if st.scheduled or st.lock.locked():
            return  # 正在跑的那一轮结束后会再检查
        st.scheduled = True
        task = asyncio.get_running_loop().create_task(self._run(st))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """等所有进行中的轮次结束（测试和关闭时用）。"""
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    def _over_budget(self, st: ChannelState, view: View, now: float) -> tuple[bool, bool]:
        guild = self.ledger.guild_spent(st.guild_id, now)
        total = self.ledger.total_spent(now)
        g_budget = view.daily_budget_usd
        t_budget = self.settings.get("global_daily_budget_usd")  # 全局上限只认全局设置
        over = guild >= g_budget or total >= t_budget
        mult = self.ec.hard_stop_multiplier
        hard = guild >= g_budget * mult or total >= t_budget * mult
        return over, hard

    def _proactive_ready(self, st: ChannelState, now: float) -> bool:
        view = self.view(st)
        if not view.enabled or not view.proactive or st.first_human_at is None:
            return False
        if now < self.paused_until or now < st.error_until:
            return False
        if not st.bucket.ready(view.quota_capacity):
            return False  # 没额度时连判断都不做，省 token
        if now - st.last_speak_at < view.min_speak_gap:
            return False
        if now - st.last_eval_at < view.min_eval_interval:
            return False
        if self._over_budget(st, view, now)[0]:
            return False
        quiet = now - st.last_activity_at >= view.quiet_seconds
        waited = now - st.first_human_at >= view.max_wait_seconds
        return quiet or waited

    async def _run(self, st: ChannelState) -> None:
        async with st.lock:
            st.scheduled = False
            st.mention_due = False
            self._prune(st, self.clock())
            if not self.view(st).enabled:
                st.pending.clear()  # 刚被管理员关掉的区域：攒着的消息也不处理了
                st.first_human_at = None
            if st.pending and (any(m.trigger for m in st.pending) or self._proactive_ready(st, self.clock())):
                batch = list(st.pending)
                st.inflight = batch
                try:
                    await self._round(st, batch)
                except Exception:
                    log.exception("频道 %s 处理一轮时出错", st.channel_id)
                    # 不能让同一批消息每次 tick 都再花一次钱：先冷却，点名也不再自动重试
                    now = self.clock()
                    st.last_eval_at = now
                    st.error_until = now + ERROR_BACKOFF
                    for m in batch:
                        m.trigger = False
                finally:
                    st.inflight = []
        if st.mention_due and st.pending:
            self._kick(st)

    # ------------------------------------------------------------------ 一轮

    def _conversation(self, st: ChannelState) -> Conversation:
        if st.conversation is None:
            st.conversation = self.store.load_conversation(st.channel_id, st.guild_id)
            for sid, mid in self.store.load_msgmap(st.channel_id).items():
                st.msg_by_sid.setdefault(sid, mid)
                st.sid_by_msg.setdefault(mid, sid)
        return st.conversation

    def _number(self, st: ChannelState, conv: Conversation, shown: list[IncomingMessage]) -> list[tuple[int, IncomingMessage]]:
        numbered = []
        for m in shown:
            sid = conv.next_short_id
            conv.next_short_id += 1
            st.sid_by_msg[m.message_id] = sid
            st.msg_by_sid[sid] = m.message_id
            numbered.append((sid, m))
        if len(st.msg_by_sid) > SID_MAP_LIMIT:
            for sid in sorted(st.msg_by_sid)[: len(st.msg_by_sid) - SID_MAP_LIMIT + 500]:
                mid = st.msg_by_sid.pop(sid)
                if st.sid_by_msg.get(mid) == sid:
                    del st.sid_by_msg[mid]
        return numbered

    def _select(self, batch: list[IncomingMessage]) -> list[IncomingMessage]:
        """最近的 N 条，再加上更早但在叫它的消息（点名不能因为后面消息太多被挤掉）。"""
        limit = self.ec.max_batch_messages
        recent = batch[-limit:]
        older = batch[: max(0, len(batch) - limit)]
        return [m for m in older if m.trigger] + recent

    async def _round(self, st: ChannelState, batch: list[IncomingMessage]) -> None:
        now = self.clock()
        view = self.view(st)
        generation = st.generation
        triggers = [m for m in batch if m.trigger]
        kind = "mention" if triggers else "proactive"

        over, hard = self._over_budget(st, view, now)
        if kind == "proactive" and (over or now < self.paused_until):
            return
        if kind == "mention" and (hard or now < self.paused_until):
            log.warning("频道 %s：%s，不回应点名", st.channel_id, "暂停中" if now < self.paused_until else "已超预算")
            for m in triggers:
                m.trigger = False
                await self._safe_react(st, m.message_id, "⚠️")
            return

        if any(m.trigger and m.awaiting_embeds and now - m.received_at < EMBED_WAIT for m in batch):
            # GIF / 图片链接的预览图通常晚一两秒才到（消息更新事件会把图补进同一个对象里）
            await self.sleep(EMBED_WAIT)
            now = self.clock()

        conv = self._conversation(st)
        shown = self._select(batch)
        omitted = st.omitted + len(batch) - len(shown)
        numbered = self._number(st, conv, shown)
        shown_ids = {id(m) for m in shown}
        targets = [st.sid_by_msg[m.message_id] for m in triggers if id(m) in shown_ids]
        images = await self._images(shown, triggers) if view.vision else {}
        budget = _Budget(self.ec.max_files_per_round, self.ec.file_round_max_chars, MAX_DOC_IMAGES, bool(view.vision))
        docs: dict[int, list[DocBlock]] = {}
        planned: list[tuple[_Planned, FileText]] = []
        if view.files:
            if kind == "mention":
                async with self.transport.typing(st.channel_id):
                    docs, planned = await self._round_docs(st, shown, triggers, budget)
            else:
                docs, planned = await self._round_docs(st, shown, triggers, budget)
        if st.generation != generation:
            # 读文件的时候记忆被清空了：这一轮作废，点名留着，马上用新的记忆重来
            st.mention_due = st.mention_due or any(m.trigger for m in batch)
            return
        info = self.transport.channel_info(st.channel_id)
        system = build_system_prompt(
            guild=info.guild_name,
            channel=info.channel_name,
            name=info.bot_name,
            persona=view.persona,
            summary=conv.summary,
        )

        def build(with_images: bool) -> tuple[Any, list[dict[str, Any]]]:
            content = render_round(
                numbered,
                sid_by_msg=st.sid_by_msg,
                kind=kind,
                targets=targets,
                now=now,
                tz=self.tz,
                omitted=omitted,
                images=images if with_images else {},
                docs=docs if with_images else _without_images(docs),
                max_chars=self.ec.max_chars_per_message,
            )
            return content, self._messages(system, conv, content)

        has_images = bool(images) or any(b.result.images for blocks in docs.values() for b in blocks)
        thinking = view.mention_thinking if kind == "mention" else view.proactive_thinking
        started = self.clock()
        try:
            user_content, completion, cost, row = await self._ask_with_fallback(
                st, kind, build, has_images, thinking, generation
            )
        except LLMError as exc:
            await self._on_error(st, kind, exc, now, batch, targets, conv, info)
            return

        actions, stored = self._interpret(st, completion, kind, targets, allow_read=kind == "mention")
        if st.generation != generation:
            log.info("频道 %s：这一轮进行中记忆被清空，丢弃结果", st.channel_id)
            self.store.set_outcome(row, "discarded")
            self._consume(st, batch, now)
            return

        # 先把这一轮写进历史（只追加），再消费缓冲区
        self.store.append_round(conv, user_content, stored, [(sid, m.message_id) for sid, m in numbered], now=now)
        self._consume(st, batch, now)
        self._mark_read(conv, planned)

        reads = [a for a in actions if a.kind == "read"]
        if reads:
            # 模型要先看文件：读完再问它一次（第二步不能再 read）
            self.store.set_outcome(row, "read")
            st.last_decision = "被点名，先去看文件"
            step = await self._read_step(st, conv, reads, targets, view, info, system, generation, budget)
            if step is None:
                return
            completion, cost, row, actions = step

        decided_reply = any(a.kind == "reply" and a.messages for a in actions)
        decided_react = any(a.kind == "react" for a in actions)
        outcome = "reply" if decided_reply else ("react" if decided_react else "pass")
        try:
            spoke, _ = await self._execute(st, actions, kind, targets, elapsed=self.clock() - started)
        finally:
            # 额度按模型的决定扣，不按发没发成功扣：没权限的频道也不会无限判断
            if decided_reply:
                st.last_speak_at = self.clock()
            if kind == "proactive":
                if decided_reply:
                    st.bucket.spend(1.0)
                elif decided_react:
                    st.bucket.spend(self.ec.react_cost)
            self.store.set_outcome(row, outcome)
            st.last_decision = self._describe(kind, outcome, actions, completion)
        if decided_reply and not spoke:
            st.send_failures += 1
            if st.send_failures >= SEND_FAIL_LIMIT:
                st.error_until = self.clock() + SEND_FAIL_BACKOFF
                log.warning("频道 %s：连续 %d 轮发不出消息（缺权限？），暂停主动发言 30 分钟", st.channel_id, st.send_failures)
        elif spoke:
            st.send_failures = 0

        u = completion.usage
        log.info(
            "[%s #%s] %s → %s | 输入 %d（命中 %d）输出 %d | $%.5f | %s",
            info.guild_name,
            info.channel_name,
            "点名" if kind == "mention" else "主动",
            outcome,
            u.prompt_tokens,
            u.hit,
            u.output,
            cost,
            one_line(completion.reasoning or "", 160),
        )
        if u.prompt_tokens > view.context_budget_tokens and st.generation == generation:
            # 平时等旧记录攒够一半再压（少让缓存失效）。实在太长（比如连着读了好几个大文件）就不等了：
            # 门槛至少 10 万 token，预算设得很小时也不会刚读完文件就压掉、接着又重读
            limit = max(2 * view.context_budget_tokens, FORCE_COMPACT_TOKENS)
            await self._compact(st, conv, info, force=u.prompt_tokens > limit)

    def _messages(self, system: str, conv: Conversation, content: Any) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        msgs += [{"role": t.role, "content": t.content} for t in conv.turns]
        msgs.append({"role": "user", "content": content})
        return msgs

    async def _ask_with_fallback(
        self,
        st: ChannelState,
        kind: str,
        build: Callable[[bool], tuple[Any, list[dict[str, Any]]]],
        has_images: bool,
        thinking: str,
        generation: int,
    ) -> tuple[Any, Completion, float, int]:
        """调用模型；带图的请求被拒（400 之类）时去掉图片再试一次。返回 (实际发出的 user 内容, 结果, 花费, 用量记录)。"""
        content, messages = build(True)
        try:
            completion, cost, row = await self._ask(st, kind, messages, thinking, generation)
        except LLMError as exc:
            if exc.status not in BAD_REQUEST or not has_images or st.generation != generation:
                raise
            log.warning("频道 %s：带图片的请求被拒（%s），去掉图片再试一次", st.channel_id, exc)
            content, messages = build(False)
            completion, cost, row = await self._ask(st, kind, messages, thinking, generation)
        return content, completion, cost, row

    def _interpret(
        self, st: ChannelState, completion: Completion, kind: str, targets: list[int], *, allow_read: bool
    ) -> tuple[list[Action], str]:
        """解析模型输出，返回 (动作, 写进历史的内容)。"""
        actions, ok = parse_actions(completion.content)
        if not ok:
            log.warning("频道 %s：模型输出不是合法 JSON：%s", st.channel_id, one_line(completion.content, 200))
            text = completion.content.strip()
            if kind == "mention" and text and not text.startswith(("{", "[")):
                actions = [Action("reply", targets[-1] if targets else None, [text[:1800]])]
            return actions, dump_actions(actions)
        reads = [a for a in actions if a.kind == "read"]
        if not reads:
            return actions, completion.content
        if allow_read:
            # 要看文件时这一次只算 read；历史里也只记 read，和实际做的事一致
            reads = reads[:MAX_READ_TARGETS]
            return reads, dump_actions(reads)
        actions = [a for a in actions if a.kind != "read"]  # 没被叫到、或者已经看过一次了
        return actions, dump_actions(actions)

    async def _read_step(
        self,
        st: ChannelState,
        conv: Conversation,
        reads: list[Action],
        targets: list[int],
        view: View,
        info: ChannelInfo,
        system: str,
        generation: int,
        budget: _Budget,
    ) -> tuple[Completion, float, int, list[Action]] | None:
        now = self.clock()
        plan: list[_Planned] = []
        notes: list[str] = []
        for action in reads:
            if not view.files:
                notes.append("（读文件的功能被关掉了，看不了文件）")
                break
            mid = st.msg_by_sid.get(action.to) if action.to is not None else None
            entry = st.recent_files.get(mid) if mid is not None else None
            if mid is None or entry is None or not entry.message.files:
                notes.append(f"（#{action.to} 里没有能读的文件，或者文件太久已经拿不到了）")
                continue
            for att in entry.message.files:
                if att_key(att) in entry.read_turns:
                    name = neutralize(one_line(att.filename, 60))
                    notes.append(f"（#{action.to} 的文件「{name}」前面已经给你看过了，往上翻就有）")
                else:
                    plan.append(_Planned(mid, mid, att, self._file_chars(), entry=entry))
        async with self.transport.typing(st.channel_id):
            results = await self._read_planned(st, plan, budget)
        if st.generation != generation:
            return None  # 读文件的时候记忆被清空了
        blocks = [block for _, block, _, _ in results]

        def build(with_images: bool) -> tuple[Any, list[dict[str, Any]]]:
            used = blocks if with_images else [_block_without_images(b) for b in blocks]
            content = render_read_result(used, notes, targets=targets)
            return content, self._messages(system, conv, content)

        has_images = any(b.result.images for b in blocks)
        try:
            content, completion, cost, row = await self._ask_with_fallback(
                st, "mention", build, has_images, view.mention_thinking, generation
            )
        except LLMError as exc:
            await self._on_error(st, "mention", exc, now, [], targets, conv, info)
            return None
        actions, stored = self._interpret(st, completion, "mention", targets, allow_read=False)
        if st.generation != generation:
            self.store.set_outcome(row, "discarded")
            return None
        self.store.append_round(conv, content, stored, [], now=now)
        self._mark_read(conv, [(item, result) for _, _, item, result in results])
        return completion, cost, row, actions

    # ------------------------------------------------------------------ 文件

    def _label(self, st: ChannelState, message_id: int) -> str:
        sid = st.sid_by_msg.get(message_id)
        return f"#{sid}" if sid is not None else "之前"

    def _auto_entries(
        self, st: ChannelState, shown: list[IncomingMessage], triggers: list[IncomingMessage]
    ) -> list[tuple[FileEntry, int]]:
        """不用模型开口就直接读的文件：叫它的那条消息里的、叫它时回复的那条里的、同一个人刚刚（两分钟内）发的。

        返回 [(文件所在的消息, 内容块放在哪条消息后面)]。已经读进对话的附件不再读。
        """
        shown_ids = {m.message_id for m in shown}
        picked: dict[int, tuple[FileEntry, int]] = {}
        for t in triggers:
            candidates = [st.recent_files.get(t.message_id)]
            if t.reply_to_id is not None:
                candidates.append(st.recent_files.get(t.reply_to_id))
            for entry in reversed(list(st.recent_files.values())):
                m = entry.message
                if m.author_id == t.author_id and 0 <= t.received_at - m.received_at <= AUTO_READ_WINDOW:
                    candidates.append(entry)
            for entry in candidates:
                if entry is None or not entry.unread():
                    continue
                mid = entry.message.message_id
                if mid not in picked:
                    picked[mid] = (entry, mid if mid in shown_ids else t.message_id)
        return list(picked.values())

    async def _round_docs(
        self, st: ChannelState, shown: list[IncomingMessage], triggers: list[IncomingMessage], budget: _Budget
    ) -> tuple[dict[int, list[DocBlock]], list[tuple[_Planned, FileText]]]:
        plan: list[_Planned] = []
        auto = self._auto_entries(st, shown, triggers)
        for entry, anchor in auto:
            for att in entry.unread():
                plan.append(_Planned(anchor, entry.message.message_id, att, self._file_chars(), entry=entry))
        # Discord 把太长的消息自动转成 message.txt：没被直接读的，也给模型看个开头，就像普通的长消息
        auto_ids = {entry.message.message_id for entry, _ in auto}
        previews = 0
        for m in shown:
            if m.message_id in auto_ids:
                continue
            for att in m.files:
                if att.filename == PASTE_NAME and att.size <= PASTE_MAX_BYTES and previews < MAX_PASTE_PREVIEWS:
                    plan.append(_Planned(m.message_id, m.message_id, att, self.ec.max_chars_per_message, preview=True))
                    previews += 1
        docs: dict[int, list[DocBlock]] = {}
        done: list[tuple[_Planned, FileText]] = []
        for anchor, block, item, result in await self._read_planned(st, plan, budget):
            docs.setdefault(anchor, []).append(block)
            done.append((item, result))
        return docs, done

    def _file_chars(self) -> int:
        """单个文件最多读多少字（不超过一轮的总量，否则大文件永远读不完、每次都要重读）。"""
        return max(1, min(self.ec.file_max_chars, self.ec.file_round_max_chars))

    def _precheck(self, att: FileAttachment) -> FileText | None:
        """不用下载就知道读不了的（格式不支持、太大），不占“一次最多读几个文件”的名额。"""
        kind, reason = classify(att.filename, att.content_type)
        if kind == "unsupported":
            return FileText(error=reason)
        limit = max_bytes_for(kind, self.ec.file_max_bytes)
        if att.size and att.size > limit:
            return FileText(error=f"文件太大（{_size(att.size)}），最多读 {_size(limit)}")
        return None

    def _fit_images(self, result: FileText, budget: _Budget) -> tuple[FileText, bool]:
        """返回 (结果, 是不是因为这一轮图太多少附了页面图)。"""
        if not result.images:
            return result, False
        if not budget.allow_images:
            return replace(result, images=[], detail=_add_detail(result.detail, "看图功能关着，没附页面图")), False
        keep = result.images[: max(0, budget.images)]
        budget.images -= len(keep)
        if len(keep) == len(result.images):
            return result, False
        note = f"这一轮图太多，只附了 {len(keep)} 页" if keep else "这一轮图太多，页面图没附上"
        return replace(result, images=keep, detail=_add_detail(result.detail, note)), True

    async def _read_planned(
        self, st: ChannelState, plan: list[_Planned], budget: _Budget
    ) -> list[tuple[int, DocBlock, _Planned, FileText]]:
        out: list[tuple[int, DocBlock, _Planned, FileText]] = []
        for item in plan:
            result = self._precheck(item.att)
            if result is None:
                if not item.preview and budget.files <= 0:
                    result = FileText(error=f"一次最多读 {self.ec.max_files_per_round} 个文件，这个没读", transient=True)
                elif budget.chars < 500:
                    result = FileText(error="这一轮读的内容已经太多了，这个没读", transient=True)
                else:
                    cap = min(item.max_chars, budget.chars)
                    result = await self.file_reader(item.att, cap)
                    if not item.preview:
                        budget.files -= 1
                    budget.chars -= len(result.text)
                    result, trimmed = self._fit_images(result, budget)
                    # 文件本身就短、额度够读完的，不算没读完；文件比单个文件的上限还长、只能读前面的，也不算
                    item.partial = (result.truncated and cap < item.max_chars) or trimmed
            block = DocBlock(self._label(st, item.message_id), item.att.filename, result)
            out.append((item.anchor, block, item, result))
        return out

    def _mark_read(self, conv: Conversation, done: list[tuple[_Planned, FileText]]) -> None:
        """完整读到的附件记成“已经在对话里了”（记下是哪条记录，压缩记忆删掉那条时再取消）；
        确定读不了的（格式不支持、坏文件）记下来，以后不再自动读。暂时的失败和只读了一部分的，下次还会读。"""
        if not conv.turns:
            return
        turn_id = conv.turns[-2].id if len(conv.turns) >= 2 else conv.turns[-1].id
        for item, result in done:
            if item.entry is None or item.preview:
                continue
            key = att_key(item.att)
            if result.error:
                if not result.transient:
                    item.entry.failed.add(key)
            elif not item.partial:
                item.entry.read_turns[key] = turn_id
                item.entry.failed.discard(key)

    async def _read_file(self, att: FileAttachment, max_chars: int) -> FileText:
        kind, reason = classify(att.filename, att.content_type)
        if kind == "unsupported":
            return FileText(error=reason)
        limit = max_bytes_for(kind, self.ec.file_max_bytes)
        if att.size and att.size > limit:
            return FileText(error=f"文件太大（{_size(att.size)}），最多读 {_size(limit)}")
        if att.fetch is None:
            return FileText(error="拿不到这个文件")
        try:
            raw = await asyncio.wait_for(att.fetch(), DOWNLOAD_TIMEOUT)
        except Exception as exc:
            log.info("下载文件 %s 失败：%r", att.filename, exc)
            return FileText(error="下载失败（文件可能已经被删了）", transient=True)
        if len(raw) > limit:
            return FileText(error=f"文件太大（{_size(len(raw))}），最多读 {_size(limit)}")
        async with self._file_slots:
            return await read_file(raw, att.filename, att.content_type, max_chars, self.ec.file_timeout_seconds)

    def _consume(self, st: ChannelState, batch: list[IncomingMessage], now: float) -> None:
        consumed = {id(m) for m in batch}
        st.pending = [m for m in st.pending if id(m) not in consumed]
        st.omitted = 0
        st.first_human_at = next((m.received_at for m in st.pending if not m.is_bot), None)
        st.last_eval_at = now
        st.error_until = 0.0

    async def _ask(
        self, st: ChannelState, kind: str, messages: list[dict[str, Any]], thinking: str, generation: int
    ) -> tuple[Completion, float, int]:
        """调用模型；被点名时显示“正在输入”，空回复（或推理把 token 用光）时关掉思考重试一次。"""
        if kind != "mention":
            return await self._call(st, kind, messages, thinking)
        async with self.transport.typing(st.channel_id):
            completion, cost, row = await self._call(st, kind, messages, thinking)
            empty = not completion.content.strip() or (
                completion.finish_reason == "length" and not parse_actions(completion.content)[1]
            )
            if empty and st.generation == generation:  # 记忆刚被清空就别再花一次钱了
                self.store.set_outcome(row, "empty")
                completion, cost, row = await self._call(st, kind, messages, "off")
        return completion, cost, row

    async def _call(
        self, st: ChannelState, kind: str, messages: list[dict[str, Any]], thinking: str
    ) -> tuple[Completion, float, int]:
        max_tokens = self.ec.max_output_tokens if thinking == "off" else self.ec.max_thinking_tokens
        completion = await self.llm.complete(
            messages, thinking=thinking, json_mode=True, max_tokens=max_tokens, user_id=f"guild-{st.guild_id}"
        )
        now = self.clock()
        cost = self.pricing.cost(completion.usage, now)
        self.ledger.add(st.guild_id, cost, now)
        u = completion.usage
        # 花了钱就立刻记账，后面出什么错数据库和内存账本都一致
        row = self.store.record_usage(
            ts=now,
            day=self.ledger.day(now),
            guild_id=st.guild_id,
            channel_id=st.channel_id,
            kind=kind,
            outcome="pending",
            hit=u.hit,
            miss=u.miss,
            output=u.output,
            reasoning=u.reasoning,
            cost=cost,
        )
        return completion, cost, row

    async def _on_error(
        self,
        st: ChannelState,
        kind: str,
        exc: LLMError,
        now: float,
        batch: list[IncomingMessage],
        targets: list[int],
        conv: Conversation,
        info: ChannelInfo,
    ) -> None:
        log.warning("频道 %s：DeepSeek 调用失败：%s", st.channel_id, exc)
        st.last_eval_at = now
        for m in batch:
            m.trigger = False  # 只清这一批的点名；调用期间新来的点名照常处理
        if exc.status == 402:
            self.paused_until = now + 600
            log.error("DeepSeek 余额不足，暂停 10 分钟。请到 platform.deepseek.com 充值。")
        elif exc.status == 401:
            self.paused_until = now + 3600
            log.error("DeepSeek API key 无效，暂停 1 小时。请检查 .env 里的 DEEPSEEK_API_KEY。")
        elif exc.status in BAD_REQUEST:
            # 这批消息本身被拒（比如内容审核）：丢掉它，别卡住整个频道
            log.error("频道 %s：这批消息被 DeepSeek 拒绝，已丢弃：%s", st.channel_id, exc)
            self._consume(st, batch, now)
            text = str(exc).lower()
            too_big = exc.status == 413 or any(word in text for word in ("context", "length", "token", "too large", "too many"))
            if too_big and st.conversation is conv:
                await self._compact(st, conv, info, force=True)
        else:
            st.error_until = now + ERROR_BACKOFF
        self.store.record_usage(
            ts=now,
            day=self.ledger.day(now),
            guild_id=st.guild_id,
            channel_id=st.channel_id,
            kind=kind,
            outcome="error",
        )
        st.last_decision = f"出错：{exc}"
        if kind == "mention" and targets:
            mid = st.msg_by_sid.get(targets[-1])
            if mid is not None:
                await self._safe_react(st, mid, "⚠️")

    async def _safe_react(self, st: ChannelState, message_id: int, emoji: str) -> bool:
        try:
            return await self.transport.react(st.channel_id, message_id, emoji)
        except Exception:
            log.exception("频道 %s：加表情出错", st.channel_id)
            return False

    async def _execute(
        self, st: ChannelState, actions: list[Action], kind: str, targets: list[int], elapsed: float
    ) -> tuple[bool, bool]:
        spoke = reacted = False
        sent_count = 0
        for action in actions:
            if action.kind == "react":
                mid = st.msg_by_sid.get(action.to) if action.to is not None else None
                if mid is not None and action.emoji:
                    reacted = await self._safe_react(st, mid, action.emoji) or reacted
                continue

            to = action.to
            if to is None and kind == "mention" and targets:
                to = targets[-1]
            reply_mid = st.msg_by_sid.get(to) if to is not None else None
            ping = kind == "mention" and to in targets
            for i, text in enumerate(action.messages):
                if sent_count >= MAX_MESSAGES_PER_ROUND:
                    break
                delay = typing_delay(
                    text,
                    self.ec.typing_chars_per_second,
                    self.ec.typing_min_seconds,
                    self.ec.typing_max_seconds,
                    self.rng,
                )
                if sent_count == 0:
                    delay = max(0.5, delay - elapsed)  # 模型生成的时间也算在“打字”里
                async with self.transport.typing(st.channel_id):
                    await self.sleep(delay)
                for j, chunk in enumerate(split_for_discord(text)):
                    first = i == 0 and j == 0
                    try:
                        sent = await self.transport.send(
                            st.channel_id, chunk, reply_to=reply_mid if first else None, ping=ping and first
                        )
                    except Exception:
                        log.exception("频道 %s：发消息出错", st.channel_id)
                        sent = None
                    if sent is not None:
                        spoke = True
                sent_count += 1
        return spoke, reacted

    async def _images(
        self, shown: list[IncomingMessage], triggers: list[IncomingMessage]
    ) -> dict[int, list[tuple[str, str]]]:
        out: dict[int, list[tuple[str, str]]] = {}
        budget = self.ec.max_images_per_round
        trigger_ids = {m.message_id for m in triggers}
        for m in reversed(shown):  # 优先最近的图
            for att in m.images:
                if budget <= 0:
                    return out
                wanted = m.message_id in trigger_ids  # 叫它的人发的图看清楚点，其他图看个大概
                side = self.ec.image_max_side if wanted else self.ec.low_detail_max_side
                uri = await self.image_loader(att, side)
                if uri is None:
                    continue
                budget -= 1
                out.setdefault(m.message_id, []).append((uri, "auto" if wanted else "low"))
        return out

    async def _load_image(self, att: ImageAttachment, max_side: int) -> str | None:
        if att.fetch is None or att.size > MAX_RAW_BYTES:
            return None
        try:
            raw = await asyncio.wait_for(att.fetch(), DOWNLOAD_TIMEOUT)
        except Exception as exc:
            log.info("下载图片失败：%r", exc)
            return None
        return await asyncio.to_thread(to_data_uri, raw, max_side)

    async def _compact(self, st: ChannelState, conv: Conversation, info: ChannelInfo, *, force: bool = False) -> None:
        """把最早的一部分对话压成摘要。只在这一次会让缓存 miss。"""
        remove = plan_compaction([t.content for t in conv.turns], force=force)
        if remove < 2:
            return
        generation = st.generation
        lines = []
        for t in conv.turns[:remove]:
            if t.role == "user":
                lines.append(content_text(t.content))
            else:
                raw = t.content if isinstance(t.content, str) else json.dumps(t.content, ensure_ascii=False)
                said = assistant_text(raw, info.bot_name)
                if said:
                    lines.append(said)
        prompt = [
            {"role": "system", "content": SUMMARY_SYSTEM.format(name=info.bot_name, limit=self.ec.summary_max_chars)},
            {
                "role": "user",
                "content": f"【旧摘要】\n{conv.summary or '（无）'}\n\n【聊天记录】\n" + "\n".join(lines),
            },
        ]
        now = self.clock()
        try:
            completion = await self.llm.complete(
                prompt, thinking="off", json_mode=False, max_tokens=2000, user_id=f"guild-{st.guild_id}"
            )
        except LLMError as exc:
            log.warning("频道 %s：压缩摘要失败：%s", st.channel_id, exc)
            return
        cost = self.pricing.cost(completion.usage, now)
        self.ledger.add(st.guild_id, cost, now)
        self.store.record_usage(
            ts=now,
            day=self.ledger.day(now),
            guild_id=st.guild_id,
            channel_id=st.channel_id,
            kind="summary",
            outcome="summary",
            hit=completion.usage.hit,
            miss=completion.usage.miss,
            output=completion.usage.output,
            cost=cost,
        )
        summary = completion.content.strip()
        if not summary or st.generation != generation:
            return
        last_removed = conv.turns[remove - 1].id
        self.store.compact(conv, summary[: self.ec.summary_max_chars * 2], remove)
        for entry in st.recent_files.values():
            # 文件内容随旧记录一起压成了摘要，被问到时要能重新读
            entry.read_turns = {k: v for k, v in entry.read_turns.items() if v > last_removed}
        log.info("频道 %s：已把 %d 条旧记录压成摘要", st.channel_id, remove)

    @staticmethod
    def _describe(kind: str, outcome: str, actions: list[Action], completion: Completion) -> str:
        if outcome == "reply":
            said = next((a.messages[0] for a in actions if a.kind == "reply" and a.messages), "")
            text = f"说了：{one_line(said, 40)}"
        elif outcome == "react":
            text = "加了表情 " + " ".join(a.emoji or "" for a in actions if a.kind == "react")
        else:
            text = "没说话"
        prefix = "被点名" if kind == "mention" else "主动判断"
        reason = one_line(completion.reasoning or "", 80)
        return f"{prefix}，{text}" + (f"（想法：{reason}）" if reason else "")

    # ------------------------------------------------------------------ 给命令用

    def reset_channel(self, channel_id: int) -> None:
        self.store.reset_conversation(channel_id)
        st = self.states.get(channel_id)
        if st is not None:
            st.generation += 1
            st.conversation = None
            for entry in st.recent_files.values():
                entry.read_turns.clear()  # 读过的内容随记忆一起清掉了
                entry.failed.clear()

    def status(
        self, guild_id: int, channel_id: int, parent_id: int | None, category_id: int | None = None
    ) -> dict[str, Any]:
        now = self.clock()
        st = self.states.get(channel_id)
        view = self.settings.view(guild_id, channel_id, parent_id, category_id)
        if st is not None:
            st.bucket.ready(view.quota_capacity)  # 先按当前上限截一下
        return {
            "tokens": st.bucket.tokens if st else float(view.quota_capacity),
            "turns_since_refill": st.bucket.turns_since_refill if st else 0,
            "pending": len(st.pending) if st else 0,
            "last_speak_ago": (now - st.last_speak_at) if st and st.last_speak_at else None,
            "last_decision": st.last_decision if st else "",
            "guild_spent": self.ledger.guild_spent(guild_id, now),
            "total_spent": self.ledger.total_spent(now),
            "paused": now < self.paused_until,
            "send_blocked": bool(st and st.send_failures >= SEND_FAIL_LIMIT and now < st.error_until),
            "day": self.ledger.day(now),
        }
