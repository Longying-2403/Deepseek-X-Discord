"""人设卡和长期记忆：各存一个 SQLite 文件（默认 data/cards.sqlite3、data/memory.sqlite3），和聊天记录分开，方便备份、搬家。

- 人设卡：每个服务器里每人一张，记本人说的、关于自己的事（称呼、设定、喜好、梗）。管理员看不到；
  本人用 /ds-rp 查看、修改、清空；bot 主人只能按用户 ID 整个删掉（看不到内容）。每人保留最近几个旧版本。
- 长期记忆：每个服务器一份带编号的清单，记群里的事（约定、大事、长期的梗、进行中的剧情）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .store import open_private_db

CARD_VERSIONS_KEEP = 5  # 每人保留几个旧版本（写坏了能找回）
ANY = object()  # put 的 expect 不填：不管现在是哪个版本都写

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
    replaced_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_card_versions ON card_versions (guild_id, user_id, id);
CREATE TABLE IF NOT EXISTS card_tombstones (
    guild_id INTEGER NOT NULL,    -- 0 = 所有服务器（bot 主人删的）
    user_id INTEGER NOT NULL,
    deleted_at REAL NOT NULL,     -- 整理记忆时，这之前的聊天不再拿来写这个人的卡（免得删掉的又被“想起来”）
    PRIMARY KEY (guild_id, user_id)
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

    def _trim_versions(self, guild_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM card_versions WHERE guild_id = ? AND user_id = ? AND id NOT IN "
            "(SELECT id FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?)",
            (guild_id, user_id, guild_id, user_id, CARD_VERSIONS_KEEP),
        )

    def restore_previous(self, guild_id: int, user_id: int, *, name: str = "", now: float | None = None) -> str | None:
        """改回上一个版本（现在这份就不要了；刚删掉的也能找回来）。没有旧版本时返回 None。"""
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
            self._tombstone(guild_id, user_id, now)
        return cur.rowcount > 0 or old.rowcount > 0

    def delete_user(self, user_id: int, *, now: float | None = None) -> tuple[int, int]:
        """删掉这个人在所有服务器的卡，连同旧版本（bot 主人用）。返回 (删了几张卡, 删了几个旧版本)。"""
        now = time.time() if now is None else now
        with self.db:
            cur = self.db.execute("DELETE FROM cards WHERE user_id = ?", (user_id,))
            old = self.db.execute("DELETE FROM card_versions WHERE user_id = ?", (user_id,))
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
        """按名字找人设卡：先找名字完全一样的（不分大小写），没有再找名字里包含它的。"""
        key = " ".join(wanted.split()).lstrip("@＠").strip().casefold()
        if not key:
            return []
        rows = [self._card(r) for r in self.db.execute("SELECT * FROM cards WHERE guild_id = ?", (guild_id,))]
        exact = [c for c in rows if c.name.casefold() == key]
        if exact:
            return exact[:limit]
        return [c for c in rows if key in c.name.casefold() or (len(c.name) >= 2 and c.name.casefold() in key)][:limit]


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
        """记一条，返回编号。调用方先检查条数上限和字数。"""
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
# 整理记忆（模型回顾一段聊天之后的输出）
# ---------------------------------------------------------------------------

_INDEX = re.compile(r"[0-9]{1,9}")


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


def parse_memory_op(item: Any) -> MemoryOp | None:
    """{"add": "…"} / {"edit": 7, "text": "…"} / {"delete": 7}（也认 {"op": "add", "text": "…"} 这种写法）。"""
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
    """整理记忆的输出 → ([(消息编号, 新的完整人设卡, 写的是谁)], [长期记忆的改动])。不是合法 JSON 时返回 None。"""
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
