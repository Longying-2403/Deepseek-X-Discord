"""用简单的假对象模拟 discord.Message，测试消息转换（不需要装 discord.py）。"""

import unittest
from types import SimpleNamespace as NS

import helpers  # noqa: F401  (设置 sys.path)

from dsbot.convert import convert_message, parse_aliases

ME = 999


def fake_message(**kw):
    async def read():
        return b"img"

    base = dict(
        id=501,
        channel=NS(id=100),
        guild=NS(id=1),
        author=NS(id=11, display_name="阿明", bot=False),
        clean_content="你好",
        mentions=[],
        role_mentions=[],
        reference=None,
        attachments=[],
        stickers=[],
    )
    base.update(kw)
    for att in base["attachments"]:
        att.read = read
    return NS(**base)


def convert(message, aliases="", ours=()):
    return convert_message(message, me_id=ME, aliases=parse_aliases(aliases), is_ours=lambda mid: mid in ours)


class Convert(unittest.TestCase):
    def test_plain(self):
        m = convert(fake_message())
        self.assertEqual((m.message_id, m.channel_id, m.guild_id, m.author_name, m.text), (501, 100, 1, "阿明", "你好"))
        self.assertFalse(m.mentions_bot or m.reply_to_bot or m.is_bot)
        self.assertIsNone(m.parent_id)

    def test_direct_mention_and_role_mention(self):
        self.assertTrue(convert(fake_message(mentions=[NS(id=ME)])).mentions_bot)
        role = NS(id=5, tags=NS(bot_id=ME))
        self.assertTrue(convert(fake_message(role_mentions=[role])).mentions_bot)
        other = NS(id=6, tags=None)
        self.assertFalse(convert(fake_message(role_mentions=[other])).mentions_bot)

    def test_alias(self):
        self.assertTrue(convert(fake_message(clean_content="小d你说呢"), aliases="DS，小D").mentions_bot)
        self.assertFalse(convert(fake_message(clean_content="今天天气"), aliases="DS,小D").mentions_bot)

    def test_reply_to_bot(self):
        ref = NS(message_id=400, channel_id=100, resolved=NS(author=NS(id=ME)))
        m = convert(fake_message(reference=ref))
        self.assertEqual((m.reply_to_id, m.reply_to_bot), (400, True))
        # 被回复的消息没缓存到（或已删除）时，靠 bot 自己记下的发送记录
        ref = NS(message_id=401, channel_id=100, resolved=None)
        self.assertTrue(convert(fake_message(reference=ref), ours={401}).reply_to_bot)
        self.assertFalse(convert(fake_message(reference=ref)).reply_to_bot)
        # 转发自别的频道的引用不算回复
        ref = NS(message_id=402, channel_id=777, resolved=None)
        self.assertIsNone(convert(fake_message(reference=ref)).reply_to_id)

    def test_thread_parent(self):
        m = convert(fake_message(channel=NS(id=300, parent_id=100)))
        self.assertEqual((m.channel_id, m.parent_id), (300, 100))

    def test_attachments_and_stickers(self):
        atts = [
            NS(filename="cat.PNG", content_type="image/png", size=10),
            NS(filename="photo", content_type="image/jpeg; charset=binary", size=20),
            NS(filename="report.pdf", content_type="application/pdf", size=30),
        ]
        m = convert(fake_message(attachments=atts, stickers=[NS(name="猫猫")], clean_content="<:pepe:123456789012345678>"))
        self.assertEqual([i.filename for i in m.images], ["cat.PNG", "photo"])
        self.assertEqual(m.images[1].content_type, "image/jpeg")
        self.assertEqual(m.files, ["report.pdf"])
        self.assertEqual(m.stickers, ["猫猫"])
        self.assertEqual(m.text, ":pepe:")

    def test_bot_author(self):
        m = convert(fake_message(author=NS(id=12, display_name="Mee6", bot=True), mentions=[NS(id=ME)]))
        self.assertTrue(m.is_bot)
        self.assertFalse(m.addressed)  # 别的 bot 叫它不算数



class ConvertRegressions(unittest.TestCase):
    def test_ascii_alias_needs_whole_word(self):
        for text in ("these words", "the kids need sleep", "cool cards", "dsl 很难"):
            self.assertFalse(convert(fake_message(clean_content=text), aliases="DS").mentions_bot, text)
        for text in ("DS 你说呢", "问问ds", "hey ds!", "@DS"):
            self.assertTrue(convert(fake_message(clean_content=text), aliases="DS").mentions_bot, text)

    def test_forward_is_not_a_reply(self):
        ref = NS(message_id=400, channel_id=100, resolved=NS(author=NS(id=ME)), type=NS(value=1))
        m = convert(fake_message(reference=ref, clean_content="", message_snapshots=[NS(content="原文在这")]))
        self.assertIsNone(m.reply_to_id)
        self.assertFalse(m.reply_to_bot)
        self.assertEqual(m.text, "[转发] 原文在这")

    def test_reply_type_still_reply(self):
        ref = NS(message_id=400, channel_id=100, resolved=NS(author=NS(id=ME)), type=NS(value=0))
        self.assertTrue(convert(fake_message(reference=ref)).reply_to_bot)


if __name__ == "__main__":
    unittest.main()
