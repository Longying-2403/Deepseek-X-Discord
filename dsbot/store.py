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
    updated_at REAL
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
    PRIMARY KEY (channel_id, short_id)
);
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
"""

MSGMAP_KEEP = 3000  # 每个频道保留最近多少个消息编号的映射


@dataclass
class Turn:
    id: int
    role: str
    content: Any  # str 或 list[dict]


@dataclass
class Conversation:
    channel_id: int
    guild_id: int
    summary: str | None = None
    next_short_id: int = 1
    turns: list[Turn] = field(default_factory=list)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        # 上次运行到一半就停了（断电、强制关闭）的调用：不会再有结果了
        self.db.execute("UPDATE usage SET outcome = 'error' WHERE outcome = 'pending'")
        self.db.commit()
        if self.path != ":memory:":
            self._make_private()

    def _make_private(self) -> None:
        """聊天记录和读过的文件内容都在数据库里：只让 bot 自己读写（NAS 上别的账号看不到）。尽力而为。"""
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self.path + suffix, 0o600)
            except OSError:
                pass

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
            "SELECT guild_id, summary, next_short_id FROM conversations WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        conv = Conversation(channel_id=channel_id, guild_id=guild_id)
        if row is not None:
            conv.summary = row["summary"]
            conv.next_short_id = row["next_short_id"]
        for r in self.db.execute(
            "SELECT id, role, content FROM turns WHERE channel_id = ? ORDER BY id", (channel_id,)
        ):
            conv.turns.append(Turn(r["id"], r["role"], json.loads(r["content"])))
        return conv

    def load_msgmap(self, channel_id: int) -> dict[int, int]:
        """short_id -> message_id（最近的一批）"""
        rows = self.db.execute(
            "SELECT short_id, message_id FROM msgmap WHERE channel_id = ? ORDER BY short_id DESC LIMIT ?",
            (channel_id, MSGMAP_KEEP),
        ).fetchall()
        return {r["short_id"]: r["message_id"] for r in rows}

    def append_round(
        self,
        conv: Conversation,
        user_content: Any,
        assistant_content: str,
        assigned: Iterable[tuple[int, int]],
        now: float | None = None,
    ) -> None:
        """追加一轮（user + assistant）。conv.turns 和 conv.next_short_id 同步更新。"""
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
            self.db.executemany(
                "INSERT OR REPLACE INTO msgmap (channel_id, short_id, message_id) VALUES (?, ?, ?)",
                [(conv.channel_id, sid, mid) for sid, mid in assigned],
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
        conv.turns.append(Turn(user_id, "user", json.loads(_dump(user_content))))
        conv.turns.append(Turn(assistant_id, "assistant", json.loads(_dump(assistant_content))))

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
