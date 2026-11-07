"""SQLite 存储：参数覆盖、每个频道的对话记录（只追加）、消息编号映射、用量账本。

单进程单线程使用；每次操作都很短，直接在事件循环里同步执行。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS overrides (
    scope TEXT NOT NULL,          -- global / guild / channel
    scope_id INTEGER NOT NULL,    -- global 用 0
    key TEXT NOT NULL,
    value TEXT NOT NULL,          -- JSON
    PRIMARY KEY (scope, scope_id, key)
);
CREATE TABLE IF NOT EXISTS conversations (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    summary TEXT,
    next_short_id INTEGER NOT NULL DEFAULT 1,
    updated_at REAL,
    reviewed_upto INTEGER NOT NULL DEFAULT 0  -- 整理记忆（人设卡、长期记忆）整理到哪条记录了
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,        -- JSON：字符串或内容块数组，原样回放保证缓存前缀一致
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_channel ON turns (channel_id, id);
CREATE TABLE IF NOT EXISTS dm_memory_attached (
    turn_id INTEGER NOT NULL,     -- 频道里附上某人私信记忆的那条记录
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,           -- 附的时候写的名字（撤回时按它找到那一段）
    PRIMARY KEY (turn_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_dm_memory_attached_user ON dm_memory_attached (user_id);
CREATE TABLE IF NOT EXISTS dm_notices (
    user_id INTEGER NOT NULL,
    what TEXT NOT NULL,           -- 私信里说明过的事（比如 dm_memory：私信记忆）
    at REAL NOT NULL,
    PRIMARY KEY (user_id, what)
);
CREATE TABLE IF NOT EXISTS msgmap (
    channel_id INTEGER NOT NULL,
    short_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    author_id INTEGER,            -- 发这条消息的人（整理记忆时知道 #12 是谁说的）；bot 发的是 NULL
    author_name TEXT,
    PRIMARY KEY (channel_id, short_id)
);
CREATE INDEX IF NOT EXISTS idx_msgmap_message ON msgmap (channel_id, message_id);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day TEXT NOT NULL,            -- 按 bot.timezone 的日期
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    kind TEXT NOT NULL,           -- mention / proactive / summary / search
    outcome TEXT NOT NULL,        -- reply / react / pass / error / summary（超时、断线的 error 行：用量和花费是估的）
    hit INTEGER NOT NULL DEFAULT 0,
    miss INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    reasoning INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage (day, guild_id, channel_id);
CREATE TABLE IF NOT EXISTS persona_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,          -- guild / category / channel
    scope_id INTEGER NOT NULL,
    old TEXT,                     -- 改之前这一级单独设置的人设；NULL = 没单独设置（沿用上一级）
    new TEXT,                     -- 改之后；NULL = 不再单独设置
    by_user INTEGER NOT NULL,     -- 谁改的（用户 ID）
    via TEXT NOT NULL,            -- chat（聊天里让它改）/ command（/ds 人设）/ undo（撤销）
    request TEXT,                 -- 当时那条要求，方便以后查
    reverted INTEGER NOT NULL DEFAULT 0  -- 这次修改已经被撤销了
);
CREATE TABLE IF NOT EXISTS dm_homes (
    user_id INTEGER PRIMARY KEY,  -- 私信它的人
    guild_id INTEGER NOT NULL,    -- 这段私信挂在哪个服务器（用它的人设、记忆、人设卡、预算）
    channel_id INTEGER,           -- 和这个人的私信频道
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dm_homes_channel ON dm_homes (channel_id);
CREATE TABLE IF NOT EXISTS server_groups (
    guild_id INTEGER PRIMARY KEY,  -- 连成一组的服务器（bot 主人用 /dsowner 服务器组 连的）
    group_id INTEGER NOT NULL,     -- 哪个组：负数，组里共用的人设卡、长期记忆按它存（和服务器 ID 撞不上）
    joined_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS server_group_seq (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_no INTEGER NOT NULL       -- 下一个新组的编号（组的 ID 是它的负数；散掉的组的编号不再用）
);
CREATE TABLE IF NOT EXISTS server_group_leaving (
    guild_id INTEGER PRIMARY KEY,  -- 正在退出组的服务器：已经不算组里的了，还没拿到组里那份的副本（中途断了，下次接着做完）
    group_id INTEGER NOT NULL,
    at REAL NOT NULL
);
"""

MSGMAP_KEEP = 3000  # 每个频道保留最近多少个消息编号的映射
# 旧版本的数据库缺的列：启动时自动补上，原有数据不动
_MIGRATIONS = (
    ("msgmap", "author_id", "INTEGER"),
    ("msgmap", "author_name", "TEXT"),
    ("conversations", "reviewed_upto", "INTEGER NOT NULL DEFAULT 0"),
)


def open_private_db(path: str, schema: str) -> sqlite3.Connection:
    """打开（或新建）一个只给 bot 自己读写的 SQLite 数据库：聊天记录、人设卡、长期记忆都是这样存的。"""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    if path != ":memory:":
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(schema)
    db.commit()
    if path != ":memory:":
        make_private(path)
    return db


def make_private(path: str) -> None:
    """只让 bot 自己读写（NAS 上别的账号看不到）。尽力而为。"""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(path + suffix, 0o600)
        except OSError:
            pass


@dataclass
class Turn:
    id: int
    role: str
    content: Any  # str 或 list[dict]
    created_at: float = 0.0


@dataclass
class Conversation:
    channel_id: int
    guild_id: int
    summary: str | None = None
    next_short_id: int = 1
    turns: list[Turn] = field(default_factory=list)
    reviewed_upto: int = 0  # 整理记忆整理到哪条记录了（记录的 id）


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # 聊天记录和读过的文件内容都在数据库里：只让 bot 自己读写
        self.db = open_private_db(self.path, "")
        self.db.executescript(SCHEMA)  # 没有的表按新的格式建；已经有的表下面补上缺的列
        self._migrate()
        # 上次运行到一半就停了（断电、强制关闭）的调用：不会再有结果了
        self.db.execute("UPDATE usage SET outcome = 'error' WHERE outcome = 'pending'")
        self.db.commit()
        # 服务器 -> 组（每一轮都要查好几次：放在内存里，按加入的先后排）
        self._groups: dict[int, int] = {
            r["guild_id"]: r["group_id"]
            for r in self.db.execute("SELECT guild_id, group_id FROM server_groups ORDER BY joined_at, guild_id")
        }
        self._epochs: dict[int, int] = {}  # 服务器 -> 它的服务器组变过几次（问模型期间变过没有：连了又退回来，归属一样、编号却变了）

    def _migrate(self) -> None:
        """旧版本建的表少几列：补上（新建的表直接按 SCHEMA 建，已经有这些列）。"""
        for table, column, decl in _MIGRATIONS:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                if column == "reviewed_upto":
                    # 升级前的聊天不回头整理（那时候没记是谁说的，一下子整理一大堆也费钱）：从现在开始整理
                    self.db.execute(
                        "UPDATE conversations SET reviewed_upto = "
                        "(SELECT COALESCE(MAX(id), 0) FROM turns WHERE turns.channel_id = conversations.channel_id)"
                    )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ---- 参数覆盖 ----
    def load_overrides(self) -> list[tuple[str, int, str, Any]]:
        rows = self.db.execute("SELECT scope, scope_id, key, value FROM overrides").fetchall()
        return [(r["scope"], r["scope_id"], r["key"], json.loads(r["value"])) for r in rows]

    def set_override(self, scope: str, scope_id: int, key: str, value: Any) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO overrides (scope, scope_id, key, value) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (scope, scope_id, key) DO UPDATE SET value = excluded.value",
                (scope, scope_id, key, _dump(value)),
            )

    def delete_override(self, scope: str, scope_id: int, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM overrides WHERE scope = ? AND scope_id = ? AND key = ?", (scope, scope_id, key))

    # ---- 对话 ----
    def load_conversation(self, channel_id: int, guild_id: int) -> Conversation:
        row = self.db.execute(
            "SELECT guild_id, summary, next_short_id, reviewed_upto FROM conversations WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        conv = Conversation(channel_id=channel_id, guild_id=guild_id)
        if row is not None:
            conv.summary = row["summary"]
            conv.next_short_id = row["next_short_id"]
            conv.reviewed_upto = row["reviewed_upto"] or 0
        for r in self.db.execute(
            "SELECT id, role, content, created_at FROM turns WHERE channel_id = ? ORDER BY id", (channel_id,)
        ):
            conv.turns.append(Turn(r["id"], r["role"], json.loads(r["content"]), r["created_at"] or 0.0))
        return conv

    def load_msgmap(self, channel_id: int) -> dict[int, int]:
        """short_id -> message_id（最近的一批）"""
        rows = self.db.execute(
            "SELECT short_id, message_id FROM msgmap WHERE channel_id = ? ORDER BY short_id DESC LIMIT ?",
            (channel_id, MSGMAP_KEEP),
        ).fetchall()
        return {r["short_id"]: r["message_id"] for r in rows}

    def load_authors(self, channel_id: int, short_ids: Iterable[int]) -> dict[int, tuple[int, str]]:
        """消息编号 -> (发消息的人, 当时的名字)。bot 发的、旧版本记下的（没记作者）不在里面。"""
        ids = sorted(set(short_ids))
        out: dict[int, tuple[int, str]] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in self.db.execute(
                f"SELECT short_id, author_id, author_name FROM msgmap WHERE channel_id = ? AND short_id IN ({marks}) "
                "AND author_id IS NOT NULL",
                (channel_id, *chunk),
            ):
                out[r["short_id"]] = (r["author_id"], r["author_name"] or "")
        return out

    def recent_authors(self, channel_id: int, limit: int) -> list[tuple[int, str]]:
        """这个频道最近说过话的人（不含 bot）：[(用户 ID, 最近一次的名字)]，最近的在前。"""
        rows = self.db.execute(
            "SELECT author_id, author_name FROM msgmap WHERE channel_id = ? AND author_id IS NOT NULL "
            "ORDER BY short_id DESC LIMIT ?",
            (channel_id, limit * 5),
        ).fetchall()
        seen: dict[int, str] = {}
        for r in rows:
            if r["author_name"]:
                seen.setdefault(r["author_id"], r["author_name"])
        return list(seen.items())[:limit]

    def message_rows(self, channel_id: int, message_ids: Iterable[int]) -> dict[int, list[tuple[int, str | None]]]:
        """消息 -> [(它在记录里的编号, 发消息的人当时的名字)]。不在记录里的消息不在结果里。"""
        ids = sorted(set(message_ids))
        out: dict[int, list[tuple[int, str | None]]] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in self.db.execute(
                f"SELECT short_id, message_id, author_name FROM msgmap WHERE channel_id = ? AND message_id IN ({marks}) "
                "ORDER BY short_id",
                (channel_id, *chunk),
            ):
                out.setdefault(r["message_id"], []).append((r["short_id"], r["author_name"]))
        return out

    # ---------------------------------------------------------------- 频道里附过的私信记忆

    def note_dm_memory(self, turn_id: int, channel_id: int, user_id: int, name: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO dm_memory_attached (turn_id, channel_id, user_id, name) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (turn_id, user_id) DO UPDATE SET name = excluded.name",
                (turn_id, channel_id, user_id, name),
            )

    def dm_memory_turns(self, *, user_id: int | None = None, guild_id: int | None = None) -> list[tuple[int, int, int, str]]:
        """附过私信记忆的记录 [(记录, 频道, 人, 名字)]：只看某个人的、某个服务器的（都不填就是全部）。"""
        sql = "SELECT a.turn_id, a.channel_id, a.user_id, a.name FROM dm_memory_attached a"
        where: list[str] = []
        args: list[Any] = []
        if guild_id is not None:
            sql += " JOIN conversations c ON c.channel_id = a.channel_id"
            where.append("c.guild_id = ?")
            args.append(guild_id)
        if user_id is not None:
            where.append("a.user_id = ?")
            args.append(user_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [(r["turn_id"], r["channel_id"], r["user_id"], r["name"]) for r in self.db.execute(sql + " ORDER BY a.turn_id", args)]

    def forget_dm_memory(self, rows: list[tuple[int, int]]) -> None:
        """不用再管的附件记录 [(记录, 人)]（已经撤回了，或者那条记录已经没了）。"""
        with self.db:
            self.db.executemany("DELETE FROM dm_memory_attached WHERE turn_id = ? AND user_id = ?", rows)

    def dm_noticed(self, user_id: int, what: str) -> bool:
        return self.db.execute("SELECT 1 FROM dm_notices WHERE user_id = ? AND what = ?", (user_id, what)).fetchone() is not None

    def mark_dm_noticed(self, user_id: int, what: str, now: float | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO dm_notices (user_id, what, at) VALUES (?, ?, ?)",
                (user_id, what, time.time() if now is None else now),
            )

    def turn_content(self, turn_id: int) -> Any | None:
        """一条记录现在的内容（已经被压缩、清空掉了就是 None）。"""
        r = self.db.execute("SELECT content FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return json.loads(r["content"]) if r is not None else None

    def update_turn(self, turn_id: int, content: Any) -> None:
        """改写一条记录（消息被删、被编辑时用；平时记录只追加）。"""
        with self.db:
            self.db.execute("UPDATE turns SET content = ? WHERE id = ?", (_dump(content), turn_id))

    def set_reviewed(self, conv: Conversation, turn_id: int) -> None:
        """整理记忆整理到了哪条记录（只往前走，不会退回去）。"""
        with self.db:
            self.db.execute(
                "INSERT INTO conversations (channel_id, guild_id, summary, next_short_id, updated_at, reviewed_upto) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (channel_id) DO UPDATE SET "
                "reviewed_upto = MAX(conversations.reviewed_upto, excluded.reviewed_upto)",
                (conv.channel_id, conv.guild_id, conv.summary, conv.next_short_id, time.time(), turn_id),
            )
        conv.reviewed_upto = max(conv.reviewed_upto, turn_id)

    def append_round(
        self,
        conv: Conversation,
        user_content: Any,
        assistant_content: str,
        assigned: Iterable[tuple[Any, ...]],
        now: float | None = None,
    ) -> None:
        """追加一轮（user + assistant）。conv.turns 和 conv.next_short_id 同步更新。

        assigned：这一轮新编的号，(编号, 消息 id) 或 (编号, 消息 id, 发消息的人, 名字)（bot 发的人填 None）。"""
        now = time.time() if now is None else now
        with self.db:
            cur = self.db.execute(
                "INSERT INTO turns (channel_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                (conv.channel_id, _dump(user_content), now),
            )
            user_id = cur.lastrowid
            cur = self.db.execute(
                "INSERT INTO turns (channel_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                (conv.channel_id, _dump(assistant_content), now),
            )
            assistant_id = cur.lastrowid
            rows = []
            for item in assigned:
                sid, mid, *who = item
                author, name = (who + [None, None])[:2]
                rows.append((conv.channel_id, sid, mid, author, name))
            self.db.executemany(
                "INSERT OR REPLACE INTO msgmap (channel_id, short_id, message_id, author_id, author_name) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self.db.execute(
                "INSERT INTO conversations (channel_id, guild_id, summary, next_short_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (channel_id) DO UPDATE SET "
                "next_short_id = excluded.next_short_id, updated_at = excluded.updated_at",
                (conv.channel_id, conv.guild_id, conv.summary, conv.next_short_id, now),
            )
            self.db.execute(
                "DELETE FROM msgmap WHERE channel_id = ? AND short_id < ?",
                (conv.channel_id, conv.next_short_id - MSGMAP_KEEP),
            )
        # JSON 往返一次，保证内存里的内容和以后从库里读出来的完全一致
        conv.turns.append(Turn(user_id, "user", json.loads(_dump(user_content)), now))
        conv.turns.append(Turn(assistant_id, "assistant", json.loads(_dump(assistant_content)), now))

    def compact(self, conv: Conversation, summary: str, remove_count: int) -> None:
        """用摘要替换最早的 remove_count 条 turn。"""
        if remove_count <= 0:
            return
        last_removed = conv.turns[remove_count - 1].id
        with self.db:
            self.db.execute("DELETE FROM turns WHERE channel_id = ? AND id <= ?", (conv.channel_id, last_removed))
            self.db.execute(
                "INSERT INTO conversations (channel_id, guild_id, summary, next_short_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (channel_id) DO UPDATE SET "
                "summary = excluded.summary, updated_at = excluded.updated_at",
                (conv.channel_id, conv.guild_id, summary, conv.next_short_id, time.time()),
            )
        conv.summary = summary
        conv.turns = conv.turns[remove_count:]

    def conversation_channels(self, guild_id: int) -> list[int]:
        """这个服务器里有对话记录（或摘要）的频道。"""
        rows = self.db.execute(
            "SELECT channel_id FROM conversations c WHERE guild_id = ? AND (summary IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM turns t WHERE t.channel_id = c.channel_id)) ORDER BY channel_id",
            (guild_id,),
        ).fetchall()
        return [r["channel_id"] for r in rows]

    def move_conversation(self, channel_id: int, guild_id: int) -> None:
        """私信换了挂的服务器：记录归到新的服务器下（先清空再换）。"""
        with self.db:
            self.db.execute("UPDATE conversations SET guild_id = ? WHERE channel_id = ?", (guild_id, channel_id))

    def reset_conversation(self, channel_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM turns WHERE channel_id = ?", (channel_id,))
            self.db.execute(
                "UPDATE conversations SET summary = NULL, updated_at = ? WHERE channel_id = ?",
                (time.time(), channel_id),
            )

    # ---- 人设修改记录 ----
    def add_persona_change(
        self,
        *,
        ts: float,
        scope: str,
        scope_id: int,
        old: str | None,
        new: str | None,
        by_user: int,
        via: str,
        request: str = "",
    ) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO persona_history (ts, scope, scope_id, old, new, by_user, via, request) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, scope, scope_id, old, new, by_user, via, request),
            )
        return int(cur.lastrowid)

    def last_persona_change(self, scopes: Iterable[tuple[str, int]]) -> dict[str, Any] | None:
        """这些范围里最近一次还没被撤销的人设修改（撤销用；撤销本身不算一次修改，所以连着撤会一步步往前撤）。"""
        keys = list(scopes)
        if not keys:
            return None
        where = " OR ".join("(scope = ? AND scope_id = ?)" for _ in keys)
        params = [x for key in keys for x in key]
        row = self.db.execute(
            f"SELECT id, ts, scope, scope_id, old, new, by_user, via FROM persona_history WHERE ({where}) "
            "AND via != 'undo' AND reverted = 0 ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_persona_reverted(self, row_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE persona_history SET reverted = 1 WHERE id = ?", (row_id,))

    # ---- 用量 ----
    def record_usage(
        self,
        *,
        ts: float,
        day: str,
        guild_id: int,
        channel_id: int,
        kind: str,
        outcome: str,
        hit: int = 0,
        miss: int = 0,
        output: int = 0,
        reasoning: int = 0,
        cost: float = 0.0,
    ) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO usage (ts, day, guild_id, channel_id, kind, outcome, hit, miss, output, reasoning, cost) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, day, guild_id, channel_id, kind, outcome, hit, miss, output, reasoning, cost),
            )
        return int(cur.lastrowid)

    def update_usage(
        self,
        row_id: int,
        *,
        outcome: str,
        hit: int = 0,
        miss: int = 0,
        output: int = 0,
        cost: float = 0.0,
        ts: float | None = None,
        day: str | None = None,
    ) -> None:
        """ts / day：花钱的时刻（跨过午夜的话，这笔记在结束的那天，和内存账本一致）。不填就不改。"""
        with self.db:
            self.db.execute(
                "UPDATE usage SET outcome = ?, hit = ?, miss = ?, output = ?, cost = ?, "
                "ts = COALESCE(?, ts), day = COALESCE(?, day) WHERE id = ?",
                (outcome, hit, miss, output, cost, ts, day, row_id),
            )

    def set_outcome(self, row_id: int, outcome: str) -> None:
        with self.db:
            self.db.execute("UPDATE usage SET outcome = ? WHERE id = ?", (outcome, row_id))

    def count_usage(self, day: str, *, guild_id: int, kind: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM usage WHERE day = ? AND guild_id = ? AND kind = ?", (day, guild_id, kind)
        ).fetchone()
        return int(row["n"])

    def channel_spent(self, day: str, channel_id: int) -> float:
        """这个频道今天花了多少（私信：这个人今天私信它花了多少）。"""
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost), 0) AS c FROM usage WHERE day = ? AND channel_id = ?", (day, channel_id)
        ).fetchone()
        return float(row["c"])

    # ---------------------------------------------------------------- 私信挂在哪个服务器

    def dm_home(self, user_id: int) -> tuple[int, int | None] | None:
        """(服务器, 私信频道)；没定过是 None。"""
        row = self.db.execute("SELECT guild_id, channel_id FROM dm_homes WHERE user_id = ?", (user_id,)).fetchone()
        return (int(row["guild_id"]), row["channel_id"]) if row is not None else None

    def dm_homes(self) -> dict[int, tuple[int, int]]:
        """私信频道 -> (人, 服务器)。"""
        rows = self.db.execute("SELECT user_id, guild_id, channel_id FROM dm_homes WHERE channel_id IS NOT NULL")
        return {int(r["channel_id"]): (int(r["user_id"]), int(r["guild_id"])) for r in rows}

    def set_dm_home(self, user_id: int, guild_id: int, channel_id: int | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO dm_homes (user_id, guild_id, channel_id, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET guild_id = excluded.guild_id, "
                "channel_id = COALESCE(excluded.channel_id, dm_homes.channel_id), updated_at = excluded.updated_at",
                (user_id, guild_id, channel_id, time.time()),
            )

    # ---------------------------------------------------------------- 服务器组

    def memory_scope(self, guild_id: int) -> int:
        """人设卡、长期记忆按哪个 ID 存：连成一组的服务器用组的 ID（负数，组里共用一份），不然就是服务器自己。"""
        return self._groups.get(guild_id, guild_id)

    def group_of(self, guild_id: int) -> int | None:
        return self._groups.get(guild_id)

    def group_members(self, group_id: int) -> list[int]:
        """组里的服务器，按加入的先后。"""
        return [g for g, grp in self._groups.items() if grp == group_id]

    def groups(self) -> dict[int, list[int]]:
        """组 -> 组里的服务器（按加入的先后）；组按建的先后（编号从 1 往上）。"""
        out: dict[int, list[int]] = {}
        for guild_id, group_id in self._groups.items():
            out.setdefault(group_id, []).append(guild_id)
        return dict(sorted(out.items(), key=lambda item: -item[0]))

    def new_group_id(self, lowest: int = 0) -> int:
        """新组的 ID（-1、-2……用过的不再用：中途断掉留下的数据不会被新组认走）。
        lowest：卡、记忆文件里用过的最小的组 ID。这个文件被单独还原、删掉以后编号会从头算，要比它小才不会撞上旧组的数据。"""
        with self.db:
            row = self.db.execute("SELECT next_no FROM server_group_seq WHERE id = 1").fetchone()
            no = max(row["next_no"] if row is not None else 1, 1 - min(0, lowest))
            self.db.execute(
                "INSERT INTO server_group_seq (id, next_no) VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET next_no = excluded.next_no",
                (no + 1,),
            )
        return -no

    def join_group(self, guild_id: int, group_id: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.db:
            # 加入的先后要分得出来（同一次连起来的两个服务器时间一样）：重启以后顺序不变
            last = self.db.execute("SELECT MAX(joined_at) AS t FROM server_groups WHERE guild_id != ?", (guild_id,)).fetchone()["t"]
            at = max(now, last + 0.001) if last is not None else now
            self.db.execute(
                "INSERT INTO server_groups (guild_id, group_id, joined_at) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET group_id = excluded.group_id, joined_at = excluded.joined_at",
                (guild_id, group_id, at),
            )
            self.db.execute("DELETE FROM server_group_leaving WHERE guild_id = ?", (guild_id,))
        self._groups.pop(guild_id, None)
        self._groups[guild_id] = group_id  # 排在最后（加入的先后）
        self._bump([guild_id])  # 只有它的记在哪、编号变了：组里原来的服务器编号不变（并进来的接在后面）

    def start_leaving(self, guild_id: int, now: float | None = None) -> None:
        """退出组的第一步：记下“正在退出”。复制完之前还算组里的（照常用组里那份；期间记下的也会复制过去），
        复制完了才换成自己那份（unmap）。"""
        group = self._groups.get(guild_id)
        if group is None:
            return
        with self.db:
            last = self.db.execute("SELECT MAX(at) AS t FROM server_group_leaving").fetchone()["t"]
            now = time.time() if now is None else now
            at = max(now, last + 0.001) if last is not None else now
            self.db.execute(
                "INSERT INTO server_group_leaving (guild_id, group_id, at) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET group_id = excluded.group_id, at = excluded.at",
                (guild_id, group, at),
            )

    def unmap(self, guild_id: int) -> None:
        """退出的服务器拿到了副本：从这一刻起不算组里的了（人设卡、长期记忆按自己的存）。“正在退出”的记录留着，收完尾才删。"""
        group = self._groups.get(guild_id)
        if group is None:
            return
        with self.db:
            self.db.execute("DELETE FROM server_groups WHERE guild_id = ?", (guild_id,))
        self._groups.pop(guild_id, None)
        self._bump([guild_id])  # 只有它换成了自己那份：组里剩下的照常用组里那份（退出的拿的是副本，组里的不动）

    def _bump(self, guilds: list[int]) -> None:
        for guild_id in dict.fromkeys(guilds):
            self._epochs[guild_id] = self._epochs.get(guild_id, 0) + 1

    def group_epoch(self, guild_id: int) -> int:
        """这个服务器自己连组、退组过几次（问模型前后对不上：它看到的长期记忆不是现在这份，编号可能对不上）。
        别的服务器连进、退出它的组不算：组里那份的编号不变（并进来的接在后面，退出的拿走的是副本）。"""
        return self._epochs.get(guild_id, 0)

    def leaving(self) -> list[tuple[int, int]]:
        """还没做完的退出 [(服务器, 组)]，按开始的先后。"""
        rows = self.db.execute("SELECT guild_id, group_id FROM server_group_leaving ORDER BY at, guild_id")
        return [(r["guild_id"], r["group_id"]) for r in rows]

    def done_leaving(self, guild_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM server_group_leaving WHERE guild_id = ?", (guild_id,))

    def spend_by_guild(self, day: str) -> dict[int, float]:
        rows = self.db.execute("SELECT guild_id, SUM(cost) AS c FROM usage WHERE day = ? GROUP BY guild_id", (day,))
        return {r["guild_id"]: r["c"] or 0.0 for r in rows}

    def day_stats(self, day: str, *, guild_id: int | None = None, channel_id: int | None = None) -> dict[str, Any]:
        where, args = ["day = ?"], [day]
        if guild_id is not None:
            where.append("guild_id = ?")
            args.append(guild_id)
        if channel_id is not None:
            where.append("channel_id = ?")
            args.append(channel_id)
        # 打开链接、上网找图、点歌时按歌名找歌只是记次数（不是 DeepSeek 的调用），不算进调用次数和结果统计
        where.append("kind NOT IN ('link', 'image')")
        where.append("NOT (kind = 'search' AND outcome = 'music')")
        clause = " AND ".join(where)
        totals = self.db.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(output),0) AS output, COALESCE(SUM(cost),0) AS cost FROM usage WHERE {clause}",
            args,
        ).fetchone()
        # 缓存命中率只看聊天请求：搜索、压缩摘要是单独的请求，前缀本来就不一样；
        # 出错的那几笔（超时、断线）记的是估出来的用量，也不算
        chat = self.db.execute(
            f"SELECT COALESCE(SUM(hit),0) AS hit, COALESCE(SUM(miss),0) AS miss FROM usage "
            f"WHERE {clause} AND kind IN ('mention', 'proactive') AND outcome != 'error'",
            args,
        ).fetchone()
        outcomes = {
            r["outcome"]: r["n"]
            for r in self.db.execute(
                f"SELECT outcome, COUNT(*) AS n FROM usage WHERE {clause} GROUP BY outcome", args
            )
        }
        hit, miss = chat["hit"], chat["miss"]
        return {
            "calls": totals["n"],
            "hit": hit,
            "miss": miss,
            "output": totals["output"],
            "cost": totals["cost"],
            "hit_rate": (hit / (hit + miss)) if (hit + miss) else None,
            "outcomes": outcomes,
        }
