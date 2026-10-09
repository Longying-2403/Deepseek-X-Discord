"""把 discord.Message 转成 IncomingMessage。只用鸭子类型访问属性，方便不装 discord.py 也能测试。"""

from __future__ import annotations

import re
from typing import Any, Callable

from .models import ImageAttachment, IncomingMessage
from .textutil import clean_custom_emoji

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
FORWARD = 1  # MessageReferenceType.forward


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


def convert_message(
    message: Any,
    *,
    me_id: int,
    aliases: list[str],
    is_ours: Callable[[int], bool],
) -> IncomingMessage:
    channel = message.channel
    parent_id = getattr(channel, "parent_id", None)  # 只有帖子（Thread）有 parent_id

    text = clean_custom_emoji(message.clean_content or "")
    ref = message.reference
    forwarded = ref is not None and getattr(getattr(ref, "type", None), "value", 0) == FORWARD
    if forwarded:
        snapshots = getattr(message, "message_snapshots", None) or []
        inner = clean_custom_emoji(getattr(snapshots[0], "content", "") or "") if snapshots else ""
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
    files: list[str] = []
    for att in message.attachments:
        ctype = (att.content_type or "").split(";")[0].strip().lower()
        if ctype in IMAGE_TYPES or att.filename.lower().endswith(IMAGE_EXTS):
            images.append(ImageAttachment(att.filename, ctype or None, att.size, fetch=att.read))
        else:
            files.append(att.filename)

    return IncomingMessage(
        message_id=message.id,
        channel_id=channel.id,
        guild_id=message.guild.id,
        parent_id=parent_id,
        author_id=message.author.id,
        author_name=message.author.display_name,
        text=text,
        is_bot=bool(message.author.bot),
        reply_to_id=reply_to_id,
        reply_to_bot=reply_to_bot,
        mentions_bot=mentions_bot,
        images=images,
        files=files,
        stickers=[sticker.name for sticker in message.stickers],
    )
