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
    outcome TEXT NOT NULL,        -- reply / react / pass / error / summary
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
        self, row_id: int, *, outcome: str, hit: int = 0, miss: int = 0, output: int = 0, cost: float = 0.0
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE usage SET outcome = ?, hit = ?, miss = ?, output = ?, cost = ? WHERE id = ?",
                (outcome, hit, miss, output, cost, row_id),
            )

    def set_outcome(self, row_id: int, outcome: str) -> None:
        with self.db:
            self.db.execute("UPDATE usage SET outcome = ? WHERE id = ?", (outcome, row_id))

    def count_usage(self, day: str, *, guild_id: int, kind: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM usage WHERE day = ? AND guild_id = ? AND kind = ?", (day, guild_id, kind)
        ).fetchone()
        return int(row["n"])

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
        # 缓存命中率只看聊天请求：搜索、压缩摘要是单独的请求，前缀本来就不一样
        chat = self.db.execute(
            f"SELECT COALESCE(SUM(hit),0) AS hit, COALESCE(SUM(miss),0) AS miss FROM usage "
            f"WHERE {clause} AND kind IN ('mention', 'proactive')",
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
