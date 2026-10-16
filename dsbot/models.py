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
    """要放进聊天记录的一个内容块：文件，或者打开的链接。"""

    label: str  # 文件所在的消息，比如 "#12"；不知道编号时是 "之前"
    filename: str  # 文件名；链接是标题
    result: FileText
    source: str = "文件"  # 文件 / 链接


@dataclass
class LinkRef:
    """消息里的一个链接。title / site 来自 Discord 生成的预览（可能没有）。"""

    url: str
    title: str | None = None
    site: str | None = None
    preview_image: bool = False  # 预览里已经有图（会走看图），打开时就不用再附图了


@dataclass
class LinkResult:
    """打开一个链接的结果。result.error 不为空表示没打开。"""

    title: str
    result: FileText


@dataclass
class Picture:
    """上网找到的一张候选图。"""

    data: bytes = field(repr=False)
    mime: str  # image/png / image/jpeg / image/gif / image/webp
    uri: str = field(repr=False)  # 给模型看的缩小版（data URI）
    description: str = ""
    host: str = ""  # 图片所在的网站

    @property
    def filename(self) -> str:
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}.get(self.mime, "png")
        return f"image.{ext}"


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
    links: list[LinkRef] = field(default_factory=list)  # 能打开的网页链接（图片、GIF、Discord 自己的链接不算）
    received_at: float = 0.0
    trigger: bool = False  # 引擎设置：这条点名通过了限速，需要立刻回应
    awaiting_embeds: bool = False  # 带链接但预览还没到（Discord 会稍后补上）
    from_owner: bool = False  # 引擎设置：发言的是 OWNER_ID（xai-owner）本人
    webhook: bool = False  # webhook 发的（PluralKit 之类代真人发的消息也是）：别的 bot 刚接过话时，这种不算

    @property
    def addressed(self) -> bool:
        """这条消息是不是在叫 bot。"""
        return not self.is_bot and (self.mentions_bot or self.reply_to_bot)


@dataclass
class Action:
    # "reply" | "react" | "sticker" | "meme" | "pick" | "read" | "search" | "image" | "recall" | "persona"
    # | "card" | "memory" | "who"
    kind: str
    to: int | None = None  # 消息编号（#后面的数字）
    messages: list[str] = field(default_factory=list)
    emoji: str | None = None
    query: str | None = None  # search / image：要搜什么；recall：想找的关键词（可以没有）
    channel: str | None = None  # recall：哪个频道
    name: str | None = None  # sticker：贴图名；meme：表情包名；who：要查谁的人设卡
    text: str | None = None  # persona：改好之后的完整人设（"" = 恢复默认；None = 没写）；card：完整的新人设卡；memory：内容
    scope: str | None = None  # persona：None = 改这里现在用的那份；"server" = 全服务器；"channel" = 这个频道
    undo: bool = False  # persona：撤销上一次修改
    image: int | None = None  # pick：发找到的第几张图（从 1 开始）
    op: str | None = None  # memory：add / edit / delete
    index: int | None = None  # memory：改、删第几条（长期记忆的编号）

    def to_json(self) -> dict[str, Any]:
        if self.kind == "card":
            return {"type": "card", "to": self.to, "text": self.text or ""}
        if self.kind == "memory":
            if self.op == "add":
                return {"type": "memory", "add": self.text}
            if self.op == "edit":
                return {"type": "memory", "edit": self.index, "text": self.text}
            return {"type": "memory", "delete": self.index}
        if self.kind == "who":
            return {"type": "who", "name": self.name}
        if self.kind in ("sticker", "meme"):
            d: dict[str, Any] = {"type": self.kind}
            if self.to is not None:
                d["to"] = self.to
            d["name"] = self.name
            return d
        if self.kind == "pick":
            d = {"type": "pick"}
            if self.to is not None:
                d["to"] = self.to
            d["image"] = self.image
            return d
        if self.kind == "image":
            return {"type": "image", "query": self.query}
        if self.kind == "persona":
            d = {"type": "persona"}
            if self.to is not None:
                d["to"] = self.to
            if self.scope:
                d["scope"] = self.scope
            if self.undo:
                d["undo"] = True
            if self.text is not None:
                d["text"] = self.text
            return d
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
    output: int = 0  # 按输出价计费的 token（含推理）
    reasoning: int = 0
    cost_usd: float | None = None  # xAI 直接给出的这次的实际花费（cost_in_usd_ticks）；没有时按 [pricing] 估
    web_searches: int = 0  # 联网搜索时 xAI 那边实际搜了几次（每次另外收费）


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
    emojis: list[str] = field(default_factory=list)  # 这个服务器里 bot 能用的自定义表情名（排好序）
    stickers: list[str] = field(default_factory=list)  # 这个服务器里能发的贴图名（排好序）


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
