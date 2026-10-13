"""Data structures used inside the engine, decoupled from discord.py so they're easy to test."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ImageAttachment:
    filename: str
    content_type: str | None
    size: int  # 0 = unknown (link previews, stickers)
    # coroutine function that downloads the original (the Discord layer passes attachment.read); tests can plug in a fake
    fetch: Callable[[], Awaitable[bytes]] | None = field(default=None, repr=False, compare=False)
    kind: str = "image"  # image (uploaded image) / gif (preview frame of a GIF link) / sticker


@dataclass
class FileAttachment:
    """A non-image file someone uploaded."""

    filename: str
    content_type: str | None
    size: int
    fetch: Callable[[], Awaitable[bytes]] | None = field(default=None, repr=False, compare=False)


@dataclass
class FileText:
    """The result of reading a file. A non-empty error means it couldn't be read (error is the reason shown to the model)."""

    kind: str = ""  # type shown to the model: text file / PDF / Word document / Excel spreadsheet / PowerPoint
    text: str = ""
    detail: str = ""  # e.g. "12 pages, text only read up to page 8"
    error: str = ""
    images: list[str] = field(default_factory=list)  # page images of a scanned PDF (data URIs)
    transient: bool = False  # the failure is temporary (download failed, out of slots) and can be retried later
    truncated: bool = False  # the file was too long, so only the first part was read

    @classmethod
    def from_dict(cls, data: Any) -> "FileText":
        if not isinstance(data, dict):
            return cls(error="parsing failed")
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
    """One content block to put into the chat log: a file, or an opened link."""

    label: str  # the message the file is in, e.g. "#12"; "an earlier message" when its number isn't known
    filename: str  # the file name; for a link, its title
    result: FileText
    source: str = "file"  # "file" / "link"


@dataclass
class LinkRef:
    """A link in a message. title / site come from the preview Discord generates (there may be none)."""

    url: str
    title: str | None = None
    site: str | None = None
    preview_image: bool = False  # the preview already has an image (it goes through image viewing), so opening the link doesn't need to attach images again


@dataclass
class LinkResult:
    """The result of opening a link. A non-empty result.error means it wasn't opened."""

    title: str
    result: FileText


@dataclass
class Picture:
    """One candidate picture found online."""

    data: bytes = field(repr=False)
    mime: str  # image/png / image/jpeg / image/gif / image/webp
    uri: str = field(repr=False)  # the downsized version shown to the model (data URI)
    description: str = ""
    host: str = ""  # the site the picture is on

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
    parent_id: int | None = None  # the channel a thread belongs to
    category_id: int | None = None  # the category it's in (the collapsible groups in Discord's channel list)
    reply_to_id: int | None = None
    reply_to_bot: bool = False  # replying to one of the bot's own messages
    mentions_bot: bool = False  # @mentions the bot (including the bot's role, or an alias)
    images: list[ImageAttachment] = field(default_factory=list)
    files: list[FileAttachment] = field(default_factory=list)
    stickers: list[str] = field(default_factory=list)
    links: list[LinkRef] = field(default_factory=list)  # web links that can be opened (images, GIFs and Discord's own links don't count)
    received_at: float = 0.0
    trigger: bool = False  # set by the engine: this mention passed the rate limit and needs a response right away
    awaiting_embeds: bool = False  # has a link, but the preview hasn't arrived yet (Discord adds it a bit later)
    from_owner: bool = False  # set by the engine: the speaker is OWNER_ID (dsowner) in person

    @property
    def addressed(self) -> bool:
        """Whether this message is calling the bot."""
        return not self.is_bot and (self.mentions_bot or self.reply_to_bot)


@dataclass
class Action:
    # "reply" | "react" | "sticker" | "meme" | "pick" | "read" | "search" | "image" | "recall" | "persona"
    kind: str
    to: int | None = None  # message number (the number after #)
    messages: list[str] = field(default_factory=list)
    emoji: str | None = None
    query: str | None = None  # search / image: what to look for; recall: keywords to look for (optional)
    channel: str | None = None  # recall: which channel
    name: str | None = None  # sticker: the sticker's name; meme: the meme's name
    text: str | None = None  # persona: the complete persona after the change ("" = back to default; None = not given)
    scope: str | None = None  # persona: None = the one currently used here; "server" = whole server; "channel" = this channel
    undo: bool = False  # persona: undo the last change
    image: int | None = None  # pick: which of the found pictures to send (starting from 1)

    def to_json(self) -> dict[str, Any]:
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
    emojis: list[str] = field(default_factory=list)  # names of the custom emoji the bot can use on this server (sorted)
    stickers: list[str] = field(default_factory=list)  # names of the stickers it can send on this server (sorted)


@dataclass
class ChannelPlace:
    """Where a channel sits in its server (cross-channel memory uses it to look up settings and to check whether a channel is private)."""

    guild_id: int
    name: str
    parent_id: int | None = None  # the channel a thread is in
    category_id: int | None = None
    public: bool = False  # everyone (@everyone) can see it; False when unsure
    private_thread: bool = False  # private thread (only invited people can see it)


@dataclass
class SearchResult:
    summary: str
    sources: list[tuple[str, str]] = field(default_factory=list)  # (title, URL)
    usage: Usage = field(default_factory=lambda: Usage())
    provider: str = ""
