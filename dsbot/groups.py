"""服务器组：bot 主人用 /dsowner 服务器组 把几个服务器连成一组，当成一个服务器来记：组里共用一份人设卡和长期记忆，
开了记忆共享的频道也能翻组里别的服务器的记录。各服务器的设置、预算、人设还是各管各的。

- 组的人设卡、长期记忆按组的 ID（负数）存在原来的 cards.sqlite3、memory.sqlite3 里；哪个服务器在哪个组记在主数据库。
- 加入：这个服务器的长期记忆并进组里（一样的不重复）；人设卡组里没有的搬过去，两边都有的用组里那张、这边那张存成旧版本。
  两个都不在组里时新建一个组，“连到”的那个服务器的记忆就是组里原来的。组里记着每个人的卡在哪些服务器出现过。
- 退出：这个服务器带走一份组里的长期记忆，和在这个服务器出现过的人的人设卡（别的服务器的人管不到留在这里的卡，不带）；
  只在这个服务器出现过的人，卡跟着它走、从组里拿掉。组里只剩一个服务器时组就散了，剩下的那个服务器把组里的都留着。
- 人设卡、长期记忆在两个文件里，没法一次事务改完：每一步都可以重来。
  - 连起来：先记进组，再把它原来的并进来。组里的服务器自己名下不该有东西：有的话（中途断了）下次连、退时扫进组里。
  - 退出：先记成“正在退出”（这一刻起就不算组里的了），再复制、拿掉，最后才算完。没做完的，下次执行组命令、重启时接着做完。
  - 组操作期间三个文件每一步都等写进磁盘再做下一步（平时为了快不这样）：断电时停在哪一步，都和“做到这一步进程崩了”一样，
    下次能接着做完。不然断电后三个文件可能各停在不同的时刻，恢复时会删错。
  - 散掉的组在卡、记忆文件里还有数据（正常不会有：多半是只还原了其中一两个文件的备份）：不删，只报出来，交给 bot 主人。
- 所有服务器共用一张人设卡时（config.toml 的 [bot] shared_cards），调用方传 CardStore.for_groups()（SharedCardMarks）：
  卡不跟着搬，只照样搬删卡的记号。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


class GroupError(ValueError):
    """连不成、退不了（原因是给 bot 主人看的）。"""


@dataclass
class LinkPlan:
    joining: int  # 要加入组的服务器
    base: int  # 组里原来的那个服务器（新建组时就是“连到”的那个）
    group_id: int | None  # 加进哪个组；None = 新建一个
    members: list[int] = field(default_factory=list)  # 组里原来的服务器
    memories_added: int = 0  # 会并进去几条长期记忆
    memory_total: int = 0  # 并完组里一共几条
    cards_kept: int = 0  # 这边的卡会存成旧版本的人（两边都有卡、内容不一样；或者组里删掉了他的卡）
    cards_deleted: int = 0  # 其中组里删掉了他的卡（删得比这边那张新）的人：这边那张不顶上去
    renumbered: bool = False  # 加入的服务器的长期记忆会接在组里后面、重新编号

    def key(self) -> tuple[int, int, int | None]:
        """确认的时候照当时的情况再算一次，和给 bot 主人看的这份对不上就不办。"""
        return (self.joining, self.base, self.group_id)


@dataclass
class Linked:
    group_id: int
    joining: int
    members: list[int]  # 现在组里的服务器（按加入的先后）
    created: bool  # 新建的组
    cards_moved: int
    cards_kept: int
    memories_added: int
    memory_total: int
    renumbered: bool = False  # 加入的服务器的长期记忆接在组里后面、重新编号了


@dataclass
class UnlinkPlan:
    group_id: int
    rest: list[int]  # 退出之后组里剩下的服务器（只剩一个时组就散了）
    cards_taken: int  # 带走几张卡（在这个服务器出现过的人的；组散了的话剩下的那个服务器留着全部）
    cards_moved_out: int  # 其中只在这个服务器出现过的人：卡从组里拿掉
    memories: int  # 带走几条长期记忆（组里的全部）

    def key(self) -> tuple[int, tuple[int, ...]]:
        return (self.group_id, tuple(self.rest))


@dataclass
class Unlinked:
    group_id: int
    left: int
    members: list[int]  # 组里剩下的服务器（散了就是空的）
    dissolved: int | None  # 组散了：剩下的那个服务器（它把组里的留着自己用）
    cards_copied: int
    memories_copied: int
    cards_moved_out: int = 0


def group_name(group_id: int) -> str:
    return f"组 {-group_id}"


@contextmanager
def durable(*parts: Any) -> Iterator[None]:
    """组操作期间：三个数据库每次提交都等写进磁盘（synchronous=FULL），做完改回原来的设置。可以套着用。"""
    dbs = [db for db in (getattr(p, "db", None) for p in parts if p is not None) if db is not None]
    before = [int(db.execute("PRAGMA synchronous").fetchone()[0]) for db in dbs]
    for db in dbs:
        db.execute("PRAGMA synchronous=FULL")
    try:
        yield
    finally:
        for db, level in zip(dbs, before):
            db.execute(f"PRAGMA synchronous={level}")


def leftovers(store: Any, cards: Any, memory: Any) -> dict[int, tuple[int, int]]:
    """卡、记忆文件里还有数据、却不是现在哪个组（或者正在退出的组）的组 ID：{组: (人设卡几张, 长期记忆几条)}。
    正常不会有：多半是只还原了 cards.sqlite3 / memory.sqlite3（或者只还原、删掉了 dsbot.sqlite3）。不删：那可能是唯一的一份。"""
    alive = set(store.groups()) | {grp for _, grp in store.leaving()}
    found: dict[int, list[int]] = {}
    if cards is not None:
        for scope, n in cards.group_scopes().items():
            found.setdefault(scope, [0, 0])[0] = n
    if memory is not None:
        for scope, n in memory.group_scopes().items():
            found.setdefault(scope, [0, 0])[1] = n
    return {scope: (c, m) for scope, (c, m) in sorted(found.items(), reverse=True) if scope not in alive and (c or m)}


def _lowest_scope(cards: Any, memory: Any) -> int:
    """卡、记忆文件里用过的最小（最负）的组 ID（没有就是 0）：新组的编号要比它小，免得认走旧组留下的数据。"""
    return min([0] + [s.lowest_scope() for s in (cards, memory) if s is not None])


def plan_link(store: Any, cards: Any, memory: Any, guild_id: int, other_id: int) -> LinkPlan:
    """把 guild_id 连到 other_id 会怎样（不改任何东西）。连不了抛 GroupError。

    other_id 在组里：guild_id 加进那个组。guild_id 在组里、other_id 不在：other_id 加进 guild_id 的组。
    都不在：新建一个组，other_id 的记忆算组里原来的，guild_id 加进来。"""
    if guild_id == other_id:
        raise GroupError("两个是同一个服务器。")
    leaving = {g for g, _ in store.leaving()}
    if guild_id in leaving or other_id in leaving:
        raise GroupError("有一个服务器上次退出组还没做完：再执行一次命令（比如 `/dsowner 服务器组 查看`）就会接着做完。")
    mine, theirs = store.group_of(guild_id), store.group_of(other_id)
    if mine is not None and theirs is not None:
        if mine == theirs:
            raise GroupError(f"这两个服务器已经在同一组里了（{group_name(mine)}）。")
        raise GroupError(f"这两个服务器各在一个组里（{group_name(mine)}、{group_name(theirs)}）：先让其中一个退出它的组，再连。")
    if mine is not None:
        joining, base, group = other_id, guild_id, mine
    elif theirs is not None:
        joining, base, group = guild_id, other_id, theirs
    else:
        joining, base, group = guild_id, other_id, None
    target = group if group is not None else base  # 新建的组：并完就是 base 现在这些
    plan = LinkPlan(joining, base, group, store.group_members(group) if group is not None else [base])
    if memory is not None:
        plan.memories_added, plan.memory_total = memory.merge_preview(joining, target)
        plan.renumbered = bool(plan.memories_added) and not memory.keeps_numbers(target)
    if cards is not None:
        both, plan.cards_deleted = cards.overlap_parts(joining, target)
        plan.cards_kept = both + plan.cards_deleted
    return plan


def link(store: Any, cards: Any, memory: Any, guild_id: int, other_id: int, *, now: float) -> Linked:
    """把 guild_id 和 other_id 连成一组（规则见 plan_link）。连不了抛 GroupError。"""
    with durable(store, cards, memory):
        plan = plan_link(store, cards, memory, guild_id, other_id)
        created = plan.group_id is None
        if plan.group_id is None:
            group = store.new_group_id(lowest=_lowest_scope(cards, memory))
            store.join_group(plan.base, group, now)  # 先记进组：之后它的人设卡、长期记忆都记在组里
            _merge(cards, memory, plan.base, group, now, [plan.base, plan.joining])  # 组里原来的那个：原样搬过去，编号不变
        else:
            group = plan.group_id
        _sweep(store, cards, memory, group, now, coming=plan.joining)
        renumbered = memory is not None and not memory.keeps_numbers(group)
        store.join_group(plan.joining, group, now)  # 先记进组，再把它原来的并进来（中途断了，下次连、退时会扫进来）
        moved, kept, added = _merge(cards, memory, plan.joining, group, now, store.group_members(group))
        total = memory.count(group) if memory is not None else 0
        return Linked(group, plan.joining, store.group_members(group), created, moved, kept, added, total, renumbered and added > 0)


def _merge(
    cards: Any, memory: Any, guild_id: int, group_id: int, now: float, members: list[int] | None
) -> tuple[int, int, int]:
    """guild_id 的人设卡、长期记忆并进组里。members：并完以后组里的服务器（长期记忆记着是它们记的，照旧算它们的）；
    None：记着是谁记的都原样留着（见 _sweep）。"""
    moved, kept = cards.merge_scope(guild_id, group_id, now=now) if cards is not None else (0, 0)
    added = memory.merge_scope(guild_id, group_id, members=members) if memory is not None else 0
    return moved, kept, added


def _sweep(store: Any, cards: Any, memory: Any, group_id: int, now: float, coming: int | None = None) -> None:
    """组里的服务器自己名下不该有人设卡、长期记忆（都记在组里）：有的话（中途断了）并进组里。
    正在退出的不扫：它名下的是复制到一半的副本，复制时整个换掉。coming：马上要加进来的服务器（也算组里的）。
    组里只有一个服务器（连到一半就断了，recover 接着就把它散掉）：长期记忆记着是谁记的原样留着，下次再连时照常算
    （不然另一个服务器记的都算成它的，连好以后那边整理时改不了自己记的）。"""
    leaving = {g for g, _ in store.leaving()}
    members = store.group_members(group_id) + ([coming] if coming is not None else [])
    keep = coming is None and len(members) == 1
    for guild_id in store.group_members(group_id):
        if guild_id not in leaving:
            _merge(cards, memory, guild_id, group_id, now, None if keep else members)


def _leaver_cards(cards: Any, group_id: int, guild_id: int, rest: list[int]) -> tuple[set[int], set[int]]:
    """退出的服务器带走哪些人的卡、组里拿掉哪些人的卡。组里没别的服务器了（组散了）：全部留给它。"""
    if cards is None:
        return set(), set()
    everyone = cards.users(group_id)
    if not rest:
        return everyone, set()
    seen = cards.seen_in(group_id)
    taken = {u for u in everyone if guild_id in seen.get(u, ())}
    only = {u for u in taken if not seen.get(u, set()) & set(rest)}
    return taken, only


def plan_unlink(store: Any, cards: Any, memory: Any, guild_id: int) -> UnlinkPlan:
    """guild_id 退出它的组会怎样（不改任何东西）。不在组里抛 GroupError。"""
    group = store.group_of(guild_id)
    if group is None:
        raise GroupError("这个服务器不在任何组里。")
    rest = [g for g in store.group_members(group) if g != guild_id]
    taken, only = _leaver_cards(cards, group, guild_id, rest)
    memories = memory.count(group) if memory is not None else 0
    return UnlinkPlan(group, rest, len(taken), len(only), memories)


def unlink(store: Any, cards: Any, memory: Any, guild_id: int, *, now: float) -> Unlinked:
    """guild_id 退出它的组：带走一份组里的长期记忆和自己的人的人设卡。组里只剩一个服务器时组就散了。"""
    group = store.group_of(guild_id)
    if group is None:
        raise GroupError("这个服务器不在任何组里。")
    with durable(store, cards, memory):
        _sweep(store, cards, memory, group, now)  # 先把没并完的并进来，带走的才是全的
        plan = plan_unlink(store, cards, memory, guild_id)
        store.start_leaving(guild_id, now)  # 先记成“正在退出”：这一刻起就不算组里的了
        dissolved = plan.rest[0] if len(plan.rest) == 1 else None
        if dissolved is not None:
            store.start_leaving(dissolved, now)  # 只剩一个：组散了，它也一起“退出”，拿到组里的全部
        finish_leaving(store, cards, memory, now=now)
    return Unlinked(
        plan.group_id, guild_id, [] if dissolved is not None else plan.rest, dissolved, plan.cards_taken, plan.memories, plan.cards_moved_out
    )


def finish_leaving(store: Any, cards: Any, memory: Any, *, now: float) -> list[tuple[int, int]]:
    """把还没做完的退出做完。返回做完的 [(服务器, 组)]。每一步都可以重来：
    1. 还在组里（照常用组里那份）：把组里现在的长期记忆、在它那里出现过的人的卡、所有删卡的记号复制给它（整个换掉
       它名下的：那里只可能是上次复制到一半的）；复制完了才不算组里的（unmap）。中途出错，它照常用组里那份，期间记下的
       下次一起复制过去。
    2. 已经不在组里了（拿到了副本，之后它可能已经记了新东西，不能再复制）：只收尾——组里拿掉只在它那里出现过的人的卡；
       组里没别的服务器了就删掉组里那份。"""
    done: list[tuple[int, int]] = []
    for guild_id, group in store.leaving():
        mapped = store.group_of(guild_id)
        if mapped is not None and mapped != group:
            store.done_leaving(guild_id)  # 记录过时了（已经在别的组里）
            continue
        if mapped == group:
            rest = [g for g in store.group_members(group) if g != guild_id]
            taken, _ = _leaver_cards(cards, group, guild_id, rest)
            if cards is not None:
                cards.copy_scope(group, guild_id, users=taken)
            if memory is not None:
                memory.copy_scope(group, guild_id)
            store.unmap(guild_id)
        rest = store.group_members(group)  # 退出之后组里剩下的（一起退出、还没轮到的也在里面）
        _, only = _leaver_cards(cards, group, guild_id, rest)
        if cards is not None:
            cards.drop_users(group, only)
            cards.forget_seen_in(group, guild_id)
        if not rest:  # 最后一个也拿到了：组里那份不要了
            if cards is not None:
                cards.drop_scope(group)
            if memory is not None:
                memory.drop_scope(group)
        store.done_leaving(guild_id)
        done.append((guild_id, group))
    return done


def recover(store: Any, cards: Any, memory: Any, *, now: float) -> list[tuple[int, int]]:
    """上次没做完的连组、退组接着做完（每次执行组命令前、启动时）：退出的拿到副本；组里服务器自己名下剩下的扫进组里；
    只剩一个服务器的组散掉。返回这次做完的退出 [(服务器, 组)]。
    散掉的组在卡、记忆文件里剩下的数据这里不删（见 leftovers）：退出时最后一步已经删过了，还在就是文件之间对不上。"""
    with durable(store, cards, memory):
        done = finish_leaving(store, cards, memory, now=now)
        for group, members in store.groups().items():
            _sweep(store, cards, memory, group, now)
            if len(members) == 1:  # 连到一半就断了：一个服务器成不了组
                store.start_leaving(members[0], now)
        done += finish_leaving(store, cards, memory, now=now)
    return done
