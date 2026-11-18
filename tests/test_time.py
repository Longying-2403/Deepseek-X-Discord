"""2.9.1 它对时间的概念：每段开头的时间带年份、星期（不写年份，模型会照它训练时的年份猜，把今年的日期当成很久以后的）；
规则里写明最后一段开头的时间就是现在；以前存下的记录（没有年份的写法）照样认。"""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from helpers import make_engine, msg
from test_engine import REPLY

from dsbot.prompts import SYSTEM_RULES
from dsbot.render import _stamp, history_lines, now_text

TZ = "Asia/Taipei"


def at(*when):
    return datetime(*when, tzinfo=ZoneInfo(TZ)).timestamp()


def heard(received_at):
    m = msg(1, "早")
    m.received_at = received_at
    return [(1, m)]


class Stamp(unittest.TestCase):
    def test_year_and_weekday(self):
        now = at(2026, 11, 18, 13, 7)
        self.assertEqual(_stamp(heard(now - 10), now, TZ), "【2026-11-18 周三 13:07】")
        self.assertEqual(now_text(now, TZ), "2026-11-18 周三 13:07")
        days = [now_text(at(2026, 11, 16 + i, 9, 0), TZ).split()[1] for i in range(7)]
        self.assertEqual(days, ["周一", "周二", "周三", "周四", "周五", "周六", "周日"])

    def test_follows_the_configured_timezone(self):
        now = at(2026, 11, 18, 1, 30)  # 台北凌晨一点半 = UTC 前一天下午
        self.assertEqual(_stamp([], now, TZ), "【2026-11-18 周三 01:30】")
        self.assertEqual(_stamp([], now, "UTC"), "【2026-11-17 周二 17:30】")

    def test_range_when_the_batch_is_spread_out(self):
        now = at(2026, 11, 18, 13, 7)
        self.assertEqual(_stamp(heard(now - 15 * 60), now, TZ), "【2026-11-18 周三 12:52 ~ 13:07】")
        self.assertEqual(_stamp(heard(now - 299), now, TZ), "【2026-11-18 周三 13:07】")  # 5 分钟以内只写现在
        late = at(2027, 1, 1, 0, 5)  # 跨了一天（还跨了年）：两头都写全
        self.assertEqual(_stamp(heard(late - 600), late, TZ), "【2026-12-31 周四 23:55 ~ 2027-01-01 周五 00:05】")

    def test_where_goes_after_the_time(self):
        now = at(2026, 11, 18, 13, 7)
        self.assertEqual(_stamp([], now, TZ, "「主服」#闲聊"), "【2026-11-18 周三 13:07 · 「主服」#闲聊】")

    def test_old_records_are_still_read(self):
        turns = [
            ("user", "【10-07 20:00】\n#1 阿明：以前的写法"),
            ("user", "【2026-11-18 周三 13:07 · 「主服」#闲聊】\n#2 阿明：现在的写法"),
            ("user", "【2026-11-18 周三 12:52 ~ 13:07】\n#3 阿明：隔得久的"),
        ]
        self.assertEqual(
            history_lines(turns, "小D"),
            ["【10-07 20:00】", "阿明：以前的写法", "【2026-11-18 周三 13:07 · 「主服」#闲聊】", "阿明：现在的写法",
             "【2026-11-18 周三 12:52 ~ 13:07】", "阿明：隔得久的"],
        )


class WhatTheModelSees(unittest.IsolatedAsyncioTestCase):
    async def test_every_round_starts_with_the_full_date(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "嗯"))
        await engine.handle_message(msg(501, "@小D 今天几号", mentions_bot=True))
        await engine.drain()
        user = llm.calls[0]["messages"][-1]["content"]
        self.assertEqual(user.split("\n")[0], f"【{now_text(clock(), engine.tz)}】")
        self.assertRegex(user, r"^【20\d\d-\d\d-\d\d 周[一二三四五六日] \d\d:\d\d】\n")

    def test_rules_say_the_last_stamp_is_now(self):
        self.assertIn("最后一段开头的时间就是现在", SYSTEM_RULES)
        self.assertIn("今年是哪年、今天几号、星期几、现在几点，都照它，别凭印象", SYSTEM_RULES)
        self.assertIn("问候", SYSTEM_RULES)


if __name__ == "__main__":
    unittest.main()
