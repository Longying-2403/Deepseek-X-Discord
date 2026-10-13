"""The meme library.

- The shared library: the pictures in data/memes/, usable on every server. Just create this folder on the NAS yourself and put pictures in it;
  the file name is the meme's name (e.g. "facepalm-cat.png"). The bot only reads this folder and never writes to it.
- Each server's own library: data/server-memes/<server ID>/, where admins upload and remove memes with /ds meme; they're only used on that server.
  When names clash, the server's own one wins.

Memes are pictures meant to be posted in the chat anyway, so they aren't private: the folders and files are readable by accounts on the NAS
(handy for backing up and organizing them in File Station).
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
SERVER_MAX = 300  # max uploads per server (so the NAS's disk can't be filled up)
RESCAN_SECONDS = 60.0  # rescan this often even if the folder hasn't changed (when a large file finishes copying, the folder's modification time doesn't change)
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def clean_name(raw: str) -> str:
    """A meme's name (also its file name): drop characters that aren't allowed in file names, and dots and whitespace at either end; at most 40 characters."""
    name = " ".join(_BAD_CHARS.sub(" ", raw or "").split()).strip(" .")
    return name[:NAME_MAX].strip(" .")


def name_key(name: str) -> str:
    """Names are compared ignoring case and whitespace."""
    return "".join(name.casefold().split())


@dataclass(frozen=True)
class Meme:
    name: str
    path: Path
    shared: bool  # True = the shared library


class MemeLibrary:
    def __init__(self, shared_dir: str | Path, server_dir: str | Path, *, max_bytes: int = 8_000_000):
        self.shared_dir = Path(shared_dir)
        self.server_dir = Path(server_dir)
        self.max_bytes = max_bytes
        # folder -> (modification time, scan time, {comparison key: (name, file)})
        self._cache: dict[Path, tuple[int, float, dict[str, tuple[str, Path]]]] = {}
        self._lock = threading.Lock()  # uploads and removals run in worker threads: two uploads with the same name at once won't both be saved
        self._warned: set[str] = set()

    def _folder(self, guild_id: int) -> Path:
        return self.server_dir / str(int(guild_id))

    def _scan(self, folder: Path, *, fresh: bool = False) -> dict[str, tuple[str, Path]]:
        """The memes in a folder: {comparison key: (name, file)}. Rescans only when files in the folder were added, removed or renamed, or after a minute."""
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
                # the file name isn't UTF-8 (e.g. extracted from a zip made on a non-English Windows): the name is garbled, and putting it in the prompt would make the request fail
                if str(path) not in self._warned:
                    self._warned.add(str(path))
                    log.warning("Meme file name isn't UTF-8, skipping it (please rename it on the NAS): %r", path.name)
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
            merged[key] = Meme(name, path, False)  # when names clash, the server's own one wins
        return {meme.name: meme for meme in merged.values()}

    def names(self, guild_id: int) -> list[str]:
        """Sorted (the list goes into the system prompt, and if the order shifted the cache would miss)."""
        return sorted(self.entries(guild_id))

    def counts(self, guild_id: int) -> tuple[int, int]:
        """(this server's own, the shared library's)"""
        return len(self._scan(self._folder(guild_id))), len(self._scan(self.shared_dir))

    def find(self, guild_id: int, wanted: str) -> Meme | None:
        key = name_key(wanted or "")
        own = self._scan(self._folder(guild_id)).get(key)
        if own is not None:
            return Meme(own[0], own[1], False)
        shared = self._scan(self.shared_dir).get(key)
        return Meme(shared[0], shared[1], True) if shared is not None else None

    def load(self, meme: Meme) -> tuple[bytes, str] | None:
        """(image content, file name for the upload). None when the file was just deleted or has grown too large.

        The upload uses a plain ASCII file name (meme.png and the like): non-English file names show up garbled in some clients, and the picture is displayed inline anyway."""
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
                    os.chmod(path, 0o755)  # the bot runs with umask 077: loosen it here so the folder is visible on the NAS
                except OSError:
                    pass
        return folder

    def add(self, guild_id: int, name: str, data: bytes) -> Meme:
        """Save a meme for this server. Raises ValueError (with a reason for people to read) when the name is unusable, the format isn't supported, it's too big or the name is taken."""
        name = clean_name(name)
        if not name:
            raise ValueError("the name can't be empty or only symbols")
        mime = sniff_mime(data)
        if mime not in MIME_EXT:
            raise ValueError("only PNG, JPG, GIF and WebP images are supported")
        if len(data) > self.max_bytes:
            raise ValueError(f"the image is too big (the limit is {self.max_bytes // 1_000_000} MB)")
        with self._lock:
            folder = self._ensure_folder(guild_id)
            existing = self._scan(folder, fresh=True)  # no cache here: the ones just uploaded or being uploaded must count too
            if name_key(name) in existing:
                raise ValueError(f'this server already has a meme called "{name}"; remove it first or pick another name')
            if len(existing) >= SERVER_MAX:
                raise ValueError(f"this server already has {SERVER_MAX} memes; remove some first")
            path = folder / (name + MIME_EXT[mime])
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                raise ValueError(f'this server already has a meme called "{name}"; remove it first or pick another name') from None
            try:
                os.fchmod(fd, 0o644)  # umask is 077: loosen it for this file
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
        """Remove one of this server's memes. Returns removed / shared (it's in the shared library, so it has to be deleted on the NAS) / missing."""
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
