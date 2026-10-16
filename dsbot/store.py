"""SQLite storage: setting overrides, each channel's conversation history (append-only), message number mapping, usage ledger.

Used from a single process and thread; every operation is short, so it runs synchronously right in the event loop.
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
    scope_id INTEGER NOT NULL,    -- 0 for global
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
    reviewed_upto INTEGER NOT NULL DEFAULT 0  -- how far the memory review (profile cards, long-term memory) has got in the history
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,        -- JSON: a string or a list of content blocks, replayed as is so the cache prefix stays identical
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_channel ON turns (channel_id, id);
CREATE TABLE IF NOT EXISTS msgmap (
    channel_id INTEGER NOT NULL,
    short_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    author_id INTEGER,            -- who sent this message (so a memory review knows who said #12); NULL for the bot's own
    author_name TEXT,
    PRIMARY KEY (channel_id, short_id)
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day TEXT NOT NULL,            -- date in bot.timezone
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
    old TEXT,                     -- this level's own persona before the change; NULL = none of its own (inherited from the level above)
    new TEXT,                     -- after the change; NULL = no longer set at this level
    by_user INTEGER NOT NULL,     -- who changed it (user ID)
    via TEXT NOT NULL,            -- chat (asked in chat) / command (/ds persona) / undo
    request TEXT,                 -- the request at the time, for looking things up later
    reverted INTEGER NOT NULL DEFAULT 0  -- this change has since been undone
);
"""

MSGMAP_KEEP = 3000  # how many recent message-number mappings to keep per channel
# columns that databases from older versions lack: added automatically on startup, existing data stays as it is
_MIGRATIONS = (
    ("msgmap", "author_id", "INTEGER"),
    ("msgmap", "author_name", "TEXT"),
    ("conversations", "reviewed_upto", "INTEGER NOT NULL DEFAULT 0"),
)


def open_private_db(path: str, schema: str) -> sqlite3.Connection:
    """Open (or create) an SQLite database only the bot itself can read or write: chat history, profile cards and long-term memory are all stored this way."""
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
    """Only the bot itself may read/write it (other accounts on a NAS can't see it). Best effort."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(path + suffix, 0o600)
        except OSError:
            pass


@dataclass
class Turn:
    id: int
    role: str
    content: Any  # str or list[dict]
    created_at: float = 0.0


@dataclass
class Conversation:
    channel_id: int
    guild_id: int
    summary: str | None = None
    next_short_id: int = 1
    turns: list[Turn] = field(default_factory=list)
    reviewed_upto: int = 0  # how far the memory review has got (a turn id)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # chat history and the contents of files it read live in the database: only the bot itself may read/write it
        self.db = open_private_db(self.path, "")
        self.db.executescript(SCHEMA)  # missing tables are created in the new format; tables that already exist get their missing columns added below
        self._migrate()
        # calls left halfway by the last run (power loss, forced shutdown) will never get a result
        self.db.execute("UPDATE usage SET outcome = 'error' WHERE outcome = 'pending'")
        self.db.commit()

    def _migrate(self) -> None:
        """Tables created by older versions lack a few columns: add them (newly created tables are built from SCHEMA and already have them)."""
        for table, column, decl in _MIGRATIONS:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                if column == "reviewed_upto":
                    # chat from before the upgrade isn't reviewed retroactively (back then nobody recorded who said what, and reviewing a big backlog at once costs money too): reviewing starts from now
                    self.db.execute(
                        "UPDATE conversations SET reviewed_upto = "
                        "(SELECT COALESCE(MAX(id), 0) FROM turns WHERE turns.channel_id = conversations.channel_id)"
                    )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ---- setting overrides ----
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

    # ---- conversations ----
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
        """short_id -> message_id (the most recent batch)"""
        rows = self.db.execute(
            "SELECT short_id, message_id FROM msgmap WHERE channel_id = ? ORDER BY short_id DESC LIMIT ?",
            (channel_id, MSGMAP_KEEP),
        ).fetchall()
        return {r["short_id"]: r["message_id"] for r in rows}

    def load_authors(self, channel_id: int, short_ids: Iterable[int]) -> dict[int, tuple[int, str]]:
        """Message number -> (sender, their name at the time). The bot's own messages, and ones recorded by older versions (without an author), aren't included."""
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

    def set_reviewed(self, conv: Conversation, turn_id: int) -> None:
        """How far the memory review has got (it only moves forward, never back)."""
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
        """Append one round (user + assistant). conv.turns and conv.next_short_id are updated to match.

        assigned: the numbers handed out this round, as (number, message id) or (number, message id, sender, name) (sender is None for bots)."""
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
        # round-trip through JSON so what's in memory is exactly what will be read back from the database later
        conv.turns.append(Turn(user_id, "user", json.loads(_dump(user_content)), now))
        conv.turns.append(Turn(assistant_id, "assistant", json.loads(_dump(assistant_content)), now))

    def compact(self, conv: Conversation, summary: str, remove_count: int) -> None:
        """Replace the oldest remove_count turns with a summary."""
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
        """Channels on this server that have conversation history (or a summary)."""
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

    # ---- persona change history ----
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
        """The most recent persona change in these scopes that hasn't been undone yet (used for undo; an undo doesn't count as a change itself, so undoing repeatedly steps further and further back)."""
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

    # ---- usage ----
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
        # opening links and finding images online are only recorded to count them (they aren't DeepSeek calls), so they're left out of the call counts and outcome stats
        where.append("kind NOT IN ('link', 'image')")
        clause = " AND ".join(where)
        totals = self.db.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(output),0) AS output, COALESCE(SUM(cost),0) AS cost FROM usage WHERE {clause}",
            args,
        ).fetchone()
        # the cache hit rate only counts chat requests: searches and summaries are separate requests whose prefixes differ anyway
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
