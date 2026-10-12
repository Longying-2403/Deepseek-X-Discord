"""Test message conversion, with simple fake objects standing in for discord.Message (no need to install discord.py)."""

import unittest
from types import SimpleNamespace as NS

import helpers  # noqa: F401  (sets up sys.path)

from dsbot.convert import convert_message, parse_aliases

ME = 999


def fake_message(**kw):
    async def read():
        return b"img"

    base = dict(
        id=501,
        channel=NS(id=100),
        guild=NS(id=1),
        author=NS(id=11, display_name="Alex", bot=False),
        clean_content="hello",
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
        self.assertEqual((m.message_id, m.channel_id, m.guild_id, m.author_name, m.text), (501, 100, 1, "Alex", "hello"))
        self.assertFalse(m.mentions_bot or m.reply_to_bot or m.is_bot)
        self.assertIsNone(m.parent_id)

    def test_direct_mention_and_role_mention(self):
        self.assertTrue(convert(fake_message(mentions=[NS(id=ME)])).mentions_bot)
        role = NS(id=5, tags=NS(bot_id=ME))
        self.assertTrue(convert(fake_message(role_mentions=[role])).mentions_bot)
        other = NS(id=6, tags=None)
        self.assertFalse(convert(fake_message(role_mentions=[other])).mentions_bot)

    def test_alias(self):
        self.assertTrue(convert(fake_message(clean_content="dee what do you think"), aliases="DS, Dee").mentions_bot)
        self.assertFalse(convert(fake_message(clean_content="nice weather today"), aliases="DS,Dee").mentions_bot)
        # A non-ASCII alias matches anywhere, even with no space around it; a full-width comma separates aliases too
        self.assertTrue(convert(fake_message(clean_content="小d你说呢"), aliases="DS，小D").mentions_bot)

    def test_reply_to_bot(self):
        ref = NS(message_id=400, channel_id=100, resolved=NS(author=NS(id=ME)))
        m = convert(fake_message(reference=ref))
        self.assertEqual((m.reply_to_id, m.reply_to_bot), (400, True))
        # When the replied-to message isn't cached (or was deleted), fall back on the bot's own record of what it sent
        ref = NS(message_id=401, channel_id=100, resolved=None)
        self.assertTrue(convert(fake_message(reference=ref), ours={401}).reply_to_bot)
        self.assertFalse(convert(fake_message(reference=ref)).reply_to_bot)
        # A reference forwarded from another channel doesn't count as a reply
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
        m = convert(fake_message(attachments=atts, stickers=[NS(name="kitty")], clean_content="<:pepe:123456789012345678>"))
        self.assertEqual([i.filename for i in m.images], ["cat.PNG", "photo"])
        self.assertEqual(m.images[1].content_type, "image/jpeg")
        self.assertEqual([(f.filename, f.content_type, f.size) for f in m.files], [("report.pdf", "application/pdf", 30)])
        self.assertEqual(m.stickers, ["kitty"])
        self.assertEqual(m.text, ":pepe:")

    def test_bot_author(self):
        m = convert(fake_message(author=NS(id=12, display_name="Mee6", bot=True), mentions=[NS(id=ME)]))
        self.assertTrue(m.is_bot)
        self.assertFalse(m.addressed)  # being called by another bot doesn't count



class ConvertRegressions(unittest.TestCase):
    def test_ascii_alias_needs_whole_word(self):
        for text in ("these words", "the kids need sleep", "cool cards", "dsl is hard"):
            self.assertFalse(convert(fake_message(clean_content=text), aliases="DS").mentions_bot, text)
        for text in (
            "DS what do you think",
            "问问ds",  # CJK text right next to the alias still counts
            "hey ds!",
            "@DS",
        ):
            self.assertTrue(convert(fake_message(clean_content=text), aliases="DS").mentions_bot, text)

    def test_forward_is_not_a_reply(self):
        ref = NS(message_id=400, channel_id=100, resolved=NS(author=NS(id=ME)), type=NS(value=1))
        m = convert(fake_message(reference=ref, clean_content="", message_snapshots=[NS(content="the original is here")]))
        self.assertIsNone(m.reply_to_id)
        self.assertFalse(m.reply_to_bot)
        self.assertEqual(m.text, "[forwarded] the original is here")

    def test_reply_type_still_reply(self):
        ref = NS(message_id=400, channel_id=100, resolved=NS(author=NS(id=ME)), type=NS(value=0))
        self.assertTrue(convert(fake_message(reference=ref)).reply_to_bot)



def embed(etype, *, url=None, thumb=None, proxy=None, image=None, title=None, provider=None):
    return NS(
        type=etype,
        url=url,
        title=title,
        provider=NS(name=provider),
        thumbnail=NS(url=thumb, proxy_url=proxy),
        image=NS(url=image, proxy_url=None),
    )


class Embeds(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fetched = []

        async def fetch_url(url):
            self.fetched.append(url)
            return b"img"

        self.fetch_url = fetch_url

    def conv(self, message):
        return convert_message(message, me_id=ME, aliases=[], is_ours=lambda mid: False, fetch_url=self.fetch_url)

    async def test_gif_picker_tenor(self):
        link = "https://tenor.com/view/cat-tongue-123"
        m = self.conv(fake_message(
            clean_content=link,
            embeds=[embed("gifv", url=link, thumb="https://media.tenor.com/x.png", proxy="https://images-ext-1.discordapp.net/x.png")],
        ))
        self.assertEqual([(i.kind, i.size) for i in m.images], [("gif", 0)])
        self.assertEqual(m.text, "")  # the link was replaced by the [GIF] marker
        self.assertFalse(m.awaiting_embeds)
        self.assertEqual(await m.images[0].fetch(), b"img")
        self.assertEqual(self.fetched, ["https://images-ext-1.discordapp.net/x.png"])  # goes through Discord's image proxy

    async def test_image_links(self):
        m = self.conv(fake_message(
            clean_content="look https://x.com/a.png and https://x.com/b.gif",
            embeds=[
                embed("image", url="https://x.com/a.png", thumb="https://x.com/a.png"),
                embed("image", url="https://x.com/b.gif", thumb="https://x.com/b.gif"),
            ],
        ))
        self.assertEqual([i.kind for i in m.images], ["image", "gif"])
        self.assertEqual(m.text, "look  and")

    async def test_article_preview_becomes_text(self):
        m = self.conv(fake_message(
            clean_content="https://youtu.be/abc",
            embeds=[embed("video", url="https://youtu.be/abc", thumb="https://i.ytimg.com/t.jpg", title="cat compilation", provider="YouTube")],
        ))
        self.assertEqual(m.images, [])
        self.assertEqual(m.text, "https://youtu.be/abc [link preview: YouTube - cat compilation]")

    async def test_stickers(self):
        stickers = [
            NS(name="kitty", format=NS(value=1), url="https://media.discordapp.net/stickers/1.png"),
            NS(name="animated", format=NS(value=3), url="https://media.discordapp.net/stickers/2.json"),
        ]
        m = self.conv(fake_message(stickers=stickers))
        self.assertEqual(m.stickers, ["kitty", "animated"])
        self.assertEqual([(i.filename, i.kind) for i in m.images], [("kitty", "sticker")])

    async def test_awaiting_embeds_flag(self):
        self.assertTrue(self.conv(fake_message(clean_content="https://tenor.com/view/x")).awaiting_embeds)
        self.assertFalse(self.conv(fake_message(clean_content="no links here")).awaiting_embeds)

    async def test_without_fetcher_no_link_images(self):
        m = convert_message(
            fake_message(clean_content="x", embeds=[embed("gifv", url="x", proxy="https://p/x.png")]),
            me_id=ME, aliases=[], is_ours=lambda mid: False,
        )
        self.assertEqual(m.images, [])

    async def test_category(self):
        self.assertEqual(self.conv(fake_message(channel=NS(id=100, category_id=50))).category_id, 50)

        class Thread:  # discord.py's category_id raises when the parent channel isn't cached
            id = 300
            parent_id = 100

            @property
            def category_id(self):
                raise RuntimeError("Parent channel not found")

        m = self.conv(fake_message(channel=Thread()))
        self.assertEqual((m.parent_id, m.category_id), (100, None))



class MoreAttachments(unittest.TestCase):
    def test_other_attachments_become_files(self):
        atts = [NS(filename="old.bmp", content_type="image/bmp", size=10), NS(filename="weekly-report.docx", content_type=None, size=20)]
        m = convert(fake_message(attachments=atts))
        self.assertEqual(m.images, [])  # BMP isn't decoded in the main process; it's handled as a file (the bot asks for a PNG/JPG instead)
        self.assertEqual([f.filename for f in m.files], ["old.bmp", "weekly-report.docx"])
        self.assertIsNotNone(m.files[1].fetch)

    def test_lone_surrogates_are_cleaned(self):
        m = convert(fake_message(clean_content="bad\ud800text", author=NS(id=11, display_name="name\udfff", bot=False)))
        self.assertEqual((m.text, m.author_name), ("bad?text", "name?"))


if __name__ == "__main__":
    unittest.main()
