"""Profile cards and long-term memory: each kept in its own SQLite file (data/cards.sqlite3 and data/memory.sqlite3 by default), apart from the chat history, so they're easy to back up or move.

- Profile cards: one per person per server, holding what that person has said about themselves (what to call them, their character, likes, running jokes). Admins can't see them;
  the person views, edits or clears their own with /ds-rp; the bot owner can only delete a person's cards wholesale by user ID (without seeing them). The last few old versions of each card are kept.
- Long-term memory: a numbered list per server, about things in the group (agreements, big events, long-running jokes, ongoing storylines).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .store import open_private_db

CARD_VERSIONS_KEEP = 5  # how many old versions to keep per person (so a broken card can be brought back)
ANY = object()  # put's expect left out: write whatever version is there now

CARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,           -- the name last seen (used to look up profile cards by name)
    text TEXT NOT NULL,
    updated_at REAL NOT NULL,
    via TEXT NOT NULL,            -- chat (asked to remember in chat) / review (memory review) / command (/ds-rp) / restore (Undo)
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS card_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,           -- an old version that was replaced
    replaced_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_card_versions ON card_versions (guild_id, user_id, id);
CREATE TABLE IF NOT EXISTS card_tombstones (
    guild_id INTEGER NOT NULL,    -- 0 = every server (deleted by the bot owner)
    user_id INTEGER NOT NULL,
    deleted_at REAL NOT NULL,     -- memory reviews no longer use chat from before this to write this person's card (so a deleted card isn't "remembered" again)
    PRIMARY KEY (guild_id, user_id)
);
"""

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    guild_id INTEGER NOT NULL,
    no INTEGER NOT NULL,          -- the number within this server (numbers of deleted entries aren't reused, so edits don't hit the wrong one)
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    via TEXT NOT NULL,            -- chat / review / command
    PRIMARY KEY (guild_id, no)
);
CREATE TABLE IF NOT EXISTS memory_seq (
    guild_id INTEGER PRIMARY KEY,
    next_no INTEGER NOT NULL
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


def clean_card(text: str) -> str:
    """Profile card text: normalize line breaks, and strip leading/trailing whitespace and extra blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def clean_memory(text: str) -> str:
    """One long-term memory entry: a single line."""
    return " ".join(text.split())


class CardStore:
    def __init__(self, path: str):
        self.path = str(path)
        self.db = open_private_db(self.path, CARD_SCHEMA)

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
        """Write a new profile card (the old one is kept as an old version). Returns whether anything changed.

        Empty text: delete this card, but keep the old versions (Undo can still bring it back). To delete the old versions too, use delete.
        expect: which version the card should be at before writing (updated_at; None = there should be no card yet). If it doesn't match (someone just changed or deleted it), nothing is written."""
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
        """When this person's card was last deleted (cleared by them, forgotten at their request in chat, or deleted by the bot owner). None if it never was."""
        row = self.db.execute(
            "SELECT MAX(deleted_at) AS t FROM card_tombstones WHERE user_id = ? AND guild_id IN (?, 0)", (user_id, guild_id)
        ).fetchone()
        return row["t"] if row is not None else None

    def _trim_versions(self, guild_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM card_versions WHERE guild_id = ? AND user_id = ? AND id NOT IN "
            "(SELECT id FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?)",
            (guild_id, user_id, guild_id, user_id, CARD_VERSIONS_KEEP),
        )

    def restore_previous(self, guild_id: int, user_id: int, *, name: str = "", now: float | None = None) -> str | None:
        """Go back to the previous version (the current one is dropped; a card that was just deleted can be brought back too). Returns None when there's no old version."""
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT id, text FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id, user_id),
        ).fetchone()
        if row is None:
            return None
        current = self.get(guild_id, user_id)
        with self.db:
            self.db.execute("DELETE FROM card_versions WHERE id = ?", (row["id"],))
            self.db.execute(
                "INSERT INTO cards (guild_id, user_id, name, text, updated_at, via) VALUES (?, ?, ?, ?, ?, 'restore') "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at, "
                "via = excluded.via",
                (guild_id, user_id, (current.name if current else "") or name, row["text"], now),
            )
        return row["text"]

    def touch_name(self, guild_id: int, user_id: int, name: str) -> None:
        """Record this person's current name (so after a nickname change, the card can be found by the new name too)."""
        if not name:
            return
        with self.db:
            self.db.execute(
                "UPDATE cards SET name = ? WHERE guild_id = ? AND user_id = ? AND name != ?", (name, guild_id, user_id, name)
            )

    def delete(self, guild_id: int, user_id: int, *, now: float | None = None) -> bool:
        """Delete this person's card on this server, old versions included (/ds-rp Clear). Returns whether there was anything."""
        now = time.time() if now is None else now
        with self.db:
            cur = self.db.execute("DELETE FROM cards WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            old = self.db.execute("DELETE FROM card_versions WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            self._tombstone(guild_id, user_id, now)
        return cur.rowcount > 0 or old.rowcount > 0

    def delete_user(self, user_id: int, *, now: float | None = None) -> tuple[int, int]:
        """Delete this person's cards on every server, old versions included (for the bot owner). Returns (cards deleted, old versions deleted)."""
        now = time.time() if now is None else now
        with self.db:
            cur = self.db.execute("DELETE FROM cards WHERE user_id = ?", (user_id,))
            old = self.db.execute("DELETE FROM card_versions WHERE user_id = ?", (user_id,))
            self._tombstone(0, user_id, now)
        return cur.rowcount, old.rowcount

    def names(self, guild_id: int) -> list[str]:
        """The names of the people on this server with a profile card (sorted; they're listed in the prompt, so the cache is only affected when they change)."""
        rows = self.db.execute("SELECT name FROM cards WHERE guild_id = ? AND name != ''", (guild_id,)).fetchall()
        return sorted({r["name"] for r in rows})

    def count(self, guild_id: int) -> int:
        return int(self.db.execute("SELECT COUNT(*) AS n FROM cards WHERE guild_id = ?", (guild_id,)).fetchone()["n"])

    def find(self, guild_id: int, wanted: str, *, limit: int = 3) -> list[Card]:
        """Find profile cards by name: exact name matches first (ignoring case); if there are none, names that contain it."""
        key = " ".join(wanted.split()).lstrip("@＠").strip().casefold()
        if not key:
            return []
        rows = [self._card(r) for r in self.db.execute("SELECT * FROM cards WHERE guild_id = ?", (guild_id,))]
        exact = [c for c in rows if c.name.casefold() == key]
        if exact:
            return exact[:limit]
        return [c for c in rows if _contains(c.name.casefold(), key) or (len(c.name) >= 2 and _contains(key, c.name.casefold()))][:limit]


def _contains(hay: str, needle: str) -> bool:
    """Substring match for find; a needle made of ASCII letters and digits must match as a whole word ("al" isn't in "sally").

    The Chinese edition matches plain substrings, which suits short Chinese names; with Latin names that would hand over the wrong person's card."""
    if needle.isascii():
        return re.search(rf"(?<![0-9a-z]){re.escape(needle)}(?![0-9a-z])", hay) is not None
    return needle in hay


class MemoryStore:
    def __init__(self, path: str):
        self.path = str(path)
        self.db = open_private_db(self.path, MEMORY_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def entries(self, guild_id: int) -> list[Memory]:
        return [
            Memory(r["no"], r["text"], r["updated_at"], r["via"])
            for r in self.db.execute("SELECT no, text, updated_at, via FROM memories WHERE guild_id = ? ORDER BY no", (guild_id,))
        ]

    def count(self, guild_id: int) -> int:
        return int(self.db.execute("SELECT COUNT(*) AS n FROM memories WHERE guild_id = ?", (guild_id,)).fetchone()["n"])

    def get(self, guild_id: int, no: int) -> Memory | None:
        r = self.db.execute("SELECT no, text, updated_at, via FROM memories WHERE guild_id = ? AND no = ?", (guild_id, no)).fetchone()
        return Memory(r["no"], r["text"], r["updated_at"], r["via"]) if r is not None else None

    def add(self, guild_id: int, text: str, *, via: str, now: float | None = None) -> int:
        """Add an entry and return its number. The caller checks the entry limit and the length first."""
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT next_no FROM memory_seq WHERE guild_id = ?", (guild_id,)).fetchone()
            no = row["next_no"] if row is not None else 1
            self.db.execute(
                "INSERT INTO memories (guild_id, no, text, created_at, updated_at, via) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, no, clean_memory(text), now, now, via),
            )
            self.db.execute(
                "INSERT INTO memory_seq (guild_id, next_no) VALUES (?, ?) ON CONFLICT (guild_id) DO UPDATE SET next_no = excluded.next_no",
                (guild_id, no + 1),
            )
        return no

    def edit(self, guild_id: int, no: int, text: str, *, via: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.db:
            cur = self.db.execute(
                "UPDATE memories SET text = ?, updated_at = ?, via = ? WHERE guild_id = ? AND no = ?",
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


# ---------------------------------------------------------------------------
# Memory review (the model's output after reviewing a stretch of chat)
# ---------------------------------------------------------------------------

_INDEX = re.compile(r"[0-9]{1,9}")


def coerce_no(value: Any) -> int | None:
    """A long-term memory number: 7, "7", "(7)", "（7）", "#7" and the Chinese "第7条" are all accepted."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip().strip("()（）#＃").removeprefix("第").removesuffix("条").strip()
        if _INDEX.fullmatch(text):
            return int(text)
    return None


@dataclass
class MemoryOp:
    op: str  # add / edit / delete
    no: int | None = None
    text: str | None = None


def parse_memory_op(item: Any) -> MemoryOp | None:
    """{"add": "…"} / {"edit": 7, "text": "…"} / {"delete": 7} (the {"op": "add", "text": "…"} form is accepted too)."""
    if not isinstance(item, dict):
        return None
    op = str(item.get("op", "")).strip().lower()
    if "add" in item or op == "add":
        text = item.get("add") if isinstance(item.get("add"), str) else item.get("text")
        return MemoryOp("add", text=text.strip()) if isinstance(text, str) and text.strip() else None
    if "edit" in item or op == "edit":
        no = coerce_no(item.get("edit", item.get("no")))
        text = item.get("text")
        if no is not None and isinstance(text, str) and text.strip():
            return MemoryOp("edit", no=no, text=text.strip())
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


def parse_review(content: str) -> tuple[list[tuple[int, str, str | None]], list[MemoryOp]] | None:
    """Memory review output → ([(message number, new complete profile card, who it's about)], [long-term memory changes]). Returns None when it isn't valid JSON."""
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
    return cards, ops
