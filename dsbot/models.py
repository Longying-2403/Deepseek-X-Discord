"""引擎内部用的数据结构，和 discord.py 解耦，方便测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ImageAttachment:
    filename: str
    content_type: str | None
    size: int  # 0 = 不知道（链接预览、贴图）
    # 下载原图的协程函数（Discord 层传 attachment.read），测试里可以塞假的
    fetch: Callable[[], Awaitable[bytes]] | None = field(default=None, repr=False, compare=False)
    kind: str = "image"  # image（上传的图）/ gif（GIF 链接的预览帧）/ sticker（贴图）


@dataclass
class FileAttachment:
    """群友上传的非图片文件。"""

    filename: str
    content_type: str | None
    size: int
    fetch: Callable[[], Awaitable[bytes]] | None = field(default=None, repr=False, compare=False)


@dataclass
class FileText:
    """读文件的结果。error 不为空表示没读出来（error 是给模型看的原因）。"""

    kind: str = ""  # 给模型看的类型：文本 / PDF / Word 文档 / Excel 表格 / PPT
    text: str = ""
    detail: str = ""  # 比如“共 12 页，只读到第 8 页”
    error: str = ""
    images: list[str] = field(default_factory=list)  # 扫描版 PDF 的页面图（data URI）
    transient: bool = False  # 没读成是暂时的（下载失败、名额用完），之后可以再试
    truncated: bool = False  # 文件太长，只读了前面一部分

    @classmethod
    def from_dict(cls, data: Any) -> "FileText":
        if not isinstance(data, dict):
            return cls(error="解析失败")
        from .textutil import clean_text

        images = [x for x in data.get("images") or [] if isinstance(x, str) and x.startswith("data:image/")]
        return cls(
            kind=clean_text(str(data.get("kind") or "")),
            text=clean_text(str(data.get("text") or "")),
            detail=clean_text(str(data.get("detail") or "")),
            error=clean_text(str(data.get("error") or "")),
            images=images,
            transient=bool(data.get("transient")),
            truncated=bool(data.get("truncated")),
        )


@dataclass
class DocBlock:
    """要放进聊天记录的一个文件内容块。"""

    label: str  # 文件所在的消息，比如 "#12"；不知道编号时是 "之前"
    filename: str
    result: FileText


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
    category_id: int | None = None  # 所在类别（Discord 里的“XX区”）
    reply_to_id: int | None = None
    reply_to_bot: bool = False  # 回复的是 bot 自己的消息
    mentions_bot: bool = False  # @了 bot（含 @bot 的身份组、别名）
    images: list[ImageAttachment] = field(default_factory=list)
    files: list[FileAttachment] = field(default_factory=list)
    stickers: list[str] = field(default_factory=list)
    received_at: float = 0.0
    trigger: bool = False  # 引擎设置：这条点名通过了限速，需要立刻回应
    awaiting_embeds: bool = False  # 带链接但预览还没到（Discord 会稍后补上）

    @property
    def addressed(self) -> bool:
        """这条消息是不是在叫 bot。"""
        return not self.is_bot and (self.mentions_bot or self.reply_to_bot)


@dataclass
class Action:
    kind: str  # "reply" | "react" | "read" | "search" | "recall"
    to: int | None = None  # 消息编号（#后面的数字）
    messages: list[str] = field(default_factory=list)
    emoji: str | None = None
    query: str | None = None  # search：要搜什么；recall：想找的关键词（可以没有）
    channel: str | None = None  # recall：哪个频道

    def to_json(self) -> dict[str, Any]:
        if self.kind == "read":
            return {"type": "read", "to": self.to}
        if self.kind == "search":
            return {"type": "search", "query": self.query}
        if self.kind == "recall":
            d: dict[str, Any] = {"type": "recall", "channel": self.channel}
            if self.query:
                d["about"] = self.query
            return d
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


@dataclass
class ChannelPlace:
    """一个频道在服务器里的位置（跨频道记忆要用它查设置、判断是不是私密频道）。"""

    guild_id: int
    name: str
    parent_id: int | None = None  # 帖子所在的频道
    category_id: int | None = None
    public: bool = False  # 所有人（@everyone）都看得到；拿不准时为 False
    private_thread: bool = False  # 私密帖子（只有被邀请的人看得到）


@dataclass
class SearchResult:
    summary: str
    sources: list[tuple[str, str]] = field(default_factory=list)  # (标题, 网址)
    usage: Usage = field(default_factory=lambda: Usage())
    provider: str = ""
