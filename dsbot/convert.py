"""把 discord.Message 转成 IncomingMessage。只用鸭子类型访问属性，方便不装 discord.py 也能测试。"""

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
STICKER_IMAGE_FORMATS = {1, 2, 4}  # png / apng / gif；lottie(3) 是动画数据，不是图片
URL_RE = re.compile(r"https?://\S+")
MAX_PREVIEW_IMAGES = 2  # 一条消息里最多看几张链接预览的图
PREVIEW_TEXT_CHARS = 300  # 链接预览的描述最多给模型看多少字

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


_EMOJI_NAME = re.compile(r"[A-Za-z0-9_]{2,32}")
_EMOJI_TAG = re.compile(r"<a?:(\w+):(\d+)>")


def usable_emojis(guild: Any) -> list[Any]:
    """bot 在这个服务器里能用的自定义表情：还能用（没因为加成等级掉了），而且不限身份组、或者 bot 有那个身份组。"""
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
    """这个服务器自己的、现在还能发的贴图。"""
    if guild is None:
        return []
    return [s for s in getattr(guild, "stickers", None) or [] if getattr(s, "available", True) and getattr(s, "name", None)]


def emoji_code(emoji: Any) -> str:
    """发消息时自定义表情的写法：<:名字:id>，动图是 <a:名字:id>。"""
    return f"<{'a' if getattr(emoji, 'animated', False) else ''}:{emoji.name}:{emoji.id}>"


def emoji_lookup(guild: Any) -> dict[str, str]:
    """表情名 -> 发出去的写法（同名的取第一个）。"""
    out: dict[str, str] = {}
    for e in usable_emojis(guild):
        out.setdefault(e.name, emoji_code(e))
    return out


def resolve_reaction(emoji: str, guild: Any) -> Any:
    """react 动作给的 emoji：服务器表情（:名字: / 名字 / <:名字:id>）换成表情对象；Unicode 表情原样返回。"""
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
    """帖子的 category_id 要通过所在频道取，所在频道没缓存时 discord.py 会抛异常。"""
    try:
        return getattr(channel, "category_id", None)
    except Exception:
        return None


def _fetcher(fetch_url: FetchUrl, url: str) -> Callable[[], Awaitable[bytes]]:
    async def fetch() -> bytes:
        return await fetch_url(url)

    return fetch


def _link_key(url: str) -> str:
    """同一个链接的不同写法当成一个：推文按推文 ID 比（x.com、twitter.com、带不带 ?s=20 都一样）。"""
    tid = tweet_id(url)
    return f"tweet:{tid}" if tid else url


def _clean(value: Any) -> str | None:
    """预览里的一段文字：不是字符串或者是空的就当没有。"""
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
    """链接预览里能下载的那张图。只从 Discord 自己的图片代理 / CDN 下载：预览里的原始网址是发链接的人（或者别的 bot）
    写的，可能指向任何地方，bot 不去直接连。"""
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
    parent_id = getattr(channel, "parent_id", None)  # 只有帖子（Thread）有 parent_id

    text = clean_text(clean_custom_emoji(message.clean_content or ""))
    own_text = text  # 转发的人自己写的部分（别名只在这里找：转发别人的话不算在叫它）
    ref = message.reference
    forwarded = ref is not None and getattr(getattr(ref, "type", None), "value", 0) == FORWARD
    forwarded_text = ""
    if forwarded:
        snapshots = getattr(message, "message_snapshots", None) or []
        inner = clean_text(clean_custom_emoji(getattr(snapshots[0], "content", "") or "")) if snapshots else ""
        forwarded_text = inner
        text = f"[转发] {inner}".strip() if not text else f"{text}\n[转发] {inner}".strip()

    mentions_bot = any(user.id == me_id for user in message.mentions)
    if not mentions_bot:
        # 有人会 @ 到 bot 自带的那个身份组
        mentions_bot = any(
            getattr(role, "tags", None) is not None and role.tags.bot_id == me_id for role in message.role_mentions
        )
    if not mentions_bot and aliases:
        mentions_bot = alias_called(own_text, aliases)

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

    # 链接预览（embed）：GIF 选单发的 GIF、贴的图片链接，正文里只有一个链接，图在预览里；
    # 网页、推文、视频的预览：标题、作者、开头一段描述给模型看，预览图走看图（每个链接一张）
    embeds = list(getattr(message, "embeds", None) or [])
    raw_text = message.clean_content or ""
    in_text = {_link_key(u) for u in find_urls(f"{raw_text} {forwarded_text}")}  # 正文里的链接（预览是给它们生成的）
    notes: list[str] = []
    previews: dict[str, tuple[str | None, str | None]] = {}  # 链接 -> (标题, 网站)
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
                    text = text.replace(link, "").strip()  # 有了 [GIF]/[图片] 标记，就不用再给模型看一长串链接
            continue
        title = _clean(getattr(embed, "title", None)) or _clean(getattr(getattr(embed, "author", None), "name", None))
        site = _clean(getattr(getattr(embed, "provider", None), "name", None)) or _clean(
            getattr(getattr(embed, "footer", None), "text", None)
        )
        description = _clean(getattr(embed, "description", None))
        key = _link_key(link) if link else (title or description or "")
        if key and key not in noted and (title or description):
            noted.add(key)  # 一条推文有好几张图时，Discord 会给同一个链接发好几个预览：文字只记一次
            # 别的 bot 发的卡片也是 rich 类型；链接在正文里的才是链接预览（推文的预览就是 rich）
            label = "卡片" if etype == "rich" and key not in in_text else "链接预览"
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

    mentioned = [
        (user.id, clean_text(getattr(user, "display_name", None) or user.name))
        for user in message.mentions
        if not getattr(user, "bot", False) and user.id != me_id
    ]
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
        mentioned=mentioned,
    )
