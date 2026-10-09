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
from dataclasses import dataclass, field
from typing import Any, AsyncContextManager, Awaitable, Callable, Protocol

from .actions import dump_actions, parse_actions
from .config import AppConfig
from .images import MAX_RAW_BYTES, to_data_uri
from .ledger import Ledger
from .llm import LLMError
from .models import Action, ChannelInfo, Completion, ImageAttachment, IncomingMessage
from .prompts import SUMMARY_SYSTEM, build_system_prompt
from .quota import QuotaBucket, SlidingWindow, TurnTracker
from .render import assistant_text, content_text, render_round
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


@dataclass
class ChannelState:
    channel_id: int
    guild_id: int
    parent_id: int | None
    bucket: QuotaBucket
    pending: list[IncomingMessage] = field(default_factory=list)
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
        self.tz = cfg.bot.timezone
        self.pricing = cfg.pricing
        self.states: dict[int, ChannelState] = {}
        self.limiter = SlidingWindow()
        self.paused_until = 0.0  # 余额不足 / key 错误时全局暂停
        self._last_housekeep = 0.0
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ 输入

    def state(self, channel_id: int, guild_id: int, parent_id: int | None = None) -> ChannelState:
        st = self.states.get(channel_id)
        if st is None:
            view = self.settings.view(guild_id, channel_id, parent_id)
            st = ChannelState(channel_id, guild_id, parent_id, QuotaBucket(view.quota_capacity))
            self.states[channel_id] = st
        elif st.parent_id is None and parent_id is not None:
            st.parent_id = parent_id
        return st

    def view(self, st: ChannelState) -> View:
        return self.settings.view(st.guild_id, st.channel_id, st.parent_id)

    async def handle_message(self, msg: IncomingMessage) -> None:
        now = self.clock()
        msg.received_at = now
        st = self.state(msg.channel_id, msg.guild_id, msg.parent_id)
        view = self.view(st)

        st.pending.append(msg)
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
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

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
        if not view.proactive or st.first_human_at is None:
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
            if st.pending and (any(m.trigger for m in st.pending) or self._proactive_ready(st, self.clock())):
                batch = list(st.pending)
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

        conv = self._conversation(st)
        shown = self._select(batch)
        omitted = st.omitted + len(batch) - len(shown)
        numbered = self._number(st, conv, shown)
        shown_ids = {id(m) for m in shown}
        targets = [st.sid_by_msg[m.message_id] for m in triggers if id(m) in shown_ids]
        images = await self._images(shown, triggers) if view.vision else {}
        info = self.transport.channel_info(st.channel_id)
        system = build_system_prompt(
            guild=info.guild_name,
            channel=info.channel_name,
            name=info.bot_name,
            persona=view.persona,
            summary=conv.summary,
        )

        def build(imgs: dict[int, list[tuple[str, str]]]) -> tuple[Any, list[dict[str, Any]]]:
            content = render_round(
                numbered,
                sid_by_msg=st.sid_by_msg,
                kind=kind,
                targets=targets,
                now=now,
                tz=self.tz,
                omitted=omitted,
                images=imgs,
                max_chars=self.ec.max_chars_per_message,
            )
            msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
            msgs += [{"role": t.role, "content": t.content} for t in conv.turns]
            msgs.append({"role": "user", "content": content})
            return content, msgs

        user_content, messages = build(images)
        thinking = view.mention_thinking if kind == "mention" else view.proactive_thinking
        started = self.clock()
        try:
            try:
                completion, cost, row = await self._ask(st, kind, messages, thinking)
            except LLMError as exc:
                if exc.status not in BAD_REQUEST or not images:
                    raise
                log.warning("频道 %s：带图片的请求被拒（%s），去掉图片再试一次", st.channel_id, exc)
                images = {}
                user_content, messages = build(images)
                completion, cost, row = await self._ask(st, kind, messages, thinking)
        except LLMError as exc:
            await self._on_error(st, kind, exc, now, batch, targets, conv, info)
            return

        actions, ok = parse_actions(completion.content)
        if not ok:
            log.warning("频道 %s：模型输出不是合法 JSON：%s", st.channel_id, one_line(completion.content, 200))
            text = completion.content.strip()
            if kind == "mention" and text and not text.startswith(("{", "[")):
                actions = [Action("reply", targets[-1] if targets else None, [text[:1800]])]
        stored = completion.content if ok else dump_actions(actions)

        if st.generation != generation:
            log.info("频道 %s：这一轮进行中记忆被清空，丢弃结果", st.channel_id)
            self.store.set_outcome(row, "discarded")
            self._consume(st, batch, now)
            return

        # 先把这一轮写进历史（只追加），再消费缓冲区
        self.store.append_round(conv, user_content, stored, [(sid, m.message_id) for sid, m in numbered], now=now)
        self._consume(st, batch, now)

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
            await self._compact(st, conv, info)

    def _consume(self, st: ChannelState, batch: list[IncomingMessage], now: float) -> None:
        consumed = {id(m) for m in batch}
        st.pending = [m for m in st.pending if id(m) not in consumed]
        st.omitted = 0
        st.first_human_at = next((m.received_at for m in st.pending if not m.is_bot), None)
        st.last_eval_at = now
        st.error_until = 0.0

    async def _ask(
        self, st: ChannelState, kind: str, messages: list[dict[str, Any]], thinking: str
    ) -> tuple[Completion, float, int]:
        """调用模型；被点名时显示“正在输入”，空回复（或推理把 token 用光）时关掉思考重试一次。"""
        if kind != "mention":
            return await self._call(st, kind, messages, thinking)
        async with self.transport.typing(st.channel_id):
            completion, cost, row = await self._call(st, kind, messages, thinking)
            if not completion.content.strip() or (
                completion.finish_reason == "length" and not parse_actions(completion.content)[1]
            ):
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
            if any(word in text for word in ("context", "length", "token")) and st.conversation is conv:
                await self._compact(st, conv, info)
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
            raw = await att.fetch()
        except Exception as exc:
            log.info("下载图片失败：%r", exc)
            return None
        return await asyncio.to_thread(to_data_uri, raw, max_side)

    async def _compact(self, st: ChannelState, conv: Conversation, info: ChannelInfo) -> None:
        """把最早的一半对话压成摘要。只在这一次会让缓存 miss。"""
        pairs = len(conv.turns) // 2
        if pairs < 2:
            return
        generation = st.generation
        remove = (pairs // 2) * 2
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
        self.store.compact(conv, summary[: self.ec.summary_max_chars * 2], remove)
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

    def status(self, guild_id: int, channel_id: int, parent_id: int | None) -> dict[str, Any]:
        now = self.clock()
        st = self.states.get(channel_id)
        view = self.settings.view(guild_id, channel_id, parent_id)
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
