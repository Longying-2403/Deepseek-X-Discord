"""运行时参数：config.toml 默认值 ← 全局覆盖 ← 服务器覆盖 ← 频道覆盖（帖子先看自己，再看所在频道）。"""

from __future__ import annotations

from typing import Any

from .config import SPECS, parse_value
from .store import Store

SCOPES = ("global", "guild", "channel")


class View:
    """某个频道当前生效的全部参数，用属性访问：view.refill_turns"""

    def __init__(self, settings: "Settings", guild_id: int | None, channel_id: int | None, parent_id: int | None):
        self._settings = settings
        self._ids = (guild_id, channel_id, parent_id)

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_") or key not in SPECS:
            raise AttributeError(key)
        guild_id, channel_id, parent_id = self._ids
        return self._settings.get(key, guild_id=guild_id, channel_id=channel_id, parent_id=parent_id)

    def source(self, key: str) -> str:
        guild_id, channel_id, parent_id = self._ids
        return self._settings.source(key, guild_id=guild_id, channel_id=channel_id, parent_id=parent_id)


class Settings:
    def __init__(self, defaults: dict[str, Any], store: Store):
        self.defaults = dict(defaults)
        self.store = store
        self._overrides: dict[tuple[str, int], dict[str, Any]] = {}
        for scope, scope_id, key, value in store.load_overrides():
            if key in SPECS:
                self._overrides.setdefault((scope, scope_id), {})[key] = value

    def _chain(self, guild_id, channel_id, parent_id):
        if channel_id is not None:
            yield "本频道", ("channel", channel_id)
        if parent_id is not None:
            yield "所在频道", ("channel", parent_id)
        if guild_id is not None:
            yield "本服务器", ("guild", guild_id)
        yield "全局", ("global", 0)

    def get(self, key: str, *, guild_id=None, channel_id=None, parent_id=None) -> Any:
        if key not in SPECS:
            raise KeyError(key)
        for _, scope_key in self._chain(guild_id, channel_id, parent_id):
            values = self._overrides.get(scope_key)
            if values is not None and key in values:
                return values[key]
        return self.defaults[key]

    def source(self, key: str, *, guild_id=None, channel_id=None, parent_id=None) -> str:
        for label, scope_key in self._chain(guild_id, channel_id, parent_id):
            values = self._overrides.get(scope_key)
            if values is not None and key in values:
                return label
        return "默认"

    def view(self, guild_id: int | None, channel_id: int | None = None, parent_id: int | None = None) -> View:
        return View(self, guild_id, channel_id, parent_id)

    def overrides(self, scope: str, scope_id: int) -> dict[str, Any]:
        return dict(self._overrides.get((scope, scope_id), {}))

    def set(self, scope: str, scope_id: int, key: str, raw: Any) -> Any:
        """校验并保存，返回解析后的值。"""
        if scope not in SCOPES:
            raise ValueError(f"未知范围：{scope}")
        spec = SPECS[key]
        value = parse_value(spec, raw)
        self.store.set_override(scope, scope_id, key, value)
        self._overrides.setdefault((scope, scope_id), {})[key] = value
        return value

    def unset(self, scope: str, scope_id: int, key: str) -> None:
        self.store.delete_override(scope, scope_id, key)
        values = self._overrides.get((scope, scope_id))
        if values is not None:
            values.pop(key, None)
