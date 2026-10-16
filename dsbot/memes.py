"""表情包库。

- 通用库：data/表情包/ 里的图，所有服务器都能用。在 NAS 上自己建这个文件夹、把图放进去就行，文件名就是表情包的名字
  （比如「熊猫头-无语.png」）。bot 只读不写这个文件夹。
- 每个服务器自己的库：data/服务器表情包/<服务器 ID>/，管理员用 /xai 表情包 上传、删除，只在那个服务器里用。
  同名时服务器自己的优先。

表情包本来就是要发到群里的图，不算隐私：文件夹和文件都让 NAS 上的账号能看（方便在 File Station 里备份、整理）。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .images import sniff_mime

log = logging.getLogger(__name__)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
NAME_MAX = 40
SERVER_MAX = 300  # 每个服务器最多上传多少张（防止把 NAS 的硬盘塞满）
RESCAN_SECONDS = 60.0  # 文件夹没变也隔这么久重新扫一次（正在复制的大文件，复制完时文件夹的修改时间不会变）
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def clean_name(raw: str) -> str:
    """表情包名（也是文件名）：去掉文件名里不能用的字符和首尾的点、空白，最长 40 个字。"""
    name = " ".join(_BAD_CHARS.sub(" ", raw or "").split()).strip(" .")
    return name[:NAME_MAX].strip(" .")


def name_key(name: str) -> str:
    """比较名字时忽略大小写和空白。"""
    return "".join(name.casefold().split())


@dataclass(frozen=True)
class Meme:
    name: str
    path: Path
    shared: bool  # True = 通用库


class MemeLibrary:
    def __init__(self, shared_dir: str | Path, server_dir: str | Path, *, max_bytes: int = 8_000_000):
        self.shared_dir = Path(shared_dir)
        self.server_dir = Path(server_dir)
        self.max_bytes = max_bytes
        # 文件夹 -> (修改时间, 扫描时间, {名字比较用的写法: (名字, 文件)})
        self._cache: dict[Path, tuple[int, float, dict[str, tuple[str, Path]]]] = {}
        self._lock = threading.Lock()  # 上传、删除在线程里做：同时上传同名的图也不会都存进去
        self._warned: set[str] = set()

    def _folder(self, guild_id: int) -> Path:
        return self.server_dir / str(int(guild_id))

    def _scan(self, folder: Path, *, fresh: bool = False) -> dict[str, tuple[str, Path]]:
        """文件夹里的表情包：{比较用的写法: (名字, 文件)}。文件夹有增删改名、或者隔了一分钟，才重新扫。"""
        try:
            stamp = folder.stat().st_mtime_ns
        except OSError:
            return {}
        now = time.monotonic()
        cached = self._cache.get(folder)
        if not fresh and cached is not None and cached[0] == stamp and now - cached[1] < RESCAN_SECONDS:
            return cached[2]
        found: dict[str, tuple[str, Path]] = {}
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name)
        except OSError:
            entries = []
        for path in entries:
            if path.name.startswith(".") or path.suffix.lower() not in IMAGE_EXTS:
                continue
            name = clean_name(path.stem)
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                # 文件名不是 UTF-8（比如在中文 Windows 上打包的 zip 解出来的）：名字是乱码，放进提示词还会让请求出错
                if str(path) not in self._warned:
                    self._warned.add(str(path))
                    log.warning("表情包文件名不是 UTF-8 编码，跳过（请在 NAS 上重新命名）：%r", path.name)
                continue
            key = name_key(name)
            if not name or key in found:
                continue
            try:
                if not path.is_file() or not 0 < path.stat().st_size <= self.max_bytes:
                    continue
            except OSError:
                continue
            found[key] = (name, path)
        self._cache[folder] = (stamp, now, found)
        return found

    def entries(self, guild_id: int) -> dict[str, Meme]:
        merged = {key: Meme(name, path, True) for key, (name, path) in self._scan(self.shared_dir).items()}
        for key, (name, path) in self._scan(self._folder(guild_id)).items():
            merged[key] = Meme(name, path, False)  # 同名时服务器自己的优先
        return {meme.name: meme for meme in merged.values()}

    def names(self, guild_id: int) -> list[str]:
        """排好序（名单放在 system prompt 里，顺序一乱缓存就失效）。"""
        return sorted(self.entries(guild_id))

    def counts(self, guild_id: int) -> tuple[int, int]:
        """(这个服务器自己的, 通用库的)"""
        return len(self._scan(self._folder(guild_id))), len(self._scan(self.shared_dir))

    def find(self, guild_id: int, wanted: str) -> Meme | None:
        key = name_key(wanted or "")
        own = self._scan(self._folder(guild_id)).get(key)
        if own is not None:
            return Meme(own[0], own[1], False)
        shared = self._scan(self.shared_dir).get(key)
        return Meme(shared[0], shared[1], True) if shared is not None else None

    def load(self, meme: Meme) -> tuple[bytes, str] | None:
        """(图片内容, 上传用的文件名)。文件刚被删掉、变得太大时返回 None。

        上传时的文件名只用英文（meme.png 之类）：中文文件名在有的客户端上会显示成乱码，反正图是直接显示的。"""
        try:
            data = meme.path.read_bytes()
        except OSError:
            return None
        if not 0 < len(data) <= self.max_bytes:
            return None
        ext = MIME_EXT.get(sniff_mime(data) or "", meme.path.suffix.lower() or ".png")
        return data, "meme" + ext

    def _ensure_folder(self, guild_id: int) -> Path:
        folder = self._folder(guild_id)
        for path in (self.server_dir, folder):
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(path, 0o755)  # bot 运行时 umask 是 077：这里单独放宽，NAS 上能看到
                except OSError:
                    pass
        return folder

    def add(self, guild_id: int, name: str, data: bytes) -> Meme:
        """存一张这个服务器的表情包。名字不合适、格式不支持、太大、重名时抛 ValueError（给人看的原因）。"""
        name = clean_name(name)
        if not name:
            raise ValueError("名字不能为空，也不能只有符号")
        mime = sniff_mime(data)
        if mime not in MIME_EXT:
            raise ValueError("只支持 PNG、JPG、GIF、WebP 图片")
        if len(data) > self.max_bytes:
            raise ValueError(f"图片太大了，最多 {self.max_bytes // 1_000_000} MB")
        with self._lock:
            folder = self._ensure_folder(guild_id)
            existing = self._scan(folder, fresh=True)  # 不用缓存：刚上传的、正在上传的都要算进去
            if name_key(name) in existing:
                raise ValueError(f"这个服务器已经有叫「{name}」的表情包了，先删掉或者换个名字")
            if len(existing) >= SERVER_MAX:
                raise ValueError(f"这个服务器的表情包已经有 {SERVER_MAX} 张了，先删掉一些")
            path = folder / (name + MIME_EXT[mime])
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                raise ValueError(f"这个服务器已经有叫「{name}」的表情包了，先删掉或者换个名字") from None
            try:
                os.fchmod(fd, 0o644)  # umask 是 077：单独放宽
            except OSError:
                pass
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            except OSError:
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
            self._cache.pop(folder, None)
            return Meme(name, path, False)

    def remove(self, guild_id: int, name: str) -> str:
        """删掉这个服务器的一张表情包。返回 removed / shared（是通用库的，要去 NAS 上删）/ missing。"""
        key = name_key(name or "")
        with self._lock:
            folder = self._folder(guild_id)
            own = self._scan(folder, fresh=True).get(key)
            if own is not None:
                try:
                    own[1].unlink()
                except FileNotFoundError:
                    pass
                self._cache.pop(folder, None)
                return "removed"
        if key in self._scan(self.shared_dir):
            return "shared"
        return "missing"
