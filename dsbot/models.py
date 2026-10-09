"""引擎内部用的数据结构，和 discord.py 解耦，方便测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ImageAttachment:
    filename: str
    content_type: str | None
    size: int
    # 下载原图的协程函数（Discord 层传 attachment.read），测试里可以塞假的
    fetch: Callable[[], Awaitable[bytes]] | None = field(default=None, repr=False, compare=False)


@dataclass
class IncomingMessage:
    message_id: int
    channel_id: int
    guild_id: int
    author_id: int
    author_name: str
    text: str
    is_bot: bool = False
    parent_id: int | None = None  # 帖子（thread）所属的频道
    reply_to_id: int | None = None
    reply_to_bot: bool = False  # 回复的是 bot 自己的消息
    mentions_bot: bool = False  # @了 bot（含 @bot 的身份组、别名）
    images: list[ImageAttachment] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    stickers: list[str] = field(default_factory=list)
    received_at: float = 0.0
    trigger: bool = False  # 引擎设置：这条点名通过了限速，需要立刻回应

    @property
    def addressed(self) -> bool:
        """这条消息是不是在叫 bot。"""
        return not self.is_bot and (self.mentions_bot or self.reply_to_bot)


@dataclass
class Action:
    kind: str  # "reply" | "react"
    to: int | None = None  # 消息编号（#后面的数字）
    messages: list[str] = field(default_factory=list)
    emoji: str | None = None

    def to_json(self) -> dict[str, Any]:
        if self.kind == "react":
            d: dict[str, Any] = {"type": "react"}
            if self.to is not None:
                d["to"] = self.to
            d["emoji"] = self.emoji
            return d
        d = {"type": "reply"}
        if self.to is not None:
            d["to"] = self.to
        d["messages"] = list(self.messages)
        return d


@dataclass
class Usage:
    prompt_tokens: int = 0
    hit: int = 0
    miss: int = 0
    output: int = 0
    reasoning: int = 0


@dataclass
class Completion:
    content: str
    reasoning: str | None
    usage: Usage
    finish_reason: str | None = None


@dataclass
class ChannelInfo:
    guild_name: str
    channel_name: str
    bot_name: str
