"""SQLite 存储：参数覆盖、每个频道的对话记录（只追加）、消息编号映射、用量账本。

单进程单线程使用；每次操作都很短，直接在事件循环里同步执行。
"""

from __future__ import annotations

import json
import logging
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
    via TEXT NOT NULL,            -- chat（聊天里让它改）/ command（/dsadmin 人设）/ undo（撤销）
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
CREATE TABLE IF NOT EXISTS dm_reach (
    user_id INTEGER PRIMARY KEY,  -- 让它主动私信自己的人（/ds-dm 主动；每个人只管自己的）
    level TEXT NOT NULL,          -- low / mid / high（关掉就删掉这一行）
    from_hour INTEGER,            -- 只在几点到几点之间发（都是 NULL = 全天）；可以跨夜（22 到 2）
    until_hour INTEGER,
    updated_at REAL NOT NULL
);
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
CREATE TABLE IF NOT EXISTS chat_syncs (
    channel_id INTEGER PRIMARY KEY, -- 连通的频道（bot 主人用 /dsowner chatsync 连的）
    sync_id INTEGER NOT NULL,       -- 哪一组：负数。这一组共用的聊天记录按它存（conversations、turns、msgmap 的 channel_id 填它）
    guild_id INTEGER NOT NULL,      -- 频道在哪个服务器
    name TEXT NOT NULL DEFAULT '',  -- 连的时候的频道名（频道删掉了、bot 被移出那个服务器了，列名单时照样认得出）
    joined_at REAL NOT NULL,
    joined_turn INTEGER NOT NULL DEFAULT 0,  -- 连进来时最新的一条记录：断开时只拿回这之后它的那些（之前那次连通拿回过的不再拿）
    joined_sid INTEGER NOT NULL DEFAULT 0,   -- 连进来时共用记录的下一个编号：它这次连通期间的消息编号都不小于它
    joined_next INTEGER NOT NULL DEFAULT 0   -- 连进来时它自己那份的下一个编号：断开时比这大，就是连通期间旧版本往里写过
);
CREATE INDEX IF NOT EXISTS idx_msgmap_msgid ON msgmap (message_id);
CREATE TABLE IF NOT EXISTS interests (
    channel_id INTEGER NOT NULL,   -- 这个频道的人感兴趣的话题（冒泡时去搜新鲜事用，/dsadmin 兴趣 管）
    topic TEXT NOT NULL,
    kind TEXT NOT NULL,            -- auto（它从聊天里看出来、搜过的）/ manual（管理员加的）/ blocked（管理员删掉、不让搜的）
    added_at REAL NOT NULL,
    used_at REAL NOT NULL DEFAULT 0,  -- 上一次拿它去搜是什么时候
    uses INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, topic)
);
CREATE TABLE IF NOT EXISTS bubble_state (
    channel_id INTEGER PRIMARY KEY,  -- 开了冒泡的频道：重启以后也记得今天冒过几次、上次有没有人理
    last_at REAL NOT NULL DEFAULT 0,
    waiting INTEGER NOT NULL DEFAULT 0,  -- 连着冒了几次泡都没人说话（频道：1 次就等有人说话；主动私信：3 次）
    day TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 0     -- day 那天冒了几次
);
CREATE TABLE IF NOT EXISTS bubble_links (
    channel_id INTEGER NOT NULL,   -- 冒泡时分享过的链接：不重复发
    url TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (channel_id, url)
);
CREATE TABLE IF NOT EXISTS chat_sync_seq (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_no INTEGER NOT NULL        -- 下一个新组的编号（组的 ID 是它的负数；散掉的组的编号不再用）
);
CREATE TABLE IF NOT EXISTS chat_sync_summary (
    sync_id INTEGER NOT NULL,       -- 连通组：它共用的记录压缩出的摘要里有这个频道的事
    channel_id INTEGER NOT NULL,    -- （这个频道断开以后清空记忆，原话都压掉了也知道要清掉那份摘要）
    PRIMARY KEY (sync_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_sync_summary_channel ON chat_sync_summary (channel_id);
CREATE TABLE IF NOT EXISTS chat_sync_kept (
    sync_id INTEGER NOT NULL,       -- 断开时没能拿回自己那部分的频道（它自己那份记录在连通期间被旧版本写过）：
    channel_id INTEGER NOT NULL,    -- 那部分留在组里，照样在组里整理记忆
    from_turn INTEGER NOT NULL,     -- 那次连进来时最新的那条记录（这之后的是那次连通期间说的）
    PRIMARY KEY (sync_id, channel_id)
);
CREATE TABLE IF NOT EXISTS chat_unvetted (
    channel_id INTEGER PRIMARY KEY,   -- 哪份记录（连通组共用的那份是负数，频道自己那份是频道 ID）
    turns TEXT NOT NULL DEFAULT '[]', -- 没问过模型就记进去的那几轮（turns 的 id）：带着它们的请求过了才划掉，被拒了换成一句
    edits TEXT NOT NULL DEFAULT '[]'  -- 连通组的记录里直接改成了新内容、还没问过模型的那几条消息（message id）
);                                    -- （记在库里：重启以后照样认得，不然被拒了换不掉，组里的频道从此每一轮都被拒）
CREATE TABLE IF NOT EXISTS chat_sync_scrub (
    sync_id INTEGER PRIMARY KEY,        -- 服务器退出服务器组时这个连通组正在跑一轮：那一轮写完以后要再查一遍（scrub_cross）
    lookups INTEGER NOT NULL DEFAULT 0, -- 那一轮按原来的组要翻记录、查人设卡的动作也拿掉
    whole INTEGER NOT NULL DEFAULT 0    -- 整份记录都查（组里原来有的服务器退出了服务器组）
);                                      -- （记在库里：那一轮写完、还没查就停了的话，下次启动时照样查）
"""

log = logging.getLogger(__name__)

MSGMAP_KEEP = 3000  # 每个频道保留最近多少个消息编号的映射
# 旧版本的数据库缺的列：启动时自动补上，原有数据不动
_MIGRATIONS = (
    ("msgmap", "author_id", "INTEGER"),
    ("msgmap", "author_name", "TEXT"),
    ("conversations", "reviewed_upto", "INTEGER NOT NULL DEFAULT 0"),
    # 连通的频道共用一份记录：每条记录、每个消息编号是哪个频道的（NULL = 就是记录所属的那个频道，没连通的都是 NULL）
    ("turns", "origin", "INTEGER"),
    ("msgmap", "origin", "INTEGER"),
    # 断开时拿回过共用记录里那部分的记录：编号中间跳了一段，旧的编号映射按条数留（不按编号差）
    ("conversations", "took_back", "INTEGER NOT NULL DEFAULT 0"),
    ("chat_syncs", "joined_turn", "INTEGER NOT NULL DEFAULT 0"),
    ("chat_syncs", "joined_sid", "INTEGER NOT NULL DEFAULT 0"),
    ("chat_syncs", "joined_next", "INTEGER NOT NULL DEFAULT 0"),
    # 连通组的记录几个频道分开整理，不是按先后连着整理完的：这一条整理过了（断开时拿回去的也照这个算，免得整理两遍）
    ("turns", "reviewed", "INTEGER NOT NULL DEFAULT 0"),
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
    origin: int | None = None  # 连通的几个频道共用的记录里：这一条是哪个频道的（没连通的记录都是 None）


@dataclass
class Conversation:
    channel_id: int  # 记录按它存：没连通的频道就是频道自己；连通的几个频道共用一份，是连通组的 ID（负数）
    guild_id: int  # 连通组的记录是 0（几个服务器共用）
    summary: str | None = None
    next_short_id: int = 1
    turns: list[Turn] = field(default_factory=list)
    reviewed_upto: int = 0  # 整理记忆整理到哪条记录了（记录的 id）
    sparse: bool = False  # 断开连通时拿回过共用记录里的那部分：编号中间跳了一段（旧的编号映射按条数留）


class SyncError(ValueError):
    """连通、断开做不了（频道已经在别的组里、不在组里……）：说明给 bot 主人看。"""


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
        # 连通的频道 -> 连通组（每条消息进来都要查：放在内存里，按连进来的先后排）
        self._syncs: dict[int, int] = {
            r["channel_id"]: r["sync_id"]
            for r in self.db.execute("SELECT channel_id, sync_id FROM chat_syncs ORDER BY joined_at, channel_id")
        }

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

    # ---- 冒泡：兴趣、状态、分享过的链接 ----
    def interests(self, channel_id: int) -> list[tuple[str, str, float, int]]:
        """这个频道的兴趣：[(话题, auto / manual / blocked, 上次拿去搜的时间, 搜过几次)]，新加的在前。"""
        rows = self.db.execute(
            "SELECT topic, kind, used_at, uses FROM interests WHERE channel_id = ? ORDER BY added_at DESC, rowid DESC", (channel_id,)
        ).fetchall()
        return [(r["topic"], r["kind"], r["used_at"], r["uses"]) for r in rows]

    def set_interest(self, channel_id: int, topic: str, kind: str, now: float) -> None:
        """加一个话题，或者改它是哪一种（管理员加的、不让搜的）。"""
        with self.db:
            self.db.execute(
                "INSERT INTO interests (channel_id, topic, kind, added_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (channel_id, topic) DO UPDATE SET kind = excluded.kind",
                (channel_id, topic, kind, now),
            )

    def note_interests(self, channel_id: int, topics: list[str], now: float, keep: int) -> None:
        """它从聊天里看出来的话题：没记过的记下（已经记着的、不让搜的都不动）。自己看出来的最多留 keep 个。"""
        with self.db:
            for topic in topics:
                self.db.execute(
                    "INSERT OR IGNORE INTO interests (channel_id, topic, kind, added_at) VALUES (?, ?, 'auto', ?)", (channel_id, topic, now)
                )
            self._trim_interests(channel_id, keep)

    def _trim_interests(self, channel_id: int, keep: int) -> None:
        self.db.execute(
            "DELETE FROM interests WHERE channel_id = ? AND kind = 'auto' AND topic NOT IN "
            "(SELECT topic FROM interests WHERE channel_id = ? AND kind = 'auto' "
            "ORDER BY MAX(used_at, added_at) DESC, topic LIMIT ?)",
            (channel_id, channel_id, keep),
        )

    def use_interest(self, channel_id: int, topic: str, now: float, keep: int) -> None:
        """它拿这个话题去搜了一次：没记过的记成它自己看出来的（auto）。自己看出来的最多留 keep 个，很久没搜的先删。"""
        with self.db:
            self.db.execute(
                "INSERT INTO interests (channel_id, topic, kind, added_at, used_at, uses) VALUES (?, ?, 'auto', ?, ?, 1) "
                "ON CONFLICT (channel_id, topic) DO UPDATE SET used_at = excluded.used_at, uses = uses + 1",
                (channel_id, topic, now, now),
            )
            self._trim_interests(channel_id, keep)

    def clear_interests(self, channel_id: int) -> None:
        """这个频道（私信）记着的兴趣、发过的链接全部删掉（关掉主动私信时）。"""
        with self.db:
            self.db.execute("DELETE FROM interests WHERE channel_id = ?", (channel_id,))
            self.db.execute("DELETE FROM bubble_links WHERE channel_id = ?", (channel_id,))
            self.db.execute("DELETE FROM bubble_state WHERE channel_id = ?", (channel_id,))

    def delete_interest(self, channel_id: int, topic: str) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM interests WHERE channel_id = ? AND topic = ?", (channel_id, topic)).rowcount > 0

    def bubble_state(self, channel_id: int) -> tuple[float, int, str, int]:
        """(上次冒泡的时间, 连着几次没人理, 哪一天, 那天冒了几次)。"""
        r = self.db.execute("SELECT last_at, waiting, day, count FROM bubble_state WHERE channel_id = ?", (channel_id,)).fetchone()
        return (r["last_at"], int(r["waiting"]), r["day"], r["count"]) if r is not None else (0.0, 0, "", 0)

    def set_bubble_state(self, channel_id: int, last_at: float, waiting: int, day: str, count: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO bubble_state (channel_id, last_at, waiting, day, count) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (channel_id) DO UPDATE SET last_at = excluded.last_at, waiting = excluded.waiting, "
                "day = excluded.day, count = excluded.count",
                (channel_id, last_at, int(waiting), day, count),
            )

    def bubble_links(self, channel_id: int, limit: int = 20) -> list[str]:
        rows = self.db.execute("SELECT url FROM bubble_links WHERE channel_id = ? ORDER BY at DESC LIMIT ?", (channel_id, limit)).fetchall()
        return [r["url"] for r in rows]

    def add_bubble_links(self, channel_id: int, urls: list[str], now: float, keep: int = 100) -> None:
        with self.db:
            for url in urls:
                self.db.execute("INSERT OR REPLACE INTO bubble_links (channel_id, url, at) VALUES (?, ?, ?)", (channel_id, url, now))
            self.db.execute(
                "DELETE FROM bubble_links WHERE channel_id = ? AND url NOT IN "
                "(SELECT url FROM bubble_links WHERE channel_id = ? ORDER BY at DESC LIMIT ?)",
                (channel_id, channel_id, keep),
            )

    # ---- 对话 ----
    def load_conversation(self, channel_id: int, guild_id: int) -> Conversation:
        row = self.db.execute(
            "SELECT guild_id, summary, next_short_id, reviewed_upto, took_back FROM conversations WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        conv = Conversation(channel_id=channel_id, guild_id=guild_id)
        if row is not None:
            conv.summary = row["summary"]
            conv.next_short_id = row["next_short_id"]
            conv.reviewed_upto = row["reviewed_upto"] or 0
            conv.sparse = bool(row["took_back"])
        for r in self.db.execute(
            "SELECT id, role, content, created_at, origin FROM turns WHERE channel_id = ? ORDER BY id", (channel_id,)
        ):
            conv.turns.append(Turn(r["id"], r["role"], json.loads(r["content"]), r["created_at"] or 0.0, r["origin"]))
        return conv

    def load_msgmap(self, channel_id: int) -> dict[int, int]:
        """short_id -> message_id（最近的一批）"""
        rows = self.db.execute(
            "SELECT short_id, message_id FROM msgmap WHERE channel_id = ? ORDER BY short_id DESC LIMIT ?",
            (channel_id, MSGMAP_KEEP),
        ).fetchall()
        return {r["short_id"]: r["message_id"] for r in rows}

    def load_msg_origins(self, channel_id: int) -> dict[int, int]:
        """连通组的记录里：short_id -> 那条消息在哪个频道（最近的一批；没连通的记录是空的）。"""
        rows = self.db.execute(
            "SELECT short_id, origin FROM msgmap WHERE channel_id = ? AND origin IS NOT NULL ORDER BY short_id DESC LIMIT ?",
            (channel_id, MSGMAP_KEEP),
        ).fetchall()
        return {r["short_id"]: r["origin"] for r in rows}

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

    def recent_authors(self, channel_id: int, limit: int, origin: int | None = None) -> list[tuple[int, str]]:
        """这个频道最近说过话的人（不含 bot）：[(用户 ID, 最近一次的名字)]，最近的在前。
        origin：连通组的记录（channel_id 是组的 ID）里只看这个频道的。"""
        where = "" if origin is None else " AND origin = ?"
        rows = self.db.execute(
            "SELECT author_id, author_name FROM msgmap WHERE channel_id = ? AND author_id IS NOT NULL"
            + where
            + " ORDER BY short_id DESC LIMIT ?",
            (channel_id, limit * 5) if origin is None else (channel_id, origin, limit * 5),
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

    def update_turns(self, pairs: Iterable[tuple[int, Any]]) -> None:
        """一次改写好几条记录（一个事务里：要么都改了，要么都没改）。"""
        with self.db:
            self.db.executemany("UPDATE turns SET content = ? WHERE id = ?", [(_dump(content), turn_id) for turn_id, content in pairs])

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
        origin: int | None = None,
    ) -> None:
        """追加一轮（user + assistant）。conv.turns 和 conv.next_short_id 同步更新。

        assigned：这一轮新编的号，(编号, 消息 id) 或 (编号, 消息 id, 发消息的人, 名字)（bot 发的人填 None）。
        origin：连通组共用的记录里，这一轮是哪个频道的（这一轮的消息都在那个频道；没连通的不填）。"""
        now = time.time() if now is None else now
        with self.db:
            cur = self.db.execute(
                "INSERT INTO turns (channel_id, role, content, created_at, origin) VALUES (?, 'user', ?, ?, ?)",
                (conv.channel_id, _dump(user_content), now, origin),
            )
            user_id = cur.lastrowid
            cur = self.db.execute(
                "INSERT INTO turns (channel_id, role, content, created_at, origin) VALUES (?, 'assistant', ?, ?, ?)",
                (conv.channel_id, _dump(assistant_content), now, origin),
            )
            assistant_id = cur.lastrowid
            rows = []
            for item in assigned:
                sid, mid, *who = item
                author, name = (who + [None, None])[:2]
                rows.append((conv.channel_id, sid, mid, author, name, origin))
            self.db.executemany(
                "INSERT OR REPLACE INTO msgmap (channel_id, short_id, message_id, author_id, author_name, origin) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self.db.execute(
                "INSERT INTO conversations (channel_id, guild_id, summary, next_short_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (channel_id) DO UPDATE SET "
                "next_short_id = excluded.next_short_id, updated_at = excluded.updated_at",
                (conv.channel_id, conv.guild_id, conv.summary, conv.next_short_id, now),
            )
            self._trim_msgmap(conv.channel_id, conv.next_short_id, sparse=conv.sparse)
        # JSON 往返一次，保证内存里的内容和以后从库里读出来的完全一致
        conv.turns.append(Turn(user_id, "user", json.loads(_dump(user_content)), now, origin))
        conv.turns.append(Turn(assistant_id, "assistant", json.loads(_dump(assistant_content)), now, origin))

    def _trim_msgmap(self, key: int, next_short_id: int, *, sparse: bool = False) -> None:
        """只留最近的 MSGMAP_KEEP 个消息编号的映射（调用方开着事务）。编号中间会跳过一大段的记录按条数留：断开连通时拿回过
        共用记录里那部分的（sparse），还有连通组共用的记录（key 是负数：有频道连进来时编号接在它自己用过的后面，会往后跳）。
        按编号差算会把跳之前、还在记录里的那些也删掉（删了它们，之后有人删消息、改消息就改不到记录了）。"""
        if not sparse and key >= 0:
            self.db.execute("DELETE FROM msgmap WHERE channel_id = ? AND short_id < ?", (key, next_short_id - MSGMAP_KEEP))
            return
        self.db.execute(
            "DELETE FROM msgmap WHERE channel_id = ? AND short_id < "
            "(SELECT short_id FROM msgmap WHERE channel_id = ? ORDER BY short_id DESC LIMIT 1 OFFSET ?)",
            (key, key, MSGMAP_KEEP - 1),
        )

    def message_keys(self, message_ids: Iterable[int]) -> set[int]:
        """这几条消息在哪几份记录里（记录的 ID：频道自己的、连通组的；连通期间、断开以后可能两份都有）。"""
        ids = sorted(set(message_ids))
        out: set[int] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            out.update(r["channel_id"] for r in self.db.execute(f"SELECT DISTINCT channel_id FROM msgmap WHERE message_id IN ({marks})", chunk))
        return out

    def origin_syncs(self, channel_id: int) -> list[int]:
        """共用记录里还有这个频道说过的话的连通组（它现在在的、和断开以后还留着它那部分的）：原话还在的，和压进了摘要的。"""
        rows = self.db.execute(
            "SELECT DISTINCT channel_id FROM turns WHERE origin = ? AND channel_id < 0 "
            "UNION SELECT sync_id FROM chat_sync_summary WHERE channel_id = ?",
            (channel_id, channel_id),
        )
        return sorted((int(r[0]) for r in rows), reverse=True)

    def mark_reviewed(self, turn_ids: Iterable[int]) -> None:
        """这几条记录整理过记忆了（连通组的记录几个频道分开整理：整理完一段记一段）。"""
        ids = sorted(set(turn_ids))
        if not ids:
            return
        with self.db:
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                marks = ",".join("?" for _ in chunk)
                self.db.execute(f"UPDATE turns SET reviewed = 1 WHERE id IN ({marks})", chunk)

    def reviewed_turns(self, key: int, after: int) -> set[int]:
        """这份记录里 after 之后已经整理过的记录（连通组分开整理完的那几段；断开时拿回来、在组里整理过的）。"""
        rows = self.db.execute("SELECT id FROM turns WHERE channel_id = ? AND id > ? AND reviewed = 1", (key, after))
        return {int(r["id"]) for r in rows}

    def record_guild(self, channel_id: int) -> int | None:
        """这个频道自己那份记录记下的服务器（没有这份记录是 None）。"""
        row = self.db.execute("SELECT guild_id FROM conversations WHERE channel_id = ?", (channel_id,)).fetchone()
        return int(row[0]) if row is not None and row[0] else None

    def turn_ids(self, key: int) -> set[int]:
        """这份记录现在有的记录（的 id）。"""
        return {int(r["id"]) for r in self.db.execute("SELECT id FROM turns WHERE channel_id = ?", (key,))}

    def load_unvetted(self, key: int) -> tuple[list[int], set[int]]:
        """这份记录里还没问过模型的：(那几轮的 id, 直接改成了新内容的那几条消息)。"""
        row = self.db.execute("SELECT turns, edits FROM chat_unvetted WHERE channel_id = ?", (key,)).fetchone()
        if row is None:
            return [], set()
        return [int(t) for t in json.loads(row["turns"])], {int(m) for m in json.loads(row["edits"])}

    def mark_scrub(self, sync_id: int, *, lookups: bool, whole: bool) -> None:
        """记下这个连通组正在跑的那一轮写完以后要再查一遍（已经记着的只会多查，不会少查）。"""
        with self.db:
            self.db.execute(
                "INSERT INTO chat_sync_scrub (sync_id, lookups, whole) VALUES (?, ?, ?) ON CONFLICT (sync_id) DO UPDATE SET "
                "lookups = MAX(lookups, excluded.lookups), whole = MAX(whole, excluded.whole)",
                (sync_id, int(lookups), int(whole)),
            )

    def pending_scrubs(self) -> dict[int, tuple[bool, bool]]:
        """上次写完以后没来得及再查的连通组：组 -> (lookups, whole)。"""
        rows = self.db.execute("SELECT sync_id, lookups, whole FROM chat_sync_scrub")
        return {int(r["sync_id"]): (bool(r["lookups"]), bool(r["whole"])) for r in rows}

    def pending_scrub(self, sync_id: int) -> tuple[bool, bool] | None:
        """这个连通组记着的、写完以后还没查的：(lookups, whole)，没有是 None。"""
        row = self.db.execute("SELECT lookups, whole FROM chat_sync_scrub WHERE sync_id = ?", (sync_id,)).fetchone()
        return (bool(row["lookups"]), bool(row["whole"])) if row is not None else None

    def clear_scrub(self, sync_id: int) -> None:
        """这个连通组那一轮写完以后要查的查过了。"""
        with self.db:
            self.db.execute("DELETE FROM chat_sync_scrub WHERE sync_id = ?", (sync_id,))

    def save_unvetted(self, key: int, turns: Iterable[int], edits: Iterable[int]) -> None:
        """记下这份记录里还没问过模型的（都过了、都换掉了就删掉这一行）。"""
        listed, changed = sorted(set(turns)), sorted(set(edits))
        with self.db:
            if listed or changed:
                self.db.execute(
                    "INSERT OR REPLACE INTO chat_unvetted (channel_id, turns, edits) VALUES (?, ?, ?)",
                    (key, _dump(listed), _dump(changed)),
                )
            else:
                self.db.execute("DELETE FROM chat_unvetted WHERE channel_id = ?", (key,))

    def record_turns(self, key: int) -> list[tuple[int, str, Any, int | None]]:
        """这份记录的每一条，按先后：[(id, user / assistant, 内容, 哪个频道的)]。"""
        rows = self.db.execute("SELECT id, role, content, origin FROM turns WHERE channel_id = ? ORDER BY id", (key,))
        return [(int(r["id"]), r["role"], json.loads(r["content"]), r["origin"]) for r in rows]

    def msg_origins(self, key: int, short_ids: Iterable[int]) -> dict[int, int]:
        """连通组的记录里，这几个编号的消息各在哪个频道（不在映射里的不在结果里）。"""
        ids = sorted(set(short_ids))
        out: dict[int, int] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in self.db.execute(
                f"SELECT short_id, origin FROM msgmap WHERE channel_id = ? AND short_id IN ({marks}) AND origin IS NOT NULL",
                (key, *chunk),
            ):
                out[int(r["short_id"])] = int(r["origin"])
        return out

    def next_short_id(self, key: int) -> int:
        """这份记录的下一个消息编号（库里记的）。"""
        row = self.db.execute("SELECT next_short_id FROM conversations WHERE channel_id = ?", (key,)).fetchone()
        return int(row["next_short_id"]) if row is not None else 1

    def compact(self, conv: Conversation, summary: str, remove_count: int) -> None:
        """用摘要替换最早的 remove_count 条 turn。"""
        if remove_count <= 0:
            return
        last_removed = conv.turns[remove_count - 1].id
        # 连通组的记录：压进摘要的有哪几个频道的事（那个频道以后清空记忆时，就算它的原话都压掉了，也知道要清掉这份摘要）
        origins = sorted({t.origin for t in conv.turns[:remove_count] if t.origin is not None}) if conv.channel_id < 0 else []
        with self.db:
            self.db.execute("DELETE FROM turns WHERE channel_id = ? AND id <= ?", (conv.channel_id, last_removed))
            self.db.executemany(
                "INSERT OR IGNORE INTO chat_sync_summary (sync_id, channel_id) VALUES (?, ?)", [(conv.channel_id, o) for o in origins]
            )
            self.db.execute(
                "INSERT INTO conversations (channel_id, guild_id, summary, next_short_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (channel_id) DO UPDATE SET "
                "summary = excluded.summary, updated_at = excluded.updated_at",
                (conv.channel_id, conv.guild_id, summary, conv.next_short_id, time.time()),
            )
        conv.summary = summary
        conv.turns = conv.turns[remove_count:]

    def conversation_channels(self, guild_id: int) -> list[int]:
        """这个服务器里有对话记录（或摘要）的频道。连通的频道：连通之前自己的记录，或者共用的记录里有它的。"""
        rows = self.db.execute(
            "SELECT channel_id FROM conversations c WHERE guild_id = ? AND (summary IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM turns t WHERE t.channel_id = c.channel_id)) "
            "UNION SELECT s.channel_id FROM chat_syncs s WHERE s.guild_id = ? "
            "AND EXISTS (SELECT 1 FROM turns t WHERE t.channel_id = s.sync_id AND t.origin = s.channel_id) ORDER BY channel_id",
            (guild_id, guild_id),
        ).fetchall()
        return [r["channel_id"] for r in rows]

    def move_conversation(self, channel_id: int, guild_id: int) -> None:
        """私信换了挂的服务器：记录归到新的服务器下（先清空再换）。"""
        with self.db:
            self.db.execute("UPDATE conversations SET guild_id = ? WHERE channel_id = ?", (guild_id, channel_id))

    def reset_conversation(self, channel_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM turns WHERE channel_id = ?", (channel_id,))
            self.db.execute("DELETE FROM chat_unvetted WHERE channel_id = ?", (channel_id,))  # 记录清空了：没问过模型的也没了
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

    def dm_reach(self, user_id: int) -> tuple[str, int | None, int | None] | None:
        """他让它主动私信自己的设置：(档位, 从几点, 到几点)；没开是 None。"""
        r = self.db.execute("SELECT level, from_hour, until_hour FROM dm_reach WHERE user_id = ?", (user_id,)).fetchone()
        return (r["level"], r["from_hour"], r["until_hour"]) if r is not None else None

    def dm_reach_channels(self) -> dict[int, tuple[int, str, int | None, int | None]]:
        """开了主动私信、又私信过它的人：私信频道 -> (人, 档位, 从几点, 到几点)。"""
        rows = self.db.execute(
            "SELECT h.channel_id, h.user_id, r.level, r.from_hour, r.until_hour FROM dm_reach r "
            "JOIN dm_homes h ON h.user_id = r.user_id WHERE h.channel_id IS NOT NULL"
        )
        return {int(r["channel_id"]): (int(r["user_id"]), r["level"], r["from_hour"], r["until_hour"]) for r in rows}

    def set_dm_reach(self, user_id: int, level: str | None, from_hour: int | None = None, until_hour: int | None = None) -> None:
        """level 是 None：关掉。"""
        with self.db:
            if level is None:
                self.db.execute("DELETE FROM dm_reach WHERE user_id = ?", (user_id,))
            else:
                self.db.execute(
                    "INSERT INTO dm_reach (user_id, level, from_hour, until_hour, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET level = excluded.level, from_hour = excluded.from_hour, "
                    "until_hour = excluded.until_hour, updated_at = excluded.updated_at",
                    (user_id, level, from_hour, until_hour, time.time()),
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

    # ---------------------------------------------------------------- 连通的频道（/dsowner chatsync）

    def sync_of(self, channel_id: int) -> int | None:
        """这个频道在哪个连通组（负数）；没连通是 None。"""
        return self._syncs.get(channel_id)

    def conversation_key(self, channel_id: int) -> int:
        """这个频道现在的聊天记录按哪个 ID 存：连通了是连通组的 ID，不然就是频道自己。"""
        return self._syncs.get(channel_id, channel_id)

    def sync_members(self, sync_id: int) -> list[int]:
        """连通组里的频道，按连进来的先后。"""
        return [c for c, s in self._syncs.items() if s == sync_id]

    def syncs(self) -> dict[int, list[int]]:
        """连通组 -> 组里的频道（按连进来的先后）；组按建的先后。"""
        out: dict[int, list[int]] = {}
        for channel_id, sync_id in self._syncs.items():
            out.setdefault(sync_id, []).append(channel_id)
        return dict(sorted(out.items(), key=lambda item: -item[0]))

    def sync_place(self, channel_id: int) -> tuple[int, str] | None:
        """连的时候记下的 (服务器, 频道名)：频道找不到了（删掉了、bot 被移出那个服务器）时用。"""
        row = self.db.execute("SELECT guild_id, name FROM chat_syncs WHERE channel_id = ?", (channel_id,)).fetchone()
        return (int(row["guild_id"]), row["name"]) if row is not None else None

    def link_channels(
        self, channel: tuple[int, int, str], other: tuple[int, int, str], now: float | None = None
    ) -> tuple[int, list[int]]:
        """把 channel 和 other（都是 (频道, 服务器, 名字)）连通：都没连通就建一个新组（other 排在前面）；有一个已经在
        组里，另一个加进那个组。返回 (组, 这次连进来的频道)。

        连进来的频道自己原来的记录留着不动：连通期间不往里写，只给它自己的频道看（断开后接着用）。共用的记录从这一刻开始，
        编号接在每个连进来的频道自己用过的编号后面：断开时把它自己的那部分拿回去，编号不会撞上。"""
        now = time.time() if now is None else now
        if channel[0] == other[0]:
            raise SyncError("不能把一个频道连到它自己。")
        a, b = self._syncs.get(channel[0]), self._syncs.get(other[0])
        if a is not None and b is not None:
            raise SyncError("这两个频道已经连通了。" if a == b else "两个频道都已经在别的连通组里了：先把其中一个断开。")
        with self.db:
            if a is None and b is None:
                row = self.db.execute("SELECT next_no FROM chat_sync_seq WHERE id = 1").fetchone()
                low = self.db.execute("SELECT MIN(channel_id) AS m FROM conversations").fetchone()["m"]
                # 编号表被单独还原过也不撞上库里已经有的组
                no = max(row["next_no"] if row is not None else 1, 1 - min(0, low or 0))
                self.db.execute(
                    "INSERT INTO chat_sync_seq (id, next_no) VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET next_no = excluded.next_no",
                    (no + 1,),
                )
                sync_id, joining = -no, [other, channel]
            else:
                sync_id = a if a is not None else b  # type: ignore[assignment]
                joining = [other] if a is not None else [channel]
            ids = [c for c, _, _ in joining]
            marks = ",".join("?" for _ in ids)
            nexts = {
                int(r["channel_id"]): int(r["next_short_id"])
                for r in self.db.execute(
                    f"SELECT channel_id, next_short_id FROM conversations WHERE channel_id IN ({marks}, ?)", (*ids, sync_id)
                )
            }
            start = max([1, *nexts.values()])
            self.db.execute(
                "INSERT INTO conversations (channel_id, guild_id, summary, next_short_id, updated_at) VALUES (?, 0, NULL, ?, ?) "
                "ON CONFLICT (channel_id) DO UPDATE SET next_short_id = MAX(conversations.next_short_id, excluded.next_short_id)",
                (sync_id, start, now),
            )
            last = self.db.execute("SELECT MAX(joined_at) AS t FROM chat_syncs").fetchone()["t"]
            newest = self.db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM turns").fetchone()["m"]
            for cid, guild_id, name in joining:
                at = max(now, last + 0.001) if last is not None else now  # 先后要分得出来：重启以后顺序不变
                last = at
                self.db.execute(
                    "INSERT INTO chat_syncs (channel_id, sync_id, guild_id, name, joined_at, joined_turn, joined_sid, joined_next) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cid, sync_id, guild_id, name, at, newest, start, nexts.get(cid, 1)),
                )
            # 又连回同一个组：之前断开时没能拿回去、留在组里整理的那部分不再算（不然这次连通期间说的，组里和它自己那份都会整理）
            self.db.execute(f"DELETE FROM chat_sync_kept WHERE sync_id = ? AND channel_id IN ({marks})", (sync_id, *ids))
        for cid in ids:
            self._syncs[cid] = sync_id
        return sync_id, ids

    def unlink_channel(self, channel_id: int, now: float | None = None) -> tuple[int, list[int], list[int]]:
        """把这个频道从连通组里断开。返回 (组, 断开的频道, 组里还剩的频道)。组里只剩一个时组就散了：剩下的那个也断开
        （也在“断开的频道”里），共用的记录删掉。

        断开的频道把共用记录里自己的那部分（它的频道里说的、它在那里回的）复制回自己原来的记录后面，接着用；别的频道说的
        不带走，共用记录更早的摘要（里面有别的频道的事）也不带走。还在组里的频道照样看得到它连通期间说过的。"""
        now = time.time() if now is None else now
        sync_id = self._syncs.get(channel_id)
        if sync_id is None:
            raise SyncError("这个频道没有连通。")
        rest = [c for c in self.sync_members(sync_id) if c != channel_id]
        leaving = [channel_id] + (rest if len(rest) <= 1 else [])
        with self.db:
            shared = self.db.execute(
                "SELECT next_short_id, reviewed_upto FROM conversations WHERE channel_id = ?", (sync_id,)
            ).fetchone()
            for cid in leaving:
                self._take_back(sync_id, cid, shared, now, keep=len(rest) > 1)
            marks = ",".join("?" for _ in leaving)
            self.db.execute(f"DELETE FROM chat_syncs WHERE channel_id IN ({marks})", leaving)
            if len(rest) <= 1:
                self.db.execute(
                    "DELETE FROM dm_memory_attached WHERE turn_id IN (SELECT id FROM turns WHERE channel_id = ?)", (sync_id,)
                )
                self.db.execute("DELETE FROM turns WHERE channel_id = ?", (sync_id,))
                self.db.execute("DELETE FROM msgmap WHERE channel_id = ?", (sync_id,))
                self.db.execute("DELETE FROM conversations WHERE channel_id = ?", (sync_id,))
                self.db.execute("DELETE FROM chat_sync_summary WHERE sync_id = ?", (sync_id,))
                self.db.execute("DELETE FROM chat_sync_kept WHERE sync_id = ?", (sync_id,))
                self.db.execute("DELETE FROM chat_unvetted WHERE channel_id = ?", (sync_id,))
                self.db.execute("DELETE FROM chat_sync_scrub WHERE sync_id = ?", (sync_id,))
        for cid in leaving:
            self._syncs.pop(cid, None)
        return sync_id, leaving, (rest if len(rest) > 1 else [])

    def _take_back(self, sync_id: int, channel_id: int, shared: Any, now: float, *, keep: bool = False) -> None:
        """断开时（调用方开着事务）：共用记录里这个频道这次连通期间的那部分（之前哪次连通拿回过的不再拿）复制回它自己的
        记录后面，编号照旧。在共用记录里已经整理过记忆的那些（整批整理完的，或者分开整理完的那几段），复制回来也记成整理过
        （别再整理一遍）；它自己那份连通之前还有没整理完的，整理进度不跳过去。
        它自己那份在连通期间被写过（旧版本跑过：不认连通组，照常往里写）的话不拿：编号会撞上，记录就乱了。组还在（keep）的话，
        那部分留在组里，记在 chat_sync_kept：照样在组里整理记忆（不然哪边都不整理）。"""
        mark = self.db.execute(
            "SELECT guild_id, joined_turn, joined_sid, joined_next FROM chat_syncs WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        own = self.db.execute(
            "SELECT next_short_id, reviewed_upto FROM conversations WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        own_next = int(own["next_short_id"]) if own is not None else 1
        if mark["joined_next"] and own_next > mark["joined_next"]:
            log.warning(
                "连通：频道 %s 连通期间它自己那份记录被写过（多半是换回旧版本跑过），断开时不把连通期间的聊天拿回去（免得编号撞上）",
                channel_id,
            )
            if keep:
                self.db.execute(
                    "INSERT OR REPLACE INTO chat_sync_kept (sync_id, channel_id, from_turn) VALUES (?, ?, ?)",
                    (sync_id, channel_id, mark["joined_turn"]),
                )
            return
        reviewed = shared["reviewed_upto"] if shared is not None else 0
        own_reviewed = int(own["reviewed_upto"] or 0) if own is not None else 0
        # 整理进度只能跳过整理过的：它自己那份后面还有没整理的（连通时没整理完），拿回来的就算整理过也只记在每一条上
        whole = (
            self.db.execute(
                "SELECT 1 FROM turns WHERE channel_id = ? AND id > ? AND reviewed = 0 LIMIT 1", (channel_id, own_reviewed)
            ).fetchone()
            is None
        )
        last_reviewed = 0
        rows = self.db.execute(
            "SELECT id, role, content, created_at, reviewed FROM turns WHERE channel_id = ? AND origin = ? AND id > ? ORDER BY id",
            (sync_id, channel_id, mark["joined_turn"]),
        ).fetchall()
        for r in rows:
            done = r["id"] <= reviewed or bool(r["reviewed"])
            cur = self.db.execute(
                "INSERT INTO turns (channel_id, role, content, created_at, origin, reviewed) VALUES (?, ?, ?, ?, NULL, ?)",
                (channel_id, r["role"], r["content"], r["created_at"], int(done)),
            )
            if not done:
                whole = False
            elif whole:
                last_reviewed = int(cur.lastrowid)
        self.db.execute(
            "INSERT OR IGNORE INTO msgmap (channel_id, short_id, message_id, author_id, author_name, origin) "
            "SELECT ?, short_id, message_id, author_id, author_name, NULL FROM msgmap "
            "WHERE channel_id = ? AND origin = ? AND short_id >= ?",
            (channel_id, sync_id, channel_id, mark["joined_sid"]),
        )
        nxt = max(own_next, shared["next_short_id"] if shared is not None else 1)
        self.db.execute(
            "INSERT INTO conversations (channel_id, guild_id, summary, next_short_id, updated_at, reviewed_upto, took_back) "
            "VALUES (?, ?, NULL, ?, ?, ?, 1) ON CONFLICT (channel_id) DO UPDATE SET "
            "next_short_id = MAX(conversations.next_short_id, excluded.next_short_id), updated_at = excluded.updated_at, "
            "reviewed_upto = MAX(conversations.reviewed_upto, excluded.reviewed_upto), took_back = 1",
            (channel_id, mark["guild_id"], nxt, now, last_reviewed),
        )
        self._trim_msgmap(channel_id, nxt, sparse=True)

    def raise_join_marks(self, channel_id: int, own_next: int) -> None:
        """连进来以后才把它缓冲区里连通之前的闲聊记进它自己那份（编号往后用了几个）：连进来时记下的编号跟着往后挪，共用记录
        的下一个编号也不能比它小。不然同一个编号会用两次，断开时也会被当成“连通期间被旧版本写过”、不拿回自己那部分。"""
        sync = self._syncs.get(channel_id)
        if sync is None:
            return
        with self.db:
            self.db.execute(
                "UPDATE chat_syncs SET joined_next = MAX(joined_next, ?), joined_sid = MAX(joined_sid, ?) WHERE channel_id = ?",
                (own_next, own_next, channel_id),
            )
            self.db.execute("UPDATE conversations SET next_short_id = MAX(next_short_id, ?) WHERE channel_id = ?", (own_next, sync))

    def sync_joined_turns(self, sync_id: int) -> dict[int, int]:
        """连通组里每个频道这次连进来时最新的那条记录：之前哪次连通时说的（断开时拿回去、在它自己那份里整理过了）在这之前。
        断开时没能拿回去的频道（chat_sync_kept）也在里面：那次连通期间说的留在组里，照样在组里整理。"""
        out = {
            int(r["channel_id"]): int(r["from_turn"])
            for r in self.db.execute("SELECT channel_id, from_turn FROM chat_sync_kept WHERE sync_id = ?", (sync_id,))
        }
        for r in self.db.execute("SELECT channel_id, joined_turn FROM chat_syncs WHERE sync_id = ?", (sync_id,)):
            out[int(r["channel_id"])] = int(r["joined_turn"])  # 又连回来了：按这次的算
        return out

    def stale_syncs(self) -> list[int]:
        """连通的频道里，它自己那份记录在连通期间被写过的（旧版本跑过）：启动时提醒一声。"""
        rows = self.db.execute(
            "SELECT s.channel_id FROM chat_syncs s JOIN conversations c ON c.channel_id = s.channel_id "
            "WHERE s.joined_next > 0 AND c.next_short_id > s.joined_next ORDER BY s.channel_id"
        )
        return [int(r["channel_id"]) for r in rows]

    def drop_origin(self, sync_id: int, channel_id: int, *, member: bool = True) -> None:
        """连通的频道清空记忆：共用记录里这个频道的那部分删掉，更早的摘要也删掉（里面有它的事，分不开）。以前在组里的频道
        （member 不是真的）：只在它说的压进过那份摘要时才删摘要（chat_sync_summary 里记着）。别的频道的不动。"""
        with self.db:
            in_summary = member or (
                self.db.execute(
                    "SELECT 1 FROM chat_sync_summary WHERE sync_id = ? AND channel_id = ?", (sync_id, channel_id)
                ).fetchone()
                is not None
            )
            self.db.execute(
                "DELETE FROM dm_memory_attached WHERE turn_id IN (SELECT id FROM turns WHERE channel_id = ? AND origin = ?)",
                (sync_id, channel_id),
            )
            self.db.execute("DELETE FROM turns WHERE channel_id = ? AND origin = ?", (sync_id, channel_id))
            self.db.execute("DELETE FROM msgmap WHERE channel_id = ? AND origin = ?", (sync_id, channel_id))
            if in_summary:
                self.db.execute("UPDATE conversations SET summary = NULL, updated_at = ? WHERE channel_id = ?", (time.time(), sync_id))
                self.db.execute("DELETE FROM chat_sync_summary WHERE sync_id = ?", (sync_id,))  # 摘要整个清掉了
            self.db.execute("DELETE FROM chat_sync_kept WHERE sync_id = ? AND channel_id = ?", (sync_id, channel_id))

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
