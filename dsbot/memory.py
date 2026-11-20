"""人设卡和长期记忆：各存一个 SQLite 文件（默认 data/cards.sqlite3、data/memory.sqlite3），和聊天记录分开，方便备份、搬家。

- 人设卡：每个服务器里每人一张，记本人说的、关于自己的事（称呼、设定、喜好、梗）。管理员看不到；
  本人用 /ds-rp 查看、修改、清空；bot 主人只能按用户 ID 整个删掉（看不到内容）。每人保留最近几个旧版本。
- 长期记忆：每个服务器一份带编号的清单，记群里的事（约定、大事、长期的梗、进行中的剧情）。
- 私信记忆：每人一份带编号的清单（不分服务器，跟着人走），记私信里聊到的、他和 bot 之间的事。每条标着敏感不敏感：
  敏感的只在他自己的私信里用；不敏感的，他在服务器里叫 bot 时也会带上。本人用 /ds-dm 记忆 查看、删除、清空。

人设卡、长期记忆的 guild_id 一栏其实是“记忆归属”：连成一组的服务器（groups.py）共用一份，按组的 ID（负数）存。
config.toml 里 [bot] shared_cards = true 时，人设卡每人只有一张，所有服务器共用，记在 SHARED（0）下面（长期记忆照旧）。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from typing import Any

from .store import open_private_db

CARD_VERSIONS_KEEP = 5  # 每人保留几个旧版本（写坏了能找回）
ANY = object()  # put 的 expect 不填：不管现在是哪个版本都写
SHARED = 0  # 所有服务器共用一张人设卡时，卡记在这个 ID 下面（和 bot 主人删卡的记号一样，0 就是“所有服务器”）

CARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,           -- 最近一次看到的名字（按名字查人设卡时用）
    text TEXT NOT NULL,
    updated_at REAL NOT NULL,
    via TEXT NOT NULL,            -- chat（聊天里让它记）/ review（整理记忆）/ command（/ds-rp）/ restore（改回上一版）
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS card_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,           -- 被换掉的旧版本
    replaced_at REAL NOT NULL,
    merged INTEGER NOT NULL DEFAULT 0  -- 1 = 从别的服务器并进来的（连成一组、改成所有服务器共用一张时；不是这张卡以前的样子）：改回它时现在这张存成旧版本
);
CREATE INDEX IF NOT EXISTS idx_card_versions ON card_versions (guild_id, user_id, id);
CREATE TABLE IF NOT EXISTS card_tombstones (
    guild_id INTEGER NOT NULL,    -- 0 = 所有服务器（bot 主人删的）
    user_id INTEGER NOT NULL,
    deleted_at REAL NOT NULL,     -- 整理记忆时，这之前的聊天不再拿来写这个人的卡（免得删掉的又被“想起来”）
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS card_seen (
    guild_id INTEGER NOT NULL,    -- 服务器组（组的 ID）：只有连成一组时才记；所有服务器共用一张卡时是 0
    user_id INTEGER NOT NULL,
    seen_in INTEGER NOT NULL,     -- 这个人的卡在组里哪个服务器用过、写过（退出组时只带走在那里出现过的人的卡）
    name TEXT NOT NULL,           -- 他在那个服务器的名字（各服务器的昵称可能不一样：@ 人、按名字查卡时用）
    PRIMARY KEY (guild_id, user_id, seen_in)
);
CREATE TABLE IF NOT EXISTS card_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL           -- shared：1 = 所有服务器共用一张（卡都记在 0 下面），0 或者没有 = 每个服务器一张
);
"""

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    guild_id INTEGER NOT NULL,
    no INTEGER NOT NULL,          -- 这个服务器里的编号（删掉的不再用，免得改错）
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    via TEXT NOT NULL,            -- chat / review / command
    origin INTEGER,               -- 哪个服务器记的（连成一组时：整理记忆只动自己服务器记的）；NULL = 不知道（旧数据）
    PRIMARY KEY (guild_id, no)
);
CREATE TABLE IF NOT EXISTS memory_seq (
    guild_id INTEGER PRIMARY KEY,
    next_no INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dm_memories (
    user_id INTEGER NOT NULL,
    no INTEGER NOT NULL,          -- 这个人的编号（删掉的不再用，免得改错）
    text TEXT NOT NULL,
    sensitive INTEGER NOT NULL,   -- 1 = 敏感：只在他自己的私信里用
    shared_as TEXT,               -- 不是 NULL = 可共享：他本人同意它在服务器里自己提起（存的是那时他的名字）
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    via TEXT NOT NULL,            -- chat / review / command
    PRIMARY KEY (user_id, no)
);
CREATE TABLE IF NOT EXISTS dm_memory_seq (
    user_id INTEGER PRIMARY KEY,
    next_no INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dm_memory_cleared (
    user_id INTEGER PRIMARY KEY,
    cleared_at REAL NOT NULL      -- 整理记忆时，这之前的聊天不再拿来写他的私信记忆（免得清掉的又被“想起来”）
);
"""


@dataclass
class Card:
    guild_id: int
    user_id: int
    name: str
    text: str
    updated_at: float
    via: str


@dataclass
class Memory:
    no: int
    text: str
    updated_at: float
    via: str
    origin: int | None = None  # 哪个服务器记的（None = 不知道）


@dataclass
class DmMemory:
    no: int
    text: str
    sensitive: bool
    updated_at: float
    via: str
    shared_as: str | None = None  # 可共享：他本人标的（用 /ds-dm 记忆），值是标的时候他的名字；敏感的不会是可共享


@dataclass
class ShareChange:
    """改成（或者改回）所有服务器共用一张人设卡：做了什么（写日志用）。"""

    shared: bool  # 现在是不是共用
    people: int  # 并了、分了几个人的卡
    merged: int = 0  # 并成一张时：几个地方各有一张卡的人（最新的那张当现在的卡，别的存成旧版本）
    stayed: int = 0  # 分回去时：没在任何服务器出现过的人（卡留在共用的那份里）


@dataclass
class _Bundle:
    """一个人在某处（服务器、组、共用的那份）的卡：现在的卡、旧版本（从早到晚）、他的卡最近一次“所有服务器都删了”的时间。"""

    scope: int
    card: Card | None
    versions: list[tuple[str, float, int]] = field(default_factory=list)  # (内容, 换掉的时间, merged)
    dead: float | None = None

    def empty(self) -> bool:
        return self.card is None and not self.versions


def _combine(bundles: list[_Bundle], now: float) -> tuple[Card | None, list[tuple[str, float, int]]]:
    """一个人的几份卡并成一份（改成所有服务器共用、改回每个服务器一张时用）。返回 (现在的卡, 旧版本：从早到晚)。

    现在的卡：最新的那张（一样新的取前面那份）。比 dead（“所有服务器都删了”的记号）旧的不算，免得删掉的又顶上来；各处自己的
    删卡记号不看：那里有卡就是在用的（删卡会把卡拿掉），比卡新的记号是连组、退组、分回各服务器时从别处带过来的。
    别的卡存成旧版本，记成从别处并进来的，排在最近（/ds-rp 改回上一版先换到它们，换过去以后再改回上一版就换回来）；
    然后是现在这张自己的旧版本（原样，只去掉和别的服务器那张一样的），最后是别处的旧版本（和前面留下的一样的不重复留）。
    一共最多 CARD_VERSIONS_KEEP 个。没有能用的卡（都删了）：旧版本和不算数的卡照时间先后留最近的几个（是他自己的历史，标记原样）。"""
    live = [b for b in bundles if b.card is not None and (b.dead is None or b.card.updated_at > b.dead)]
    winner = max(live, key=lambda b: b.card.updated_at if b.card is not None else 0.0) if live else None
    if winner is None or winner.card is None:
        if len(bundles) == 1 and bundles[0].card is None:
            return None, list(bundles[0].versions)  # 只有一份、只剩旧版本：原样
        pool = [(b.card.updated_at, b.card.text, now, 0) for b in bundles if b.card is not None]
        pool += [(at, text, at, merged) for b in bundles for text, at, merged in b.versions]
        texts: set[str] = set()
        newest: list[tuple[str, float, int]] = []
        for _, text, at, merged in sorted(pool, key=lambda v: v[0], reverse=True):
            if text not in texts:
                texts.add(text)
                newest.append((text, at, merged))
        return None, list(reversed(newest[:CARD_VERSIONS_KEEP]))
    top = winner.card
    texts = {top.text}
    order: list[tuple[str, float, int]] = []  # 改回上一版先回到的在前
    for b in sorted((b for b in bundles if b is not winner and b.card is not None), key=lambda b: -b.card.updated_at):  # type: ignore[union-attr]
        assert b.card is not None
        if b.card.text not in texts:
            texts.add(b.card.text)
            order.append((b.card.text, now, 1))
    elsewhere = set(texts) - {top.text}  # 别的服务器的卡
    for text, at, merged in reversed(winner.versions):
        # 现在这张自己的旧版本：原样留着（只有一份卡时一点不动），只去掉和别的服务器那张一样的（那张已经留了）
        if text not in elsewhere:
            texts.add(text)
            order.append((text, at, merged))
    for text, at, _ in sorted((v for b in bundles if b is not winner for v in b.versions), key=lambda v: -v[1]):
        if text not in texts:
            texts.add(text)
            order.append((text, at, 1))
    return top, list(reversed(order[:CARD_VERSIONS_KEEP]))


def clean_card(text: str) -> str:
    """人设卡正文：统一换行、去掉首尾空白和多余的空行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def clean_memory(text: str) -> str:
    """一条长期记忆：一行写完。"""
    return " ".join(text.split())


class CardStore:
    def __init__(self, path: str):
        self.path = str(path)
        self.db = open_private_db(self.path, CARD_SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(card_versions)")}
        if "merged" not in cols:  # 旧版本建的表：补上这一列（原有的都是卡自己以前的样子）
            self.db.execute("ALTER TABLE card_versions ADD COLUMN merged INTEGER NOT NULL DEFAULT 0")
            self.db.commit()
        row = self.db.execute("SELECT value FROM card_meta WHERE key = 'shared'").fetchone()
        # 所有服务器共用一张卡（卡都记在 SHARED 下面）：按文件里记的算，config.toml 改了要等 set_shared 换过来才算
        self.shared = row is not None and row["value"] == "1"

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _card(row: Any) -> Card:
        return Card(row["guild_id"], row["user_id"], row["name"], row["text"], row["updated_at"], row["via"])

    def get(self, guild_id: int, user_id: int) -> Card | None:
        row = self.db.execute("SELECT * FROM cards WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone()
        return self._card(row) if row is not None else None

    def put(
        self, guild_id: int, user_id: int, name: str, text: str, *, via: str, now: float | None = None, expect: Any = ANY
    ) -> bool:
        """写入新的人设卡（旧的那份存成旧版本）。返回有没有改动。

        text 是空的：删掉这张卡，但旧版本留着（还能“改回上一版”）。连旧版本一起删用 delete。
        expect：写之前这张卡应该是哪个版本（updated_at；None = 应该还没有卡）。对不上（别人刚改过、刚删掉）就不写。"""
        now = time.time() if now is None else now
        text = clean_card(text)
        old = self.get(guild_id, user_id)
        if expect is not ANY and (old.updated_at if old is not None else None) != expect:
            return False
        if not text:
            if old is None:
                return False
            with self.db:
                self.db.execute(
                    "INSERT INTO card_versions (guild_id, user_id, text, replaced_at) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, old.text, now),
                )
                self._trim_versions(guild_id, user_id)
                self.db.execute("DELETE FROM cards WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
                self._tombstone(guild_id, user_id, now)
            return True
        if old is not None and old.text == text:
            if old.name != name and name:
                self.touch_name(guild_id, user_id, name)
            return False
        with self.db:
            if old is not None:
                self.db.execute(
                    "INSERT INTO card_versions (guild_id, user_id, text, replaced_at) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, old.text, now),
                )
                self._trim_versions(guild_id, user_id)
            self.db.execute(
                "INSERT INTO cards (guild_id, user_id, name, text, updated_at, via) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET name = excluded.name, text = excluded.text, "
                "updated_at = excluded.updated_at, via = excluded.via",
                (guild_id, user_id, name or (old.name if old else ""), text, now, via),
            )
        return True

    def _tombstone(self, guild_id: int, user_id: int, now: float) -> None:
        self.db.execute(
            "INSERT INTO card_tombstones (guild_id, user_id, deleted_at) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET deleted_at = excluded.deleted_at",
            (guild_id, user_id, now),
        )

    def deleted_at(self, guild_id: int, user_id: int) -> float | None:
        """这个人的卡最近一次被删掉的时间（本人清空、在聊天里让它忘掉、bot 主人删的）。没删过返回 None。"""
        row = self.db.execute(
            "SELECT MAX(deleted_at) AS t FROM card_tombstones WHERE user_id = ? AND guild_id IN (?, 0)", (user_id, guild_id)
        ).fetchone()
        return row["t"] if row is not None else None

    def forgotten_at(self, guild_id: int, user_id: int) -> float | None:
        """整理记忆时，这之前的聊天不再拿来写他的卡。一般就是 deleted_at；所有服务器共用一张卡时（guild_id 是 SHARED），
        他在哪里删过卡都算：改成共用之前在某个服务器删掉的，那边删之前的聊天也别拿来写这张共用的卡。"""
        if guild_id != SHARED:
            return self.deleted_at(guild_id, user_id)
        row = self.db.execute("SELECT MAX(deleted_at) AS t FROM card_tombstones WHERE user_id = ?", (user_id,)).fetchone()
        return row["t"] if row is not None else None

    def _trim_versions(self, guild_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM card_versions WHERE guild_id = ? AND user_id = ? AND id NOT IN "
            "(SELECT id FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?)",
            (guild_id, user_id, guild_id, user_id, CARD_VERSIONS_KEEP),
        )

    def restore_previous(self, guild_id: int, user_id: int, *, name: str = "", now: float | None = None) -> str | None:
        """改回上一个版本（现在这份就不要了；刚删掉的也能找回来）。没有旧版本时返回 None。
        例外：那个旧版本是从别的服务器并进来的（连成一组、改成所有服务器共用一张时；不是这张卡以前的样子）：现在这张存成旧版本，
        再改回上一版就换回来。"""
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT id, text, merged FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id, user_id),
        ).fetchone()
        if row is None:
            return None
        current = self.get(guild_id, user_id)
        with self.db:
            self.db.execute("DELETE FROM card_versions WHERE id = ?", (row["id"],))
            if current is not None and row["merged"]:
                self.db.execute(
                    "INSERT INTO card_versions (guild_id, user_id, text, replaced_at) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, current.text, now),
                )
                self._trim_versions(guild_id, user_id)
            self.db.execute(
                "INSERT INTO cards (guild_id, user_id, name, text, updated_at, via) VALUES (?, ?, ?, ?, ?, 'restore') "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at, "
                "via = excluded.via",
                (guild_id, user_id, (current.name if current else "") or name, row["text"], now),
            )
        return row["text"]

    def touch_name(self, guild_id: int, user_id: int, name: str) -> None:
        """记下这个人现在的名字（改了昵称也能按新名字查到）。"""
        if not name:
            return
        with self.db:
            self.db.execute(
                "UPDATE cards SET name = ? WHERE guild_id = ? AND user_id = ? AND name != ?", (name, guild_id, user_id, name)
            )

    def delete(self, guild_id: int, user_id: int, *, now: float | None = None) -> bool:
        """删掉这个人在这个服务器的卡，连同旧版本（/ds-rp 清空）。返回原来有没有东西。"""
        now = time.time() if now is None else now
        with self.db:
            cur = self.db.execute("DELETE FROM cards WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            old = self.db.execute("DELETE FROM card_versions WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            self.db.execute("DELETE FROM card_seen WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            self._tombstone(guild_id, user_id, now)
        return cur.rowcount > 0 or old.rowcount > 0

    def delete_user(self, user_id: int, *, now: float | None = None) -> tuple[int, int]:
        """删掉这个人在所有服务器的卡，连同旧版本（bot 主人用）。返回 (删了几张卡, 删了几个旧版本)。"""
        now = time.time() if now is None else now
        with self.db:
            cur = self.db.execute("DELETE FROM cards WHERE user_id = ?", (user_id,))
            old = self.db.execute("DELETE FROM card_versions WHERE user_id = ?", (user_id,))
            self.db.execute("DELETE FROM card_seen WHERE user_id = ?", (user_id,))
            self._tombstone(0, user_id, now)
        return cur.rowcount, old.rowcount

    def names(self, guild_id: int) -> list[str]:
        """这个服务器里有人设卡的人的名字（排好序，提示词里列出来，变了才影响缓存）。"""
        rows = self.db.execute("SELECT name FROM cards WHERE guild_id = ? AND name != ''", (guild_id,)).fetchall()
        return sorted({r["name"] for r in rows})

    def count(self, guild_id: int) -> int:
        return int(self.db.execute("SELECT COUNT(*) AS n FROM cards WHERE guild_id = ?", (guild_id,)).fetchone()["n"])

    def own_texts(self, guild_id: int, user_id: int) -> list[str]:
        """这个人自己的卡：现在这张和留着的旧版本（改自己的卡时，本来就在自己卡上的话不算抄别人的）。"""
        rows = self.db.execute(
            "SELECT text FROM cards WHERE guild_id = ? AND user_id = ? "
            "UNION ALL SELECT text FROM card_versions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id, guild_id, user_id),
        ).fetchall()
        return [r["text"] for r in rows]

    def others(self, guild_id: int, user_id: int) -> list[str]:
        """这个服务器里别人的人设卡原文（写某个人的卡时核对：别把别人的卡抄进来，抄进来本人用 /ds-rp 就能看到）。"""
        rows = self.db.execute("SELECT text FROM cards WHERE guild_id = ? AND user_id != ?", (guild_id, user_id)).fetchall()
        return [r["text"] for r in rows]

    def owners(self, guild_id: int) -> list[tuple[int, str]]:
        """这个服务器里有人设卡的人：[(用户 ID, 卡上记的名字)]（它在话里 @ 人时用）。"""
        rows = self.db.execute("SELECT user_id, name FROM cards WHERE guild_id = ? AND name != ''", (guild_id,)).fetchall()
        return [(r["user_id"], r["name"]) for r in rows]

    def find(self, guild_id: int, wanted: str, *, limit: int = 3) -> list[Card]:
        """按名字找人设卡：先找名字完全一样的（不分大小写），没有再找名字里包含它的。
        连成一组的服务器：他在组里各个服务器的名字（昵称可能不一样）都算。"""
        key = " ".join(wanted.split()).lstrip("@＠").strip().casefold()
        if not key:
            return []
        rows = [self._card(r) for r in self.db.execute("SELECT * FROM cards WHERE guild_id = ?", (guild_id,))]
        aliases: dict[int, set[str]] = {}
        for r in self.db.execute("SELECT user_id, name FROM card_seen WHERE guild_id = ? AND name != ''", (guild_id,)):
            aliases.setdefault(r["user_id"], set()).add(r["name"].casefold())

        def names(card: Card) -> set[str]:
            return {n for n in (card.name.casefold(), *aliases.get(card.user_id, ())) if n}

        exact = [c for c in rows if key in names(c)]
        if exact:
            return exact[:limit]
        return [c for c in rows if any(key in n or (len(n) >= 2 and n in key) for n in names(c))][:limit]

    def find_here(
        self, scope: int, wanted: str, here: int, *, limit: int = 3, near: Collection[int] | None = None
    ) -> list[tuple[Card, str, str, bool]]:
        """卡里有别的服务器的人时（连成一组、所有服务器共用一张）按名字找人设卡（在 here 这个服务器问的）：
        [(卡, 给模型看的名字, 说明, 在这里的频道出现过)]。
        不同服务器可能有同名的不同的人，按这个顺序找，前面的找到了就不往后找：
        在这里出现过的人按他在这里的名字 → 按他在组里别的服务器的名字（给模型看的还是他在这里的名字）→ 只在私信里出现过的人
        （不知道他在这里叫什么，按他在各服务器的名字）→ 还没在这里出现过的人（说明里写清楚，别安到这里同名的人头上）。
        先在所有这几档里找名字完全一样的，没有再找包含的（免得这里的“小明明”盖过别的服务器的“小明”）。
        near：除了 here，还算哪些服务器的人（所有服务器共用一张卡时是组里别的服务器，没连组就是空的：不相干的服务器的人
        在这里查不到，他们在那边的名字也不算）；None = 卡里的人都算（连成一组时，组里那份就是这几个服务器的）。"""
        key = " ".join(wanted.split()).lstrip("@＠").strip().casefold()
        if not key:
            return []
        rows = [self._card(r) for r in self.db.execute("SELECT * FROM cards WHERE guild_id = ? ORDER BY user_id", (scope,))]
        here_names: dict[int, str] = {}
        aliases: dict[int, dict[str, str]] = {}  # 人 -> {名字（不分大小写）: 原样}
        nearby: set[int] = set()  # 在 near 里的服务器出现过的人
        for r in self.db.execute("SELECT user_id, seen_in, name FROM card_seen WHERE guild_id = ? ORDER BY seen_in", (scope,)):
            if r["seen_in"] == here:
                here_names[r["user_id"]] = r["name"]
            elif near is not None and r["seen_in"] not in near:
                continue  # 不相干的服务器：他在那边出现过、叫什么都不算
            else:
                nearby.add(r["user_id"])
            if r["name"]:
                aliases.setdefault(r["user_id"], {}).setdefault(r["name"].casefold(), r["name"])

        def every(card: Card) -> dict[str, str]:
            names = {card.name.casefold(): card.name} if card.name else {}
            return {**names, **aliases.get(card.user_id, {})}

        def known(card: Card) -> dict[str, str]:
            # 所有服务器共用一张卡时：卡上的名字可能是他在不相干的服务器里的昵称，只认在这里、组里记下的名字
            return dict(aliases.get(card.user_id, {}))

        def own(card: Card) -> dict[str, str]:
            return {here_names[card.user_id].casefold(): here_names[card.user_id]}

        wide = every if near is None else known
        named = [c for c in rows if here_names.get(c.user_id)]
        dm_only = [c for c in rows if c.user_id in here_names and not here_names[c.user_id]]
        others = [c for c in rows if c.user_id not in here_names and (near is None or c.user_id in nearby)]
        tiers: list[tuple[list[Card], Any, str, bool]] = [
            (named, own, "", True),
            (named, wide, "（这是他在这里的名字；你查的是他在别的服务器用的名字）", True),
            # 只在私信里出现过：不记成“卡已经附过了”（那份写着不知道他在这里叫什么，他来频道说话时照常附上）。
            # 不知道他在这里叫什么，只能连卡上的名字一起认
            (dm_only, every, "（他只在私信里跟你聊过，不知道他在这里叫什么，这是他在别的服务器用过的名字）", False),
            # 所有服务器共用一张卡时：组里别的服务器的人按他在那边记下的名字；在那边只在私信里出现过（没记名字）的查不到
            # （卡上的名字可能是不相干的服务器里的昵称），提示词里也不算（owners_near）
            (others, wide, "（还没在这个服务器出现过：是别的服务器记下的人，别当成这里同名的人）", False),
        ]
        for exact in (True, False):
            for people, names, note, local in tiers:
                hits: list[tuple[Card, str]] = []
                for c in people:
                    forms = names(c)
                    if exact and key in forms:
                        hits.append((c, forms[key]))
                    elif not exact and any(key in n or (len(n) >= 2 and n in key) for n in forms):
                        first = next(iter(forms.values()), "")
                        hits.append((c, (c.name or first) if near is None else (first or c.name)))
                if hits:
                    return [
                        (c, here_names[c.user_id] if here_names.get(c.user_id) else shown, note, local) for c, shown in hits[:limit]
                    ]
        return []

    def owners_near(self, scope: int, here: int, near: Collection[int]) -> set[int]:
        """所有服务器共用一张卡时，在这里查得到、现在有卡的人：在 here 出现过的（只在私信里出现过的也算），和在 near
        （组里别的服务器）的频道里出现过、记着名字的（在那边只在私信里出现过的查不到，也不算）。"""
        guilds = sorted(set(near) - {here})
        marks = ",".join("?" * len(guilds))
        near_part = f" OR (s.seen_in IN ({marks}) AND s.name != '')" if guilds else ""
        rows = self.db.execute(
            "SELECT DISTINCT s.user_id FROM card_seen s JOIN cards c ON c.guild_id = s.guild_id AND c.user_id = s.user_id "
            f"WHERE s.guild_id = ? AND (s.seen_in = ?{near_part})",
            (scope, here, *guilds),
        ).fetchall()
        return {r["user_id"] for r in rows}

    def named_here(self, scope: int, guild_id: int) -> list[tuple[int, str]]:
        """组里在 guild_id 出现过、记着他在那里的名字、有人设卡的人：[(用户 ID, 他在那里的名字)]（提示词里“有卡的人”）。
        只在私信里出现过的不算（不知道他在那里叫什么：卡上的名字可能是别的服务器的昵称）。"""
        rows = self.db.execute(
            "SELECT s.user_id, s.name FROM card_seen s JOIN cards c ON c.guild_id = s.guild_id AND c.user_id = s.user_id "
            "WHERE s.guild_id = ? AND s.seen_in = ? AND s.name != ''",
            (scope, guild_id),
        ).fetchall()
        return [(r["user_id"], r["name"]) for r in rows]

    def note_seen(self, scope: int, user_id: int, guild_id: int, name: str) -> None:
        """连成一组时：这个人的卡在组里的 guild_id 用过、写过（name：他在那里的名字）。名字没变就不写。"""
        row = self.db.execute(
            "SELECT name FROM card_seen WHERE guild_id = ? AND user_id = ? AND seen_in = ?", (scope, user_id, guild_id)
        ).fetchone()
        if row is not None and (row["name"] == name or not name):
            return
        with self.db:
            self.db.execute(
                "INSERT INTO card_seen (guild_id, user_id, seen_in, name) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id, seen_in) DO UPDATE SET name = excluded.name",
                (scope, user_id, guild_id, name or ""),
            )

    def seen_in(self, scope: int) -> dict[int, set[int]]:
        """组里每个人的卡在哪些服务器出现过：{人: {服务器}}。"""
        out: dict[int, set[int]] = {}
        for r in self.db.execute("SELECT user_id, seen_in FROM card_seen WHERE guild_id = ?", (scope,)):
            out.setdefault(r["user_id"], set()).add(r["seen_in"])
        return out

    def owners_seen(self, scope: int, guild_id: int) -> list[tuple[int, str]]:
        """组里在 guild_id 出现过、有人设卡的人：[(用户 ID, 他在那里的名字)]（在那个服务器里 @ 人时用）。
        只在私信里出现过的（没记名字）用卡上的名字。"""
        rows = self.db.execute(
            "SELECT s.user_id, CASE WHEN s.name != '' THEN s.name ELSE c.name END AS name FROM card_seen s "
            "JOIN cards c ON c.guild_id = s.guild_id AND c.user_id = s.user_id WHERE s.guild_id = ? AND s.seen_in = ?",
            (scope, guild_id),
        ).fetchall()
        return [(r["user_id"], r["name"]) for r in rows if r["name"]]

    def users(self, scope: int) -> set[int]:
        """有卡（或者只剩旧版本）的人。"""
        rows = self.db.execute(
            "SELECT user_id FROM cards WHERE guild_id = ? UNION SELECT user_id FROM card_versions WHERE guild_id = ?",
            (scope, scope),
        ).fetchall()
        return {r["user_id"] for r in rows}

    # ---------------------------------------------------------------- 服务器组（scope：服务器 ID 或组的 ID）

    def overlap(self, src: int, dst: int) -> int:
        """并进去时 src 那张卡会存成旧版本的人数（见 overlap_parts）。"""
        return sum(self.overlap_parts(src, dst))

    def overlap_parts(self, src: int, dst: int) -> tuple[int, int]:
        """并进去时 src 那张卡会存成旧版本的人数（连成一组之前告诉 bot 主人）：
        (两边都有卡、内容又不一样的：用组里那张；组里删掉了他的卡、而且删得比 src 那张新的：不顶上去)。"""
        both = deleted = 0
        for mine in (self._card(r) for r in self.db.execute("SELECT * FROM cards WHERE guild_id = ?", (src,)).fetchall()):
            theirs = self.get(dst, mine.user_id)
            if theirs is not None:
                both += theirs.text != mine.text
            else:
                dead = self.deleted_at(dst, mine.user_id)
                deleted += dead is not None and dead > mine.updated_at
        return both, deleted

    def swaps_on_restore(self, guild_id: int, user_id: int) -> bool:
        """改回上一版会不会是“换”：最新的旧版本是连成一组时从别处并进来的，现在又有卡（现在这张会存成旧版本）。"""
        row = self.db.execute(
            "SELECT merged FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1", (guild_id, user_id)
        ).fetchone()
        return row is not None and bool(row["merged"]) and self.get(guild_id, user_id) is not None

    def merge_scope(self, src: int, dst: int, *, now: float | None = None) -> tuple[int, int]:
        """把 src（要加入组的服务器）的人设卡并进 dst（组）。返回 (搬过去的卡, 存成旧版本的卡)。

        组里还没有这个人的卡：连同旧版本整个搬过去（版本号不变：对话里附着的那份不用重新附）。两边都有：用组里那张，
        src 那张存成组里那张最新的旧版本（记成从别处并进来的：本人用 /ds-rp 改回上一版能换成它，组里那张会存成旧版本，
        再改回上一版就换回去），src 更早的旧版本不要了。组里删掉了他的卡、而且删得比 src 那张新：不把 src 那张顶上去，
        也只存成旧版本。src 那边卡删掉了、只剩旧版本（叫它忘掉了，还能改回来）：旧版本也搬过去，但不挤掉组里这个人
        自己的旧版本（放得下几个搬几个）。src 的人都记成“在 src 出现过”（退出组时只带走自己的人的卡）。
        删卡的记号取两边较晚的那次。并完 src 就空了；再并一次什么也不做（中途断了可以重来）。"""
        now = time.time() if now is None else now
        moved = kept = 0
        with self.db:
            users = sorted(self.users(src))
            for uid in users:
                mine = self.get(src, uid)
                theirs = self.get(dst, uid)
                if mine is not None and theirs is not None:
                    if mine.text != theirs.text:
                        self._add_version(dst, uid, mine.text, now, merged=True)
                        kept += 1
                elif mine is not None:
                    dead = self.deleted_at(dst, uid)
                    if dead is not None and dead > mine.updated_at:  # 他在组里删掉了卡（删的时候 src 这张已经有了）
                        self._add_version(dst, uid, mine.text, now, merged=True)
                        kept += 1
                        self._move_versions(src, dst, uid)
                    else:
                        self.db.execute(
                            "INSERT INTO cards (guild_id, user_id, name, text, updated_at, via) VALUES (?, ?, ?, ?, ?, ?)",
                            (dst, uid, mine.name, mine.text, mine.updated_at, mine.via),
                        )
                        moved += 1
                        self._take_versions(src, dst, uid)
                else:
                    self._move_versions(src, dst, uid)
                self._trim_versions(dst, uid)
                name = mine.name if mine is not None else (theirs.name if theirs is not None else "")
                self.db.execute(
                    "INSERT INTO card_seen (guild_id, user_id, seen_in, name) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (guild_id, user_id, seen_in) DO NOTHING",
                    (dst, uid, src, name),
                )
            self.db.execute(
                "INSERT INTO card_tombstones (guild_id, user_id, deleted_at) "
                "SELECT ?, user_id, deleted_at FROM card_tombstones WHERE guild_id = ? "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET deleted_at = MAX(card_tombstones.deleted_at, excluded.deleted_at)",
                (dst, src),
            )
            self._drop(src)
        return moved, kept

    def _add_version(self, scope: int, user_id: int, text: str, now: float, *, merged: bool = False) -> None:
        self.db.execute(
            "INSERT INTO card_versions (guild_id, user_id, text, replaced_at, merged) VALUES (?, ?, ?, ?, ?)",
            (scope, user_id, text, now, int(merged)),
        )

    def _version_count(self, scope: int, user_id: int) -> int:
        return int(
            self.db.execute("SELECT COUNT(*) AS n FROM card_versions WHERE guild_id = ? AND user_id = ?", (scope, user_id)).fetchone()["n"]
        )

    def _move_versions(self, src: int, dst: int, user_id: int) -> None:
        """src 那边叫它忘掉了的卡的旧版本搬到 dst：dst 他自己的旧版本一个不挤掉，放得下几个就搬最近的几个（剩下的随 src 删掉）。
        dst 有他现在用的卡：搬来的是那张卡之外的另一张，记成从别处并进来的，排在那张卡自己的旧版本后面（改回上一版先回到
        自己的）。dst 没有他的卡：照原样搬（是他自己的历史）。"""
        room = CARD_VERSIONS_KEEP - self._version_count(dst, user_id)
        if room <= 0:
            return
        live = self.get(dst, user_id) is not None
        self.db.execute(
            "UPDATE card_versions SET guild_id = ?, merged = CASE WHEN ? THEN 1 ELSE merged END WHERE id IN "
            "(SELECT id FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?)",
            (dst, int(live), src, user_id, room),
        )
        if live:
            self._renew(dst, user_id, merged=0)

    def _renew(self, scope: int, user_id: int, *, merged: int) -> None:
        """把这个人在 scope 里 merged 标记是这个值的旧版本重新插入一遍（编号变成最大的，先后不变）：改回上一版先回到它们。"""
        old = [
            r["id"]
            for r in self.db.execute(
                "SELECT id FROM card_versions WHERE guild_id = ? AND user_id = ? AND merged = ? ORDER BY id", (scope, user_id, merged)
            )
        ]
        if not old:
            return
        marks = ",".join("?" * len(old))
        self.db.execute(
            "INSERT INTO card_versions (guild_id, user_id, text, replaced_at, merged) "
            f"SELECT guild_id, user_id, text, replaced_at, merged FROM card_versions WHERE id IN ({marks}) ORDER BY id",
            old,
        )
        self.db.execute(f"DELETE FROM card_versions WHERE id IN ({marks})", old)

    def _take_versions(self, src: int, dst: int, user_id: int) -> None:
        """src 那张卡成了 dst 的卡：它的旧版本整批搬过去，排在最近（改回上一版先回到它们）。dst 原来剩下的旧版本
        （他在组里叫它忘掉的那张）记成从别处来的、放不下就去掉最早的。"""
        self.db.execute("UPDATE card_versions SET merged = 1 WHERE guild_id = ? AND user_id = ?", (dst, user_id))
        mine = self._version_count(src, user_id)
        if not mine:
            return
        extra = self._version_count(dst, user_id) + mine - CARD_VERSIONS_KEEP
        if extra > 0:
            self.db.execute(
                "DELETE FROM card_versions WHERE id IN (SELECT id FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id LIMIT ?)",
                (dst, user_id, extra),
            )
        self.db.execute(  # 重新插入（编号比 dst 原来的大），先后不变
            "INSERT INTO card_versions (guild_id, user_id, text, replaced_at, merged) "
            "SELECT ?, user_id, text, replaced_at, merged FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id",
            (dst, src, user_id),
        )
        self.db.execute("DELETE FROM card_versions WHERE guild_id = ? AND user_id = ?", (src, user_id))

    def copy_scope(self, src: int, dst: int, users: set[int] | None = None) -> int:
        """dst 换成 src 的一份副本（卡、旧版本，版本号都不变）：退出组的服务器带走组里的。users：只复制这几个人的卡
        （不填 = 所有人）。删卡的记号总是全部复制（不然整理记忆时，删掉的卡又被删之前的聊天写回来）。
        再复制一次结果一样。返回复制了几张卡。"""
        picked = "" if users is None else f" AND user_id IN ({','.join('?' * len(users))})"
        args = () if users is None else tuple(sorted(users))
        with self.db:
            self._drop(dst)
            cur = self.db.execute(
                "INSERT INTO cards (guild_id, user_id, name, text, updated_at, via) "
                f"SELECT ?, user_id, name, text, updated_at, via FROM cards WHERE guild_id = ?{picked}",
                (dst, src, *args),
            )
            self.db.execute(
                "INSERT INTO card_versions (guild_id, user_id, text, replaced_at, merged) "
                f"SELECT ?, user_id, text, replaced_at, merged FROM card_versions WHERE guild_id = ?{picked} ORDER BY id",
                (dst, src, *args),
            )
            self.db.execute(
                "INSERT INTO card_tombstones (guild_id, user_id, deleted_at) "
                "SELECT ?, user_id, deleted_at FROM card_tombstones WHERE guild_id = ?",
                (dst, src),
            )
        return cur.rowcount

    def drop_users(self, scope: int, users: set[int]) -> None:
        """从 scope 里拿掉这几个人的卡（连同旧版本）：只在退出组的那个服务器出现过的人，卡跟着那个服务器走。
        删卡的记号留着（整理记忆时别拿删之前的聊天给他重新写卡）。"""
        with self.db:
            for uid in users:
                for table in ("cards", "card_versions", "card_seen"):
                    self.db.execute(f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?", (scope, uid))

    def forget_seen_in(self, scope: int, guild_id: int) -> None:
        """guild_id 退出了组：组里不再记“在那里出现过”。"""
        with self.db:
            self.db.execute("DELETE FROM card_seen WHERE guild_id = ? AND seen_in = ?", (scope, guild_id))

    def drop_scope(self, scope: int) -> None:
        """整个删掉（散掉的组）。"""
        with self.db:
            self._drop(scope)

    def group_scopes(self) -> dict[int, int]:
        """有卡（或者只剩旧版本）的组：{组的 ID: 几个人}（查散掉的组有没有剩下数据用）。"""
        rows = self.db.execute(
            "SELECT guild_id, COUNT(DISTINCT user_id) AS n FROM "
            "(SELECT guild_id, user_id FROM cards WHERE guild_id < 0 UNION SELECT guild_id, user_id FROM card_versions WHERE guild_id < 0) "
            "GROUP BY guild_id"
        )
        return {r["guild_id"]: r["n"] for r in rows}

    def lowest_scope(self) -> int:
        """用过的最小（最负）的组 ID，没有就是 0（新建组时编号要比它小）。"""
        row = self.db.execute(
            "SELECT MIN(m) AS m FROM (SELECT MIN(guild_id) AS m FROM cards UNION ALL SELECT MIN(guild_id) FROM card_versions "
            "UNION ALL SELECT MIN(guild_id) FROM card_tombstones UNION ALL SELECT MIN(guild_id) FROM card_seen)"
        ).fetchone()
        return min(0, row["m"] if row["m"] is not None else 0)

    def _drop(self, scope: int) -> None:
        if scope == SHARED:
            return  # 0 是“所有服务器”（bot 主人删卡的记号、共用的卡），不是哪个服务器
        for table in ("cards", "card_versions", "card_tombstones", "card_seen"):
            self.db.execute(f"DELETE FROM {table} WHERE guild_id = ?", (scope,))

    # ---------------------------------------------------------------- 所有服务器共用一张卡（config.toml 的 [bot] shared_cards）

    def set_shared(
        self, shared: bool, *, scope_of: Callable[[int], int], alive: Collection[int], now: float | None = None
    ) -> ShareChange | None:
        """改成每个人在所有服务器共用一张卡（shared），或者改回每个服务器一张。启动时照 config.toml 调一次；一次事务做完，
        中途出错就像没改过（还按原来的方式用）。scope_of：服务器的卡平时记在哪（连成一组的是组的 ID）；alive：现在的组
        （和正在退出的组）。

        改成共用：各服务器、各组的卡都并进 SHARED，每人一张（规则见 _combine：最新的那张当现在的卡，别的存成旧版本，
          本人用 /ds-rp 改回上一版能换过去），记下他在哪些服务器出现过、在那里叫什么。散掉的组剩下的不动（leftovers 照样
          报出来）。已经是共用的也照样扫一遍：别处不该有卡，有的话（比如降级到旧版本时记下的）一起并进来。
        改回每个服务器一张：每个人的卡（连同旧版本）复制到他出现过的每个服务器（在组里的就是那个组），共用那份删掉；
          没在任何服务器出现过的留在共用那份里（不丢，再改成共用时还在）。
        删卡的记号都留在原地：0 下面的本来就算所有服务器。和现在一样、也没有要扫的：返回 None。"""
        now = time.time() if now is None else now
        if not shared and not self.shared:
            return None
        with self.db:
            change = self._share(set(alive), now) if shared else self._split(scope_of, now)
            self.db.execute(
                "INSERT INTO card_meta (key, value) VALUES ('shared', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                ("1" if shared else "0",),
            )
        was, self.shared = self.shared, shared
        return change if change.people or was != shared else None

    def for_groups(self) -> "CardStore | SharedCardMarks":
        """连组、退组（groups.py）时交给它的人设卡：平时就是自己；所有服务器共用一张卡时卡不用搬，只搬删卡的记号。"""
        return SharedCardMarks(self) if self.shared else self

    def _bundle(self, scope: int, user_id: int) -> _Bundle:
        rows = self.db.execute(
            "SELECT text, replaced_at, merged FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id", (scope, user_id)
        ).fetchall()
        versions = [(r["text"], r["replaced_at"], int(r["merged"])) for r in rows]
        # 只看“所有服务器都删了”的记号（共用时本人清空、bot 主人删的）：各处自己的记号见 _combine
        row = self.db.execute(
            "SELECT deleted_at FROM card_tombstones WHERE guild_id = ? AND user_id = ?", (SHARED, user_id)
        ).fetchone()
        return _Bundle(scope, self.get(scope, user_id), versions, row["deleted_at"] if row is not None else None)

    def _write(self, scope: int, user_id: int, card: Card | None, versions: list[tuple[str, float, int]]) -> None:
        """这个人在 scope 的卡整个换成这些（版本号不变：对话里附着的同一张卡不用重新附）。"""
        self.db.execute("DELETE FROM cards WHERE guild_id = ? AND user_id = ?", (scope, user_id))
        self.db.execute("DELETE FROM card_versions WHERE guild_id = ? AND user_id = ?", (scope, user_id))
        if card is not None:
            self.db.execute(
                "INSERT INTO cards (guild_id, user_id, name, text, updated_at, via) VALUES (?, ?, ?, ?, ?, ?)",
                (scope, user_id, card.name, card.text, card.updated_at, card.via),
            )
        for text, at, merged in versions:
            self.db.execute(
                "INSERT INTO card_versions (guild_id, user_id, text, replaced_at, merged) VALUES (?, ?, ?, ?, ?)",
                (scope, user_id, text, at, merged),
            )

    def _see(self, scope: int, user_id: int, guild_id: int, name: str) -> None:
        """记下他在 guild_id 出现过（name 是空的：不知道他在那里叫什么，原来记着的名字留着）。"""
        self.db.execute(
            "INSERT INTO card_seen (guild_id, user_id, seen_in, name) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id, seen_in) DO UPDATE SET "
            "name = CASE WHEN excluded.name != '' THEN excluded.name ELSE card_seen.name END",
            (scope, user_id, guild_id, name),
        )

    def _forget(self, scope: int, user_id: int) -> None:
        """拿掉这个人在 scope 的卡、旧版本、出现过的记录（删卡的记号留着）。"""
        for table in ("cards", "card_versions", "card_seen"):
            self.db.execute(f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?", (scope, user_id))

    def _share(self, alive: set[int], now: float) -> ShareChange:
        rows = self.db.execute(
            "SELECT DISTINCT guild_id, user_id FROM (SELECT guild_id, user_id FROM cards UNION SELECT guild_id, user_id FROM card_versions) "
            "WHERE guild_id != ? ORDER BY user_id, guild_id",
            (SHARED,),
        ).fetchall()
        places: dict[int, list[int]] = {}
        for r in rows:
            if r["guild_id"] > 0 or r["guild_id"] in alive:  # 服务器和现在的组；散掉的组剩下的不动
                places.setdefault(r["user_id"], []).append(r["guild_id"])
        merged = 0
        for uid, scopes in places.items():
            bundles = [b for b in (self._bundle(s, uid) for s in (SHARED, *scopes)) if not b.empty()]
            card, versions = _combine(bundles, now)
            self._write(SHARED, uid, card, versions)
            for s in scopes:
                if s > 0:
                    # 这个服务器自己的卡：他在那里出现过，卡上的名字就是他在那里的名字（空的：分回去时他在那里只在私信里
                    # 出现过、之后也没在频道里说过话，照旧不知道他叫什么）。只剩旧版本的（他在那里把卡删了）不算出现过
                    # （和改之前一样：那里没有他的卡）
                    own = next((b.card for b in bundles if b.scope == s), None)
                    if own is not None:
                        self._see(SHARED, uid, s, own.name)
                else:  # 组：组里记着他在哪几个服务器出现过、叫什么
                    for r in self.db.execute("SELECT seen_in, name FROM card_seen WHERE guild_id = ? AND user_id = ?", (s, uid)).fetchall():
                        self._see(SHARED, uid, r["seen_in"], r["name"])
                self._forget(s, uid)
            merged += sum(b.card is not None for b in bundles) > 1
        return ShareChange(True, len(places), merged=merged)

    def _split(self, scope_of: Callable[[int], int], now: float) -> ShareChange:
        people = stayed = 0
        for uid in sorted(self.users(SHARED)):
            targets: dict[int, list[tuple[int, str]]] = {}  # 卡要复制到哪 -> [(他出现过的服务器, 在那里的名字)]
            for r in self.db.execute(
                "SELECT seen_in, name FROM card_seen WHERE guild_id = ? AND user_id = ? ORDER BY seen_in", (SHARED, uid)
            ).fetchall():
                target = scope_of(r["seen_in"])
                if target != SHARED:
                    targets.setdefault(target, []).append((r["seen_in"], r["name"]))
            if not targets:
                stayed += 1  # 不知道该放到哪个服务器：留在共用的那份里
                continue
            mine = self._bundle(SHARED, uid)
            for target, seen in targets.items():
                there = self._bundle(target, uid)  # 一般是空的（共用期间别处不会写卡）
                card, versions = _combine([mine] if there.empty() else [mine, there], now)
                if card is not None and card is mine.card:
                    # 卡上的名字用他在那个服务器（组：组里哪个服务器都行）的名字。他在那里只在私信里出现过的：不知道他在那里
                    # 叫什么，名字空着（别拿他在别的服务器的昵称顶上：那样那边就把他列出来、能按那个名字 @ 他了），他在频道里
                    # 一说话就补上。空着的名字连组时也照样记成“只在私信里出现过”，再改成共用时也一样
                    card = replace(card, name=next((n for _, n in seen if n), ""))
                self._write(target, uid, card, versions)
                if target < 0:
                    for guild_id, name in seen:
                        self._see(target, uid, guild_id, name)
            self._forget(SHARED, uid)
            people += 1
        return ShareChange(False, people, stayed=stayed)


class SharedCardMarks:
    """所有服务器共用一张人设卡时，连组、退组（groups.py）用的“人设卡”：卡都在共用的那份里，不跟着搬。删卡的记号照每个服务器
    一张时的规矩搬（加入时并进组里，退出时复制给退出的服务器，组散了删掉组里的）：改回每个服务器一张以后，整理记忆照样不会
    拿删卡之前的聊天把卡写回来。新组的编号照样避开卡文件里用过的；组操作期间卡文件也等写进磁盘（db）。"""

    def __init__(self, cards: CardStore):
        self.cards = cards
        self.db = cards.db

    def lowest_scope(self) -> int:
        return self.cards.lowest_scope()

    def group_scopes(self) -> dict[int, int]:
        return self.cards.group_scopes()

    def overlap_parts(self, src: int, dst: int) -> tuple[int, int]:
        return 0, 0

    def overlap(self, src: int, dst: int) -> int:
        return 0

    def users(self, scope: int) -> set[int]:
        return set()

    def seen_in(self, scope: int) -> dict[int, set[int]]:
        return {}

    def merge_scope(self, src: int, dst: int, *, now: float | None = None) -> tuple[int, int]:
        """src 的删卡记号并进 dst（取两边较晚的），src 的不留（和 CardStore.merge_scope 一样）。"""
        with self.db:
            self.db.execute(
                "INSERT INTO card_tombstones (guild_id, user_id, deleted_at) SELECT ?, user_id, deleted_at FROM card_tombstones "
                "WHERE guild_id = ? ON CONFLICT (guild_id, user_id) DO UPDATE SET "
                "deleted_at = MAX(card_tombstones.deleted_at, excluded.deleted_at)",
                (dst, src),
            )
            if src != SHARED:
                self.db.execute("DELETE FROM card_tombstones WHERE guild_id = ?", (src,))
        return 0, 0

    def copy_scope(self, src: int, dst: int, users: set[int] | None = None) -> int:
        """dst 的删卡记号换成 src 的（和 CardStore.copy_scope 一样：记号总是全部复制）。"""
        if dst == SHARED:
            return 0
        with self.db:
            self.db.execute("DELETE FROM card_tombstones WHERE guild_id = ?", (dst,))
            self.db.execute(
                "INSERT INTO card_tombstones (guild_id, user_id, deleted_at) SELECT ?, user_id, deleted_at FROM card_tombstones "
                "WHERE guild_id = ?",
                (dst, src),
            )
        return 0

    def drop_users(self, scope: int, users: set[int]) -> None:
        return None

    def forget_seen_in(self, scope: int, guild_id: int) -> None:
        return None

    def drop_scope(self, scope: int) -> None:
        """组散了：组里的删卡记号不要了（退出的服务器都复制过了）。"""
        if scope == SHARED:
            return
        with self.db:
            self.db.execute("DELETE FROM card_tombstones WHERE guild_id = ?", (scope,))


class MemoryStore:
    def __init__(self, path: str):
        self.path = str(path)
        self.db = open_private_db(self.path, MEMORY_SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(memories)")}
        if "origin" not in cols:  # 旧版本建的表：补上这一列（原有的记成“不知道”）
            self.db.execute("ALTER TABLE memories ADD COLUMN origin INTEGER")
            self.db.commit()
        if "shared_as" not in {r["name"] for r in self.db.execute("PRAGMA table_info(dm_memories)")}:
            self.db.execute("ALTER TABLE dm_memories ADD COLUMN shared_as TEXT")  # 2.13.0 以前建的表
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _memory(r: Any) -> Memory:
        return Memory(r["no"], r["text"], r["updated_at"], r["via"], r["origin"])

    def entries(self, guild_id: int) -> list[Memory]:
        rows = self.db.execute("SELECT no, text, updated_at, via, origin FROM memories WHERE guild_id = ? ORDER BY no", (guild_id,))
        return [self._memory(r) for r in rows]

    def count(self, guild_id: int) -> int:
        return int(self.db.execute("SELECT COUNT(*) AS n FROM memories WHERE guild_id = ?", (guild_id,)).fetchone()["n"])

    def get(self, guild_id: int, no: int) -> Memory | None:
        r = self.db.execute(
            "SELECT no, text, updated_at, via, origin FROM memories WHERE guild_id = ? AND no = ?", (guild_id, no)
        ).fetchone()
        return self._memory(r) if r is not None else None

    def add(self, guild_id: int, text: str, *, via: str, now: float | None = None, origin: int | None = None) -> int:
        """记一条，返回编号。调用方先检查条数上限和字数。origin：哪个服务器记的（连成一组时 guild_id 是组的 ID）。"""
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT next_no FROM memory_seq WHERE guild_id = ?", (guild_id,)).fetchone()
            no = row["next_no"] if row is not None else 1
            self.db.execute(
                "INSERT INTO memories (guild_id, no, text, created_at, updated_at, via, origin) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, no, clean_memory(text), now, now, via, origin),
            )
            self.db.execute(
                "INSERT INTO memory_seq (guild_id, next_no) VALUES (?, ?) ON CONFLICT (guild_id) DO UPDATE SET next_no = excluded.next_no",
                (guild_id, no + 1),
            )
        return no

    def edit(self, guild_id: int, no: int, text: str, *, via: str, now: float | None = None) -> bool:
        """改一条。记着是哪个服务器记的：在组里（guild_id 是组的 ID，负数）照旧；记在服务器自己名下（正数：没连组，
        改的就是这个服务器）就算它记的了——退出组时带走的、别的服务器记的条目，它改过以后再连组，归它整理。"""
        now = time.time() if now is None else now
        with self.db:
            cur = self.db.execute(
                "UPDATE memories SET text = ?, updated_at = ?, via = ?, origin = CASE WHEN guild_id > 0 THEN guild_id ELSE origin END "
                "WHERE guild_id = ? AND no = ?",
                (clean_memory(text), now, via, guild_id, no),
            )
        return cur.rowcount > 0

    def delete(self, guild_id: int, no: int) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM memories WHERE guild_id = ? AND no = ?", (guild_id, no))
        return cur.rowcount > 0

    def clear(self, guild_id: int) -> int:
        with self.db:
            cur = self.db.execute("DELETE FROM memories WHERE guild_id = ?", (guild_id,))
        return cur.rowcount

    # ---------------------------------------------------------------- 服务器组（scope：服务器 ID 或组的 ID）

    def keeps_numbers(self, dst: int) -> bool:
        """并进 dst 时编号不变：dst 还没有过长期记忆（整份搬过去）。不然并进去的接着 dst 的编号往下排。"""
        row = self.db.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM memories WHERE guild_id = ?) AND NOT EXISTS "
            "(SELECT 1 FROM memory_seq WHERE guild_id = ?) AS e",
            (dst, dst),
        ).fetchone()
        return bool(row["e"])

    def merge_preview(self, src: int, dst: int) -> tuple[int, int]:
        """src 并进 dst 会怎样：(会并进去几条, 并完一共几条)（一模一样的不重复记）。"""
        have = {r["text"] for r in self.db.execute("SELECT text FROM memories WHERE guild_id = ?", (dst,))}
        new = {r["text"] for r in self.db.execute("SELECT text FROM memories WHERE guild_id = ?", (src,))} - have
        return len(new), len(have) + len(new)

    def merge_scope(self, src: int, dst: int, members: Collection[int] | None = None) -> int:
        """把 src（要加入组的服务器）的长期记忆并进 dst（组），返回并进去几条。组里还没有记忆：原样搬过去，编号不变；
        已经有了：一模一样的不重复记，别的接着组里的编号往下排（超过条数上限也先留着，整理记忆时它会合并）。
        记着是哪个服务器记的：members（连完以后组里的服务器）里的照旧（退过组又连回来，各是各的）；不知道是谁记的、
        记的那个服务器不在组里的，算 src 记的（它带进来的）。没给 members：只有不知道是谁记的才算 src 的。
        并完 src 就空了；再并一次什么也不做（中途断了可以重来）。"""
        mine = None if members is None else set(members)

        def origin(old: int | None) -> int:
            return old if old is not None and (mine is None or old in mine) else src

        with self.db:
            if self.keeps_numbers(dst):
                if mine is None:
                    rule, args = "COALESCE(origin, ?)", [src]
                else:
                    rule = f"CASE WHEN origin IN ({','.join('?' * len(mine)) or 'NULL'}) THEN origin ELSE ? END"
                    args = [*sorted(mine), src]
                cur = self.db.execute(f"UPDATE memories SET guild_id = ?, origin = {rule} WHERE guild_id = ?", (dst, *args, src))
                self.db.execute("UPDATE memory_seq SET guild_id = ? WHERE guild_id = ?", (dst, src))
                self.db.execute(  # 有记忆却没有编号记录（不该有）：照最大的编号补上，免得新记的撞号；没有记忆就不补
                    "INSERT INTO memory_seq (guild_id, next_no) SELECT ?, MAX(no) + 1 FROM memories "
                    "WHERE guild_id = ? HAVING COUNT(*) > 0 ON CONFLICT (guild_id) DO NOTHING",
                    (dst, dst),
                )
                return cur.rowcount
            have = {r["text"] for r in self.db.execute("SELECT text FROM memories WHERE guild_id = ?", (dst,))}
            row = self.db.execute(
                "SELECT MAX(COALESCE((SELECT next_no FROM memory_seq WHERE guild_id = ?), 1), "
                "COALESCE((SELECT MAX(no) + 1 FROM memories WHERE guild_id = ?), 1)) AS n",
                (dst, dst),
            ).fetchone()
            no = int(row["n"])
            added = 0
            for r in self.db.execute(
                "SELECT text, created_at, updated_at, via, origin FROM memories WHERE guild_id = ? ORDER BY no", (src,)
            ).fetchall():
                if r["text"] in have:
                    continue
                self.db.execute(
                    "INSERT INTO memories (guild_id, no, text, created_at, updated_at, via, origin) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (dst, no, r["text"], r["created_at"], r["updated_at"], r["via"], origin(r["origin"])),
                )
                have.add(r["text"])
                no += 1
                added += 1
            self.db.execute(
                "INSERT INTO memory_seq (guild_id, next_no) VALUES (?, ?) ON CONFLICT (guild_id) DO UPDATE SET next_no = excluded.next_no",
                (dst, no),
            )
            self._drop(src)
        return added

    def copy_scope(self, src: int, dst: int) -> int:
        """dst 换成 src 的一份副本（编号不变）：退出组的服务器带走组里的长期记忆。返回复制了几条。
        带走的从此就是它自己的（之后各记各的，没在组里时整理都能改），照样记着是哪个服务器记的：以后连进别的组，
        记的那个服务器也在组里的还算那边的（又连回原来的组时各是各的），不在的算它带进来的（见 merge_scope）。"""
        with self.db:
            self._drop(dst)
            cur = self.db.execute(
                "INSERT INTO memories (guild_id, no, text, created_at, updated_at, via, origin) "
                "SELECT ?, no, text, created_at, updated_at, via, origin FROM memories WHERE guild_id = ?",
                (dst, src),
            )
            self.db.execute(
                "INSERT INTO memory_seq (guild_id, next_no) SELECT ?, next_no FROM memory_seq WHERE guild_id = ?", (dst, src)
            )
        return cur.rowcount

    def drop_scope(self, scope: int) -> None:
        """整个删掉（散掉的组）。"""
        with self.db:
            self._drop(scope)

    def group_scopes(self) -> dict[int, int]:
        """有长期记忆的组：{组的 ID: 几条}（查散掉的组有没有剩下数据用）。"""
        rows = self.db.execute("SELECT guild_id, COUNT(*) AS n FROM memories WHERE guild_id < 0 GROUP BY guild_id")
        return {r["guild_id"]: r["n"] for r in rows}

    def lowest_scope(self) -> int:
        """用过的最小（最负）的组 ID，没有就是 0（新建组时编号要比它小）。"""
        row = self.db.execute(
            "SELECT MIN(m) AS m FROM (SELECT MIN(guild_id) AS m FROM memories UNION ALL SELECT MIN(guild_id) FROM memory_seq)"
        ).fetchone()
        return min(0, row["m"] if row["m"] is not None else 0)

    def _drop(self, scope: int) -> None:
        self.db.execute("DELETE FROM memories WHERE guild_id = ?", (scope,))
        self.db.execute("DELETE FROM memory_seq WHERE guild_id = ?", (scope,))

    # ---------------------------------------------------------------- 私信记忆（每人一份）

    @staticmethod
    def _dm(r: Any) -> DmMemory:
        return DmMemory(r["no"], r["text"], bool(r["sensitive"]), r["updated_at"], r["via"], r["shared_as"])

    def dm_entries(self, user_id: int) -> list[DmMemory]:
        rows = self.db.execute(
            "SELECT no, text, sensitive, updated_at, via, shared_as FROM dm_memories WHERE user_id = ? ORDER BY no", (user_id,)
        )
        return [self._dm(r) for r in rows]

    def dm_get(self, user_id: int, no: int) -> DmMemory | None:
        r = self.db.execute(
            "SELECT no, text, sensitive, updated_at, via, shared_as FROM dm_memories WHERE user_id = ? AND no = ?", (user_id, no)
        ).fetchone()
        return self._dm(r) if r is not None else None

    def dm_shared(self) -> list[tuple[int, DmMemory]]:
        """所有人标成可共享的私信记忆：[(人, 那一条)]，最近改过的在前。"""
        rows = self.db.execute(
            "SELECT user_id, no, text, sensitive, updated_at, via, shared_as FROM dm_memories "
            "WHERE shared_as IS NOT NULL AND sensitive = 0 ORDER BY updated_at DESC, user_id, no"
        )
        return [(int(r["user_id"]), self._dm(r)) for r in rows]

    def dm_share(self, user_id: int, no: int, name: str | None) -> bool:
        """本人把一条标成可共享（name：他现在的名字）或者取消（None）。敏感的标不了。不算改了内容（updated_at 不动）。"""
        current = self.dm_get(user_id, no)
        if current is None or (name is not None and current.sensitive):
            return False
        with self.db:
            self.db.execute("UPDATE dm_memories SET shared_as = ? WHERE user_id = ? AND no = ?", (name, user_id, no))
        return True

    def dm_add(self, user_id: int, text: str, *, sensitive: bool, via: str, now: float | None = None) -> int:
        """记一条，返回编号。调用方先检查条数上限和字数。"""
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT next_no FROM dm_memory_seq WHERE user_id = ?", (user_id,)).fetchone()
            no = row["next_no"] if row is not None else 1
            self.db.execute(
                "INSERT INTO dm_memories (user_id, no, text, sensitive, created_at, updated_at, via) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, no, clean_memory(text), int(bool(sensitive)), now, now, via),
            )
            self.db.execute(
                "INSERT INTO dm_memory_seq (user_id, next_no) VALUES (?, ?) ON CONFLICT (user_id) DO UPDATE SET next_no = excluded.next_no",
                (user_id, no + 1),
            )
        return no

    def dm_edit(
        self, user_id: int, no: int, text: str | None, *, sensitive: bool | None = None, via: str, now: float | None = None
    ) -> bool:
        """改一条：text 是 None 就不改内容，sensitive 是 None 就不改敏感标记。"""
        current = self.dm_get(user_id, no)
        if current is None:
            return False
        new_text = clean_memory(text) if text is not None else current.text
        new_sensitive = current.sensitive if sensitive is None else bool(sensitive)
        if new_text == current.text and new_sensitive == current.sensitive:
            return False
        now = time.time() if now is None else now
        # 可共享是本人对“这句话”点的头：内容被改了（它聊天、整理时改的）、或者改成了敏感，就不再算可共享，要他再标一次
        keep = current.shared_as if new_text == current.text and not new_sensitive else None
        with self.db:
            cur = self.db.execute(
                "UPDATE dm_memories SET text = ?, sensitive = ?, shared_as = ?, updated_at = ?, via = ? WHERE user_id = ? AND no = ?",
                (new_text, int(new_sensitive), keep, now, via, user_id, no),
            )
        return cur.rowcount > 0

    def dm_delete(self, user_id: int, no: int) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM dm_memories WHERE user_id = ? AND no = ?", (user_id, no))
        return cur.rowcount > 0

    def dm_clear(self, user_id: int, *, now: float | None = None) -> int:
        """全部清掉，记下清空的时间（整理记忆时，这之前的聊天不再拿来写他的私信记忆）。"""
        now = time.time() if now is None else now
        with self.db:
            cur = self.db.execute("DELETE FROM dm_memories WHERE user_id = ?", (user_id,))
            self.db.execute(
                "INSERT INTO dm_memory_cleared (user_id, cleared_at) VALUES (?, ?) "
                "ON CONFLICT (user_id) DO UPDATE SET cleared_at = excluded.cleared_at",
                (user_id, now),
            )
        return cur.rowcount

    def dm_cleared_at(self, user_id: int) -> float | None:
        r = self.db.execute("SELECT cleared_at FROM dm_memory_cleared WHERE user_id = ?", (user_id,)).fetchone()
        return float(r["cleared_at"]) if r is not None else None


# ---------------------------------------------------------------------------
# 整理记忆（模型回顾一段聊天之后的输出）
# ---------------------------------------------------------------------------

_INDEX = re.compile(r"[0-9]{1,9}")


def _squash(text: str) -> str:
    return "".join(text.split()).casefold()


def overlaps(text: str, secret: str) -> bool:
    """text 是不是说到了 secret 这件事：原话（4 个字以上）出现在里面，或者 secret 六成以上的两字组合都出现了。
    只是兜底（换个说法就认不出来），主要还是靠提示词。"""
    s, t = _squash(secret), _squash(text)
    if not s or not t:
        return False
    if len(s) >= 4 and s in t:
        return True
    grams = {s[i : i + 2] for i in range(len(s) - 1)}
    if len(grams) < 3:
        return False
    return sum(1 for g in grams if g in t) / len(grams) >= 0.6


def reveals(text: str, secrets: list[str], already: str = "") -> bool:
    """text 说到了哪件敏感的事（already 里本来就说到的不算：比如本人自己写在人设卡上的）。"""
    return any(overlaps(text, s) and not (already and overlaps(already, s)) for s in secrets)


def coerce_no(value: Any) -> int | None:
    """长期记忆的编号：7、"7"、"(7)"、"（7）"、"#7"、"第7条" 都认。"""
    if isinstance(value, bool):
        return None
    number: int | None = None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        text = value.strip().strip("()（）#＃").removeprefix("第").removesuffix("条").strip()
        if _INDEX.fullmatch(text):
            number = int(text)
    return number if number is not None and abs(number) < 2**31 else None  # 大得离谱的存不进 SQLite


@dataclass
class MemoryOp:
    op: str  # add / edit / delete
    no: int | None = None
    text: str | None = None
    sensitive: bool | None = None  # 私信记忆：敏感不敏感（None = 没写；edit 时 None 就是不改）


_TRUE = ("true", "yes", "1", "是", "对", "敏感")
_FALSE = ("false", "no", "0", "否", "不", "不敏感")


def coerce_flag(value: Any) -> bool | None:
    """true / "true" / "是" / 1 都认；看不懂就是 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None


def _sensitive(item: dict[str, Any]) -> bool | None:
    for key in ("sensitive", "private", "secret", "敏感"):
        if key in item:
            return coerce_flag(item[key])
    return None


def parse_memory_op(item: Any) -> MemoryOp | None:
    """{"add": "…"} / {"edit": 7, "text": "…"} / {"delete": 7}（也认 {"op": "add", "text": "…"} 这种写法）。"""
    if not isinstance(item, dict):
        return None
    op = str(item.get("op", "")).strip().lower()
    sensitive = _sensitive(item)
    if "add" in item or op == "add":
        text = item.get("add") if isinstance(item.get("add"), str) else item.get("text")
        return MemoryOp("add", text=text.strip(), sensitive=sensitive) if isinstance(text, str) and text.strip() else None
    if "edit" in item or op == "edit":
        no = coerce_no(item.get("edit", item.get("no")))
        text = item.get("text")
        if no is not None and isinstance(text, str) and text.strip():
            return MemoryOp("edit", no=no, text=text.strip(), sensitive=sensitive)
        if no is not None and text is None and sensitive is not None:
            return MemoryOp("edit", no=no, sensitive=sensitive)  # 只改敏感标记
        return None
    for key in ("delete", "remove"):
        if key in item or op == key:
            no = coerce_no(item.get(key, item.get("no")))
            return MemoryOp("delete", no=no) if no is not None else None
    return None


def _load_json(text: str) -> Any:
    text = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S | re.I)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def parse_review(content: str) -> tuple[list[tuple[int, str, str | None]], list[MemoryOp], list[MemoryOp]] | None:
    """整理记忆的输出 → ([(消息编号, 新的完整人设卡, 写的是谁)], [长期记忆的改动], [私信记忆的改动])。不是合法 JSON 时返回 None。"""
    data = _load_json(content)
    if not isinstance(data, dict):
        return None
    cards: list[tuple[int, str, str | None]] = []
    for item in _items(data.get("cards")):
        if not isinstance(item, dict):
            continue
        to = coerce_no(item.get("to"))
        text = item.get("text")
        name = item.get("name")
        if to is not None and isinstance(text, str) and text.strip():
            cards.append((to, text, name.strip() if isinstance(name, str) and name.strip() else None))
    ops = [op for op in (parse_memory_op(item) for item in _items(data.get("memory"))) if op is not None]
    raw_dm = next((data[k] for k in ("dm_memory", "private_memory", "私信记忆") if k in data), None)
    dm_ops = [op for op in (parse_memory_op(item) for item in _items(raw_dm)) if op is not None]
    return cards, ops, dm_ops
