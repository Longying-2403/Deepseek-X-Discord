"""2.8.0 连通的频道，第三轮评审修掉的问题：频道连通期间发的文件、链接，它从组里断开以后（组还在）。

- 断开的频道拿回去的文件、链接是另外一份，读过没读过各记各的：组里读了，它自己那份里照样能读（内容不在它那份里）；
  断开也不让组里忘了已经读过的（不再读一遍）。
- 断开以后删掉了那条消息、编辑掉了文件和链接：内存里有这条消息的别的记录（它离开的那个组的）也跟着改，组里读不到了。"""

import unittest

from helpers import CHANNEL, GUILD
from test_chatsync import HALL, disconnect, say, world
from test_chatsync_lifecycle import users
from test_chatsync_privacy import record
from test_chatsync_r2_records import asking_to_read, files_read
from test_chatsync_records import three_linked
from test_engine_files import Reader, doc
from test_links_images import PAGE, FakeReader, link_msg

ASK = "看看主服发的那个"  # 叫它的这一轮话里有这句：它要看 #1（那条消息的文件和链接）


def links_opened(links):
    """打开过的链接，按先后。"""
    return [url for url, *_ in links.calls]


class LeaverUpload(unittest.IsolatedAsyncioTestCase):
    async def linked_upload(self):
        """「主服」#闲聊、「别的服」#大厅、「主服」#大厅 连成一组（断开一个，组还在）。阿明在 #闲聊 发了条带文件和链接的
        消息，过了一阵才有人叫它（不自动读）：那条补进了共用的记录，是 #1。"""
        reader, links = Reader(), FakeReader()
        engine, llm, transport, clock = world(asking_to_read(ASK, 1), link_reader=links)
        engine.file_reader = reader
        sync = three_linked(engine)
        await engine.handle_message(link_msg(601, f"报告和原文 {PAGE}", [PAGE], files=[doc("报告.docx")]))
        clock.advance(300)  # 传了一阵才有人叫它：不自动读
        await say(engine, clock, 602, "@小D 随便聊聊")
        self.assertEqual(engine.store.load_msgmap(sync)[1], 601)
        return engine, clock, sync, reader, links


class ReadMarks(LeaverUpload):
    async def test_a_read_in_the_group_after_leaving_does_not_count_as_read_in_the_leavers_record(self):
        engine, clock, sync, reader, links = await self.linked_upload()
        disconnect(engine, CHANNEL)  # #闲聊 断开：组里还看得到它连通期间发的
        await say(engine, clock, 603, f"@小D {ASK}", channel=HALL, author=21, name="小红")  # 在组里读了
        await say(engine, clock, 604, f"@小D {ASK}")  # 回到 #闲聊 自己那份记录：组里读到的内容不在这里
        self.assertEqual(files_read(reader), ["报告.docx", "报告.docx"])  # 不当成“前面已经给你看过了”：重新读
        self.assertEqual(links_opened(links), [PAGE, PAGE])
        own = "\n".join(users(engine, CHANNEL, GUILD))
        self.assertIn("报告.docx 的内容", own)  # 读进了它自己那份
        self.assertIn(f"{PAGE} 的正文", own)

    async def test_leaving_does_not_make_the_group_forget_what_it_already_read(self):
        engine, clock, sync, reader, links = await self.linked_upload()
        await say(engine, clock, 603, f"@小D {ASK}", channel=HALL, author=21, name="小红")  # 断开之前在组里读了
        disconnect(engine, CHANNEL)
        await say(engine, clock, 604, f"@小D 再{ASK}", channel=HALL, author=21, name="小红")
        self.assertEqual(files_read(reader), ["报告.docx"])  # 组的记录里还有：照旧“前面已经给你看过了”，不再读一遍
        self.assertEqual(links_opened(links), [PAGE])
        self.assertEqual(record(engine, sync).count("报告.docx 的内容"), 1)


class ChangedAfterLeaving(LeaverUpload):
    async def test_a_message_deleted_after_leaving_cannot_be_read_in_the_group(self):
        engine, clock, sync, reader, links = await self.linked_upload()
        disconnect(engine, CHANNEL)
        engine.handle_delete(GUILD, CHANNEL, [601])  # 断开以后阿明删了那条
        await engine.drain()
        await say(engine, clock, 603, f"@小D {ASK}", channel=HALL, author=21, name="小红")
        self.assertEqual(files_read(reader), [])  # 组里也读不到了
        self.assertEqual(links_opened(links), [])

    async def test_a_file_and_link_edited_out_after_leaving_cannot_be_read_in_the_group(self):
        engine, clock, sync, reader, links = await self.linked_upload()
        disconnect(engine, CHANNEL)
        engine.handle_edit(link_msg(601, "发错了", []), edited=True)  # 断开以后阿明把文件、链接都编辑掉了
        await engine.drain()
        await say(engine, clock, 603, f"@小D {ASK}", channel=HALL, author=21, name="小红")
        self.assertEqual(files_read(reader), [])
        self.assertEqual(links_opened(links), [])


if __name__ == "__main__":
    unittest.main()
