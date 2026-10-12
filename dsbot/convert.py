"""Turn a discord.Message into an IncomingMessage. Attributes are only accessed duck-typed, so it can be tested without discord.py installed."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from .models import FileAttachment, ImageAttachment, IncomingMessage
from .textutil import clean_custom_emoji, clean_text, one_line

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
FORWARD = 1  # MessageReferenceType.forward
STICKER_IMAGE_FORMATS = {1, 2, 4}  # png / apng / gif; lottie (3) is animation data, not an image
URL_RE = re.compile(r"https?://\S+")

FetchUrl = Callable[[str], Awaitable[bytes]]


def parse_aliases(raw: Any) -> list[str]:
    # a full-width comma (typed by Chinese/Japanese keyboards) separates aliases too
    return [a.strip().lower() for a in str(raw or "").replace("，", ",").split(",") if a.strip()]


def alias_called(text: str, aliases: list[str]) -> bool:
    """Aliases made of English letters/digits must appear as a whole word (DS shouldn't match "words"); other aliases (e.g. Chinese ones) match anywhere."""
    lowered = text.lower()
    for alias in aliases:
        if alias.isascii():
            if re.search(rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])", lowered):
                return True
        elif alias in lowered:
            return True
    return False


def safe_category_id(channel: Any) -> int | None:
    """A thread's category_id comes from its parent channel, and discord.py raises if the parent isn't cached."""
    try:
        return getattr(channel, "category_id", None)
    except Exception:
        return None


def _fetcher(fetch_url: FetchUrl, url: str) -> Callable[[], Awaitable[bytes]]:
    async def fetch() -> bytes:
        return await fetch_url(url)

    return fetch


def _embed_media(embed: Any) -> str | None:
    """The still image in a GIF / image link preview that can be downloaded directly (preferring Discord's image proxy)."""
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
    parent_id = getattr(channel, "parent_id", None)  # only threads have parent_id

    text = clean_text(clean_custom_emoji(message.clean_content or ""))
    ref = message.reference
    forwarded = ref is not None and getattr(getattr(ref, "type", None), "value", 0) == FORWARD
    if forwarded:
        snapshots = getattr(message, "message_snapshots", None) or []
        inner = clean_text(clean_custom_emoji(getattr(snapshots[0], "content", "") or "")) if snapshots else ""
        text = f"[forwarded] {inner}".strip() if not text else f"{text}\n[forwarded] {inner}".strip()

    mentions_bot = any(user.id == me_id for user in message.mentions)
    if not mentions_bot:
        # people sometimes @ the role that comes with the bot
        mentions_bot = any(
            getattr(role, "tags", None) is not None and role.tags.bot_id == me_id for role in message.role_mentions
        )
    if not mentions_bot and aliases:
        mentions_bot = alias_called(text, aliases)

    reply_to_id: int | None = None
    reply_to_bot = False
    if ref is not None and not forwarded and ref.message_id is not None and ref.channel_id == channel.id:
        reply_to_id = ref.message_id
        author = getattr(ref.resolved, "author", None)  # deleted messages have no author
        reply_to_bot = author.id == me_id if author is not None else is_ours(ref.message_id)

    images: list[ImageAttachment] = []
    files: list[FileAttachment] = []
    for att in message.attachments:
        ctype = (att.content_type or "").split(";")[0].strip().lower()
        if ctype in IMAGE_TYPES or att.filename.lower().endswith(IMAGE_EXTS):
            images.append(ImageAttachment(clean_text(att.filename), ctype or None, att.size, fetch=att.read))
        else:
            files.append(FileAttachment(clean_text(att.filename), ctype or None, att.size, fetch=att.read))

    # link previews (embeds): GIFs from the GIF picker and pasted image links have only a link in the text; the image is in the preview
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
                    text = text.replace(link, "").strip()  # with the [GIF]/[image] marker in place, the model doesn't need the long link too
        else:
            title = getattr(embed, "title", None)
            if title:
                provider = getattr(getattr(embed, "provider", None), "name", None)
                label = "card" if etype == "rich" else "link preview"
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
