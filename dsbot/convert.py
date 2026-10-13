"""Turn a discord.Message into an IncomingMessage. Attributes are only accessed duck-typed, so it can be tested without discord.py installed."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from .links import find_urls, openable, tweet_id
from .net import host_of
from .models import FileAttachment, ImageAttachment, IncomingMessage, LinkRef
from .textutil import clean_custom_emoji, clean_text, one_line

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
FORWARD = 1  # MessageReferenceType.forward
STICKER_IMAGE_FORMATS = {1, 2, 4}  # png / apng / gif; lottie (3) is animation data, not an image
URL_RE = re.compile(r"https?://\S+")
MAX_PREVIEW_IMAGES = 2  # max link-preview images looked at per message
PREVIEW_TEXT_CHARS = 300  # max characters of a link preview's description shown to the model

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


_EMOJI_NAME = re.compile(r"[A-Za-z0-9_]{2,32}")
_EMOJI_TAG = re.compile(r"<a?:(\w+):(\d+)>")


def usable_emojis(guild: Any) -> list[Any]:
    """The custom emoji the bot can use on this server: still available (not lost with a Boost level), and either not role-restricted or restricted to a role the bot has."""
    if guild is None:
        return []
    me = getattr(guild, "me", None)
    mine = {getattr(r, "id", None) for r in (getattr(me, "roles", None) or [])}
    out = []
    for e in getattr(guild, "emojis", None) or []:
        if not getattr(e, "available", True) or not getattr(e, "name", None) or getattr(e, "id", None) is None:
            continue
        roles = getattr(e, "roles", None) or []
        if roles and not any(getattr(r, "id", None) in mine for r in roles):
            continue
        out.append(e)
    return out


def usable_stickers(guild: Any) -> list[Any]:
    """This server's own stickers that can still be sent right now."""
    if guild is None:
        return []
    return [s for s in getattr(guild, "stickers", None) or [] if getattr(s, "available", True) and getattr(s, "name", None)]


def emoji_code(emoji: Any) -> str:
    """How a custom emoji is written in a message: <:name:id>, or <a:name:id> for an animated one."""
    return f"<{'a' if getattr(emoji, 'animated', False) else ''}:{emoji.name}:{emoji.id}>"


def emoji_lookup(guild: Any) -> dict[str, str]:
    """Emoji name -> how it's written when sent (the first one wins when names repeat)."""
    out: dict[str, str] = {}
    for e in usable_emojis(guild):
        out.setdefault(e.name, emoji_code(e))
    return out


def resolve_reaction(emoji: str, guild: Any) -> Any:
    """The emoji from a react action: a server emoji (:name: / name / <:name:id>) becomes the emoji object; Unicode emoji are returned as is."""
    text = emoji.strip()
    emojis = usable_emojis(guild)
    tag = _EMOJI_TAG.fullmatch(text)
    if tag:
        found = next((e for e in emojis if e.id == int(tag.group(2))), None)
        if found is not None:
            return found
        name = tag.group(1)
    else:
        name = text.strip(":")
    if _EMOJI_NAME.fullmatch(name):
        found = next((e for e in emojis if e.name == name), None) or next(
            (e for e in emojis if e.name.casefold() == name.casefold()), None
        )
        if found is not None:
            return found
    return text


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


def _link_key(url: str) -> str:
    """Different spellings of the same link count as one: posts on X are compared by post ID (x.com, twitter.com, with or without ?s=20 are all the same)."""
    tid = tweet_id(url)
    return f"tweet:{tid}" if tid else url


def _clean(value: Any) -> str | None:
    """A piece of text from a preview: anything that isn't a string, or is empty, counts as missing."""
    if not isinstance(value, str):
        return None
    value = clean_text(clean_custom_emoji(value)).strip()
    return value or None


_DISCORD_MEDIA = ("discordapp.net", "discordapp.com", "discord.com", "discord.media")


def _discord_media(url: Any) -> bool:
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    host = host_of(url)
    return any(host == h or host.endswith("." + h) for h in _DISCORD_MEDIA)


def _embed_media(embed: Any) -> str | None:
    """The image in a link preview that can be downloaded. Only downloads from Discord's own image proxy / CDN: the original URL in
    a preview was written by whoever posted the link (or by another bot) and could point anywhere, so the bot doesn't connect to it directly."""
    for media in (getattr(embed, "thumbnail", None), getattr(embed, "image", None)):
        if media is None:
            continue
        for url in (getattr(media, "proxy_url", None), getattr(media, "url", None)):
            if _discord_media(url):
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
    own_text = text  # what the forwarding person wrote themselves (aliases are only looked for here: forwarding someone else's words doesn't count as calling it)
    ref = message.reference
    forwarded = ref is not None and getattr(getattr(ref, "type", None), "value", 0) == FORWARD
    forwarded_text = ""
    if forwarded:
        snapshots = getattr(message, "message_snapshots", None) or []
        inner = clean_text(clean_custom_emoji(getattr(snapshots[0], "content", "") or "")) if snapshots else ""
        forwarded_text = inner
        text = f"[forwarded] {inner}".strip() if not text else f"{text}\n[forwarded] {inner}".strip()

    mentions_bot = any(user.id == me_id for user in message.mentions)
    if not mentions_bot:
        # people sometimes @ the role that comes with the bot
        mentions_bot = any(
            getattr(role, "tags", None) is not None and role.tags.bot_id == me_id for role in message.role_mentions
        )
    if not mentions_bot and aliases:
        mentions_bot = alias_called(own_text, aliases)

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

    # link previews (embeds): GIFs from the GIF picker and pasted image links have only a link in the text; the image is in the preview.
    # Previews of web pages, posts on X and videos: the title, author and first bit of the description go to the model, and the preview image goes through image viewing (one per link)
    embeds = list(getattr(message, "embeds", None) or [])
    raw_text = message.clean_content or ""
    in_text = {_link_key(u) for u in find_urls(f"{raw_text} {forwarded_text}")}  # links in the text (the previews are generated for them)
    notes: list[str] = []
    previews: dict[str, tuple[str | None, str | None]] = {}  # link -> (title, site)
    noted: set[str] = set()
    pictured: set[str] = set()
    for embed in embeds:
        etype = getattr(embed, "type", None)
        link = getattr(embed, "url", None) or None
        if etype in ("gifv", "image"):
            url = _embed_media(embed)
            if url and fetch_url is not None:
                kind = "gif" if etype == "gifv" or url.lower().split("?")[0].endswith(".gif") else "image"
                images.append(ImageAttachment(url.rsplit("/", 1)[-1][:60], None, 0, fetch=_fetcher(fetch_url, url), kind=kind))
                if link:
                    text = text.replace(link, "").strip()  # with the [GIF]/[image] marker in place, the model doesn't need the long link too
            continue
        title = _clean(getattr(embed, "title", None)) or _clean(getattr(getattr(embed, "author", None), "name", None))
        site = _clean(getattr(getattr(embed, "provider", None), "name", None)) or _clean(
            getattr(getattr(embed, "footer", None), "text", None)
        )
        description = _clean(getattr(embed, "description", None))
        key = _link_key(link) if link else (title or description or "")
        if key and key not in noted and (title or description):
            noted.add(key)  # when a post on X has several images, Discord sends several previews for the same link: the text is noted only once
            # cards sent by other bots are also of the rich type; it's a link preview only when the link is in the text (previews of posts on X are rich too)
            label = "card" if etype == "rich" and key not in in_text else "link preview"
            head = " - ".join(x for x in (site, title) if x)
            note = f"[{label}: {one_line(head, 80)}]" if head else f"[{label}]"
            if description:
                note += " " + one_line(description, PREVIEW_TEXT_CHARS)
            notes.append(note)
        if link and key not in previews:
            previews[key] = (title, site)
        url = _embed_media(embed)
        if url and fetch_url is not None and key not in pictured and len(pictured) < MAX_PREVIEW_IMAGES:
            pictured.add(key)
            images.append(ImageAttachment(url.rsplit("/", 1)[-1][:60], None, 0, fetch=_fetcher(fetch_url, url), kind="preview"))
    if notes:
        text = f"{text} {' '.join(notes)}".strip()

    links: list[LinkRef] = []
    for url in find_urls(f"{raw_text} {forwarded_text}"):
        if openable(url):
            title, site = previews.get(_link_key(url), (None, None))
            links.append(LinkRef(url, title=title, site=site, preview_image=_link_key(url) in pictured))

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
        links=links,
        awaiting_embeds=not embeds and bool(URL_RE.search(message.clean_content or "")),
    )
