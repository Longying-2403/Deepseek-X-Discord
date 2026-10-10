"""把 discord.Message 转成 IncomingMessage。只用鸭子类型访问属性，方便不装 discord.py 也能测试。"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from .models import FileAttachment, ImageAttachment, IncomingMessage
from .textutil import clean_custom_emoji, clean_text, one_line

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
FORWARD = 1  # MessageReferenceType.forward
STICKER_IMAGE_FORMATS = {1, 2, 4}  # png / apng / gif；lottie(3) 是动画数据，不是图片
URL_RE = re.compile(r"https?://\S+")

FetchUrl = Callable[[str], Awaitable[bytes]]


def parse_aliases(raw: Any) -> list[str]:
    return [a.strip().lower() for a in str(raw or "").replace("，", ",").split(",") if a.strip()]


def alias_called(text: str, aliases: list[str]) -> bool:
    """英文/数字别名要求整词出现（DS 不该匹配 words）；中文别名直接找子串。"""
    lowered = text.lower()
    for alias in aliases:
        if alias.isascii():
            if re.search(rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])", lowered):
                return True
        elif alias in lowered:
            return True
    return False


def safe_category_id(channel: Any) -> int | None:
    """帖子的 category_id 要通过所在频道取，所在频道没缓存时 discord.py 会抛异常。"""
    try:
        return getattr(channel, "category_id", None)
    except Exception:
        return None


def _fetcher(fetch_url: FetchUrl, url: str) -> Callable[[], Awaitable[bytes]]:
    async def fetch() -> bytes:
        return await fetch_url(url)

    return fetch


def _embed_media(embed: Any) -> str | None:
    """GIF / 图片链接预览里能直接下载的那张静态图（优先走 Discord 的图片代理）。"""
    for media in (getattr(embed, "thumbnail", None), getattr(embed, "image", None)):
        if media is None:
            continue
        url = getattr(media, "proxy_url", None) or getattr(media, "url", None)
        if url:
            return url
    return None


def convert_message(
    message: Any,
    *,
    me_id: int,
    aliases: list[str],
    is_ours: Callable[[int], bool],
    fetch_url: FetchUrl | None = None,
) -> IncomingMessage:
    channel = message.channel
    parent_id = getattr(channel, "parent_id", None)  # 只有帖子（Thread）有 parent_id

    text = clean_text(clean_custom_emoji(message.clean_content or ""))
    ref = message.reference
    forwarded = ref is not None and getattr(getattr(ref, "type", None), "value", 0) == FORWARD
    if forwarded:
        snapshots = getattr(message, "message_snapshots", None) or []
        inner = clean_text(clean_custom_emoji(getattr(snapshots[0], "content", "") or "")) if snapshots else ""
        text = f"[转发] {inner}".strip() if not text else f"{text}\n[转发] {inner}".strip()

    mentions_bot = any(user.id == me_id for user in message.mentions)
    if not mentions_bot:
        # 有人会 @ 到 bot 自带的那个身份组
        mentions_bot = any(
            getattr(role, "tags", None) is not None and role.tags.bot_id == me_id for role in message.role_mentions
        )
    if not mentions_bot and aliases:
        mentions_bot = alias_called(text, aliases)

    reply_to_id: int | None = None
    reply_to_bot = False
    if ref is not None and not forwarded and ref.message_id is not None and ref.channel_id == channel.id:
        reply_to_id = ref.message_id
        author = getattr(ref.resolved, "author", None)  # 被删掉的消息没有 author
        reply_to_bot = author.id == me_id if author is not None else is_ours(ref.message_id)

    images: list[ImageAttachment] = []
    files: list[FileAttachment] = []
    for att in message.attachments:
        ctype = (att.content_type or "").split(";")[0].strip().lower()
        if ctype in IMAGE_TYPES or att.filename.lower().endswith(IMAGE_EXTS):
            images.append(ImageAttachment(clean_text(att.filename), ctype or None, att.size, fetch=att.read))
        else:
            files.append(FileAttachment(clean_text(att.filename), ctype or None, att.size, fetch=att.read))

    # 链接预览（embed）：GIF 选单发的 GIF、贴的图片链接，正文里只有一个链接，图在预览里
    embeds = list(getattr(message, "embeds", None) or [])
    notes: list[str] = []
    for embed in embeds:
        etype = getattr(embed, "type", None)
        if etype in ("gifv", "image"):
            url = _embed_media(embed)
            if url and fetch_url is not None:
                kind = "gif" if etype == "gifv" or url.lower().split("?")[0].endswith(".gif") else "image"
                images.append(ImageAttachment(url.rsplit("/", 1)[-1][:60], None, 0, fetch=_fetcher(fetch_url, url), kind=kind))
                link = getattr(embed, "url", None)
                if link:
                    text = text.replace(link, "").strip()  # 有了 [GIF]/[图片] 标记，就不用再给模型看一长串链接
        else:
            title = getattr(embed, "title", None)
            if title:
                provider = getattr(getattr(embed, "provider", None), "name", None)
                label = "卡片" if etype == "rich" else "链接预览"
                notes.append(f"[{label}: {one_line((provider + ' - ' if provider else '') + title, 80)}]")
    if notes:
        text = f"{text} {' '.join(notes)}".strip()

    stickers: list[str] = []
    for sticker in message.stickers:
        stickers.append(clean_text(sticker.name))
        fmt = getattr(getattr(sticker, "format", None), "value", None)
        url = getattr(sticker, "url", None)
        if url and fmt in STICKER_IMAGE_FORMATS and fetch_url is not None:
            images.append(ImageAttachment(sticker.name, None, 0, fetch=_fetcher(fetch_url, url), kind="sticker"))

    return IncomingMessage(
        message_id=message.id,
        channel_id=channel.id,
        guild_id=message.guild.id,
        parent_id=parent_id,
        category_id=safe_category_id(channel),
        author_id=message.author.id,
        author_name=clean_text(message.author.display_name),
        text=text,
        is_bot=bool(message.author.bot),
        reply_to_id=reply_to_id,
        reply_to_bot=reply_to_bot,
        mentions_bot=mentions_bot,
        images=images,
        files=files,
        stickers=stickers,
        awaiting_embeds=not embeds and bool(URL_RE.search(message.clean_content or "")),
    )
