"""「加班」「月底結算」: the hours past eight on each day on duty, and what they pay at the user's own hourly wage."""
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from all_staff import monthly_stats as ms
from all_staff.bot import (CUTE, OVERTIME_COMMANDS, OVERTIME_UI, Store, help_card, money, overtime_card, parse_wage,
                           stats_card, wage_text)
from test_all_staff_bot import BotCase, flex_texts, postback_event, slot
from test_all_staff_stats import MonthAnalyzer, panel, roster

TODAY = date(2026, 10, 7)


def hours(count, start=4, duty="國際線"):
    """``count`` roster rows (one hour each) from ``start`` on."""
    return [slot(row, 2, duty) for row in range(start, start + count)]


def size(node) -> int:
    return len(json.dumps(node, ensure_ascii=False, separators=(",", ":")).encode())


class OvertimeArithmeticTests(unittest.TestCase):
    def day(self, hours, status=ms.DUTY):
        return ms.Day(date(2026, 10, 1), status, hours)

    def test_only_the_hours_past_eight_are_overtime(self):
        self.assertEqual(ms.OVERTIME_AFTER, 8)
        self.assertEqual([self.day(h).overtime for h in (8, 10, 7.5, 13, 0, 8.5)], [0, 2, 0, 5, 0, .5])

    def test_a_day_off_or_a_day_not_counted_has_none(self):
        for status in (ms.OFF, ms.UNKNOWN, ms.MISSING, ms.PENDING, ms.UNREADABLE):
            self.assertEqual(self.day(12, status).overtime, 0, status)

    def test_a_short_day_does_not_make_up_for_a_long_one(self):
        month = ms.Month(2026, 10, [ms.Day(date(2026, 10, n), ms.DUTY, h) for n, h in ((1, 10), (2, 6), (3, 12))])
        self.assertEqual(month.overtime, 6)     # 2 + 0 + 4, not 28 − 24
        self.assertEqual(month.hours, 28)

    def test_an_hour_shared_by_two_duties_counts_once(self):
        person = {"slots": hours(9) + [slot(5, 31, "A1站")]}
        count, places, _ = ms.person_day(person)
        self.assertEqual(count, 9)
        self.assertEqual(ms.Day(date(2026, 10, 1), ms.DUTY, count, places).overtime, 1)
        shared = {"slots": hours(8) + [slot(4, 31, "A1站"), slot(6, 31, "A1站")]}
        self.assertEqual(ms.Day(date(2026, 10, 1), ms.DUTY, ms.person_day(shared)[0]).overtime, 0)


class WageTests(unittest.TestCase):
    def test_a_wage_can_be_typed_many_ways(self):
        for typed, value in (("200", 200), ("200元", 200), ("200 元", 200), ("NT$200", 200), ("NT$ 250", 250),
                             ("ＮＴ＄２００", 200), ("$1,000", 1000), ("196.5", 196.5), ("200元/小時", 200),
                             ("-200", -200), ("0", 0)):
            self.assertEqual(parse_wage(typed), value, typed)
        for typed in ("", "很多", "2OO", "200美金", "二百"):
            self.assertIsNone(parse_wage(typed), typed)

    def test_pay_is_whole_dollars_with_thousands_separators(self):
        self.assertEqual([money(v) for v in (0, 1234, 1234.5, 588.5, 1_549_998.45, 999.4)],
                         ["$0", "$1,234", "$1,235", "$589", "$1,549,998", "$999"])
        self.assertEqual([wage_text(v) for v in (200, 196.5, 1000, 183.25)], ["$200", "$196.5", "$1,000", "$183.25"])

    def test_the_store_keeps_a_wage_apart_from_the_name(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "bot.sqlite3")
            self.assertIsNone(store.hourly_wage("U1"))
            store.set_staff_name("U1", "林小明")
            store.set_hourly_wage("U1", 196.5)
            self.assertEqual(store.hourly_wage("U1"), 196.5)
            store.set_staff_name("U1", "林大明")              # a new name keeps the wage
            self.assertEqual(store.hourly_wage("U1"), 196.5)
            store.set_hourly_wage("U1", None)
            self.assertIsNone(store.hourly_wage("U1"))
            store.db.close()

    def test_an_older_database_gets_the_column(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot.sqlite3"
            with sqlite3.connect(path) as old:
                old.execute("create table users(user_id text primary key, code text, name text, "
                            "notify integer default 0, updated_at real, staff_name text)")
                old.execute("insert into users(user_id, staff_name) values('U1', '林小明')")
            old.close()
            store = Store(path)
            self.assertIn("hourly_wage", {row[1] for row in store.db.execute("pragma table_info(users)")})
            self.assertIsNone(store.hourly_wage("U1"))
            store.set_hourly_wage("U1", 200)
            self.assertEqual((store.hourly_wage("U1"), store.user("U1")["staff_name"]), (200, "林小明"))
            store.db.close()


class OvertimeBotTests(BotCase):
    settings = {"notify": False}

    def setUp(self):
        super().setUp()
        self.analyzer = MonthAnalyzer()
        self.bot.analyzer = self.analyzer
        self.register("U1", "林小明")
        me = {"72": "林小明"}
        self.add("run-1", "2026-10-01", {"72": hours(10)}, me)                         # 10 → +2
        self.add("run-2", "2026-10-02", {"甲": hours(8, 8, "國際線備勤")}, {"甲": "林小明"})  # 8 → none
        self.add("run-4", "2026-10-04", {"9": hours(3)}, {"5": "林小明", "9": "陳小華"}, leave={"rest": ["5"]})
        self.add("run-5", "2026-10-05", {"26": hours(12)}, {"9": "陳小華"})           # no 林小明 on it
        self.add("run-6", "2026-10-06", {"72": hours(12) + [slot(5, 31, "A1站")]}, me)  # 12 (one shared) → +4
        self.add("run-7", "2026-10-07", {"72": hours(9)}, me)                         # today: 9 → +1
        self.add("run-8", "2026-10-08", {"72": hours(13)}, me)                        # tomorrow: not yet

    def add(self, run, day, people, names, *, leave=None):
        self.analyzer.results[run] = roster(run, day, people, names, leave)
        self.store.add_roster("Cteam", run, "U9", len(people), day)

    def ask_on(self, command, user="U1", day=TODAY):
        with patch("all_staff.bot.today", return_value=day):
            reply = self.ask(command, user)
        return reply, self.transport.raw[-1][-1]

    def tiles(self, card):
        texts = flex_texts(card["contents"])
        return [texts[texts.index(label) - 1] for label in ("上班天數", "總時數", "加班時數")]

    def test_the_settlement_adds_up_days_hours_and_overtime(self):
        reply, card = self.ask_on("加班")
        self.assertEqual(card["type"], "flex")
        self.assertEqual(self.tiles(card), ["4 天", "39 小時", "7 小時"])    # 10/1, 10/2, 10/6, 10/7
        self.assertIn("💰 本月結算", reply)
        self.assertIn("林小明 的 10 月", reply)
        self.assertIn("10/1（四）–10/7（三）｜算到今天，月底就完整了", reply)
        self.assertIn("到 10/7 加班 7 小時，辛苦了！", reply)
        self.assertEqual(card["altText"], "10 月結算｜林小明｜上班 4 天、39 小時、加班 7 小時｜還沒設定時薪")
        self.assertEqual(card["contents"]["size"], "giga")

    def test_every_word_for_it_shows_it(self):
        for command in OVERTIME_COMMANDS + ("本月 結算", "加班 費"):
            self.assertTrue(self.ask_on(command)[1]["altText"].startswith("10 月結算｜林小明"), command)
        self.assertIn("加班結算", OVERTIME_COMMANDS)          # what the 本月統計 button sends

    def test_other_months_cannot_be_seen(self):
        reply, card = self.ask_on("上月結算")
        self.assertIn("結算只看得到這個月的喔！以下是 10 月", reply)
        self.assertTrue(card["altText"].startswith("10 月結算"))
        self.assertNotIn("只看得到這個月", self.ask_on("月底結算")[0])

    def test_a_wage_is_kept_and_the_pay_worked_out(self):
        reply, card = self.ask_on("時薪 200")
        self.assertIn("✅ 時薪記好了：$200", reply)
        self.assertEqual(self.store.hourly_wage("U1"), 200)
        self.assertEqual(len(self.transport.raw[-1]), 2)
        self.assertIn("🧮 加班 7 小時 × 時薪 $200＝$1,400", reply)
        texts = flex_texts(card["contents"])
        self.assertEqual(texts[texts.index("加班費（試算）") - 1], "$1,400")
        self.assertEqual(card["altText"], "10 月結算｜林小明｜上班 4 天、39 小時、加班 7 小時｜加班費試算 $1,400（時薪 $200）")
        self.assertIn("$1,400", self.ask_on("月底結算")[0])            # kept for the next time

    def test_pay_rounds_to_whole_dollars_and_groups_thousands(self):
        self.assertIn("加班 7 小時 × 時薪 $196.5＝$1,376", self.ask_on("時薪 196.5")[0])     # 1,375.5
        self.assertIn("加班 7 小時 × 時薪 $1,000＝$7,000", self.ask_on("設定時薪 1000")[0])

    def test_wages_typed_in_other_ways_are_understood(self):
        for typed, value in (("設定時薪 210", 210), ("時薪220元", 220), ("時薪 NT$230", 230), ("時薪：196.5", 196.5),
                             ("時薪 ２４０", 240), ("設定 時薪 250", 250), ("時薪 $1,200", 1200)):
            self.ask_on(typed)
            self.assertEqual(self.store.hourly_wage("U1"), value, typed)
        self.assertEqual(self.store.user("U1")["staff_name"], "林小明")   # 「設定時薪」 is not a name

    def test_a_wrong_wage_is_refused_with_a_hint(self):
        self.ask_on("時薪 200")
        for typed, hint in (("時薪 0", "時薪要大於 0"), ("時薪 -200", "時薪要大於 0"), ("時薪 0.001", "時薪要大於 0"),
                            ("時薪 20000", "好像太多了"), ("時薪 10000.5", "好像太多了"), ("時薪 2OO", "時薪請只打數字"),
                            ("時薪 200美金", "時薪請只打數字")):
            reply, message = self.ask_on(typed)
            self.assertIn(hint, reply, typed)
            self.assertIn("時薪 200", reply)                     # how to type it
            self.assertEqual(message["type"], "text")
            action = message["quickReply"]["items"][0]["action"]
            self.assertEqual((action["inputOption"], action["fillInText"]), ("openKeyboard", "時薪 "))
            self.assertEqual(self.store.hourly_wage("U1"), 200, typed)   # the old one stays
        self.ask_on("時薪 10000")
        self.assertEqual(self.store.hourly_wage("U1"), 10000)

    def test_the_wage_alone_says_what_it_is_and_how_to_set_it(self):
        reply, message = self.ask_on("時薪")
        self.assertIn("還沒設定時薪", reply)
        self.assertIn("輸入「時薪 200」", reply)
        first, second = [item["action"] for item in message["quickReply"]["items"]]
        self.assertEqual((first["label"], first["inputOption"], first["fillInText"]),
                         ("✏️ 設定時薪", "openKeyboard", "時薪 "))
        self.assertEqual((second["label"], second["text"]), ("💰 加班結算", "加班結算"))
        self.ask_on("時薪 200")
        for typed in ("設定時薪", "時薪多少", "時薪？"):        # no number: not a wage to refuse, a question
            reply, message = self.ask_on(typed)
            self.assertIn("你的時薪：$200", reply, typed)
            self.assertEqual(message["quickReply"]["items"][0]["action"]["label"], "✏️ 改時薪")
        self.assertEqual(self.store.hourly_wage("U1"), 200)

    def test_the_wage_can_be_forgotten(self):
        self.ask_on("時薪 200")
        self.assertIn("時薪已經刪掉了", self.ask_on("時薪 取消")[0])
        self.assertIsNone(self.store.hourly_wage("U1"))
        self.assertTrue(self.ask_on("加班")[1]["altText"].endswith("還沒設定時薪"))

    def test_without_a_name_the_reply_offers_to_register_one(self):
        for command in ("加班", "月底結算", "時薪 200", "時薪"):
            reply, message = self.ask_on(command, user="U3")
            self.assertIn("設定 你的姓名", reply, command)
            action = message["quickReply"]["items"][0]["action"]
            self.assertEqual((action["label"], action["fillInText"]), ("設定姓名", "設定 "))
        self.assertIsNone(self.store.hourly_wage("U3"))
        self.bot.handle(postback_event("U3", "wage"))
        self.assertIn("設定 你的姓名", self.reply())

    def test_the_pay_tile_opens_the_keyboard_and_its_postback_says_how(self):
        _, card = self.ask_on("加班")
        tile = self.pay_tile(card)
        self.assertEqual(flex_texts(tile), ["✏️", "設定時薪", "點我，算出加班費"])
        self.assertEqual(tile["action"], {"type": "postback", "label": "設定時薪", "data": "wage",
                                          "inputOption": "openKeyboard", "fillInText": "時薪 "})
        with patch("all_staff.bot.today", return_value=TODAY):
            self.bot.handle(postback_event("U1", "wage"))
        self.assertIn("還沒設定時薪", self.reply())
        self.ask_on("時薪 200")
        tile = self.pay_tile(self.ask_on("加班")[1])
        self.assertEqual(flex_texts(tile), ["💰", "$1,400", "加班費（試算）"])
        self.assertEqual(tile["action"]["label"], "改時薪")
        self.assertEqual(tile["background"]["type"], "linearGradient")

    def pay_tile(self, card):
        tiles = [box for row in card["contents"]["body"]["contents"][:2] for box in row["contents"]]
        self.assertEqual(len(tiles), 4)
        return tiles[3]

    def test_every_date_of_the_month_is_listed(self):
        _, card = self.ask_on("加班")
        days = panel(card, "🗓️ 每天的時數")
        self.assertEqual(flex_texts(days[0]), ["🗓️ 每天的時數", "10 月 1–31 日"])
        self.assertEqual(flex_texts(days[1]), ["日期", "0", "8", "時數", "加班"])
        rows = days[3]["contents"]
        self.assertEqual(len(rows), 31)
        self.assertEqual([flex_texts(row) for row in rows[:10]], [
            ["1 四", "10", "+2"], ["2 五", "8"], ["3 六", "沒有班表"], ["4 日", "💤 輪休"],
            ["5 一", "❓ 有班表，沒找到你"], ["6 二", "12", "+4"], ["今天", "9", "+1"],
            ["8 四", "還沒到"], ["9 五"], ["10 六"]])
        self.assertEqual(flex_texts(rows[-1]), ["31 六"])
        self.assertEqual(rows[8]["color"], OVERTIME_UI["faint"])            # days to come are faint
        self.assertEqual(rows[6]["contents"][0]["color"], CUTE["today"])
        self.assertEqual([rows[n].get("margin") for n in (3, 4, 10, 11)], [None, "lg", None, "lg"])   # Mondays
        self.assertIn("8 小時內", flex_texts(days[4])[0])               # the legend names both colours
        self.assertIn("超過 8 小時＝加班", flex_texts(days[4])[0])

    def test_a_day_is_a_bar_of_eight_hours_and_the_rest_in_another_colour(self):
        rows = panel(self.ask_on("加班")[1], "🗓️ 每天的時數")[3]["contents"]
        first = rows[0]["contents"]     # 10 hours on a 12-hour scale: date 3, bar 9, hours 2, overtime 2 (× 12)
        self.assertEqual([(part["type"], part.get("flex"), part.get("backgroundColor")) for part in first],
                         [("text", 36, None), ("box", 72, OVERTIME_UI["regular"]), ("box", 18, OVERTIME_UI["over"]),
                          ("filler", 18, None), ("text", 24, None), ("text", 24, None)])
        self.assertEqual(first[2]["margin"], "2px")                         # a gap between the two parts
        second = rows[1]["contents"]    # eight hours: no amber part, nothing in the overtime column
        self.assertEqual([(part["type"], part.get("flex")) for part in second],
                         [("text", 36), ("box", 72), ("filler", 36), ("text", 24), ("filler", 24)])

    def test_a_longer_day_widens_the_scale(self):
        self.add("run-7", "2026-10-07", {"72": hours(14)}, {"72": "林小明"})
        days = panel(self.ask_on("加班")[1], "🗓️ 每天的時數")
        self.assertEqual([t["flex"] for t in days[1]["contents"][1]["contents"]], [8, 6])
        today = days[3]["contents"][6]["contents"]
        self.assertEqual([part.get("flex") for part in today], [42, 72, 54, 28, 28])   # full: no filler

    def test_on_the_last_day_it_is_the_whole_month(self):
        self.add("run-31", "2026-10-31", {"72": hours(11)}, {"72": "林小明"})
        reply, card = self.ask_on("月底結算", day=date(2026, 10, 31))
        self.assertIn("10/1（四）–10/31（六）｜整個月都算好了", reply)
        self.assertNotIn("還沒到", reply)
        self.assertEqual(self.tiles(card), ["6 天", "63 小時", "15 小時"])   # 10/8 (+5) and 10/31 (+3) count now
        self.assertEqual(flex_texts(panel(card, "🗓️ 每天的時數")[3]["contents"][-1]), ["今天", "11", "+3"])

    def test_the_notes_say_what_the_total_rests_on(self):
        reply, _ = self.ask_on("加班")
        self.assertIn("只是試算：沒有乘上 1.34／1.67 倍的加班費率", reply)
        self.assertIn("不是正式出勤紀錄", reply)
        self.assertIn("沒有班表：10/3。", reply)
        self.assertIn("「我是 代號 10/5」", reply)

    def test_the_buttons_set_the_wage_or_go_back_to_the_month(self):
        _, card = self.ask_on("加班")
        items = card["quickReply"]["items"]
        self.assertEqual([item["action"]["label"] for item in items], ["✏️ 設定時薪", "📊 本月統計", "📅 我的班表"])
        self.assertEqual(items[0]["action"]["fillInText"], "時薪 ")
        self.ask_on("時薪 200")
        labels = [item["action"]["label"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertEqual(labels[0], "✏️ 改時薪")
        self.assertTrue(all(len(label) <= 20 for label in labels))

    def test_the_monthly_statistics_lead_here(self):
        _, card = self.ask_on("本月統計")
        self.assertEqual(card["quickReply"]["items"][-1]["action"], {"type": "message", "label": "💰 加班結算",
                                                                     "text": "加班結算"})
        line = next(node for node in card["contents"]["body"]["contents"][0]["contents"] if node.get("action"))
        self.assertEqual(line["text"], "🌙 其中加班 7 小時，點這裡看加班費 👉")
        self.assertEqual(line["action"]["text"], "加班結算")

    def test_feedback_about_it_is_still_feedback(self):
        for typed in ("意見 加班費算錯了", "意見 時薪 200 要怎麼改"):
            reply, message = self.ask_on(typed)
            self.assertIn("收到了，謝謝！", reply, typed)
            self.assertEqual(message["type"], "text")
        self.assertEqual({item["body"] for item in self.store.recent_feedback()}, {"加班費算錯了", "時薪 200 要怎麼改"})
        self.assertIsNone(self.store.hourly_wage("U1"))


class OvertimeCardTests(unittest.TestCase):
    def test_a_full_month_with_long_notes_stays_well_under_what_line_accepts(self):
        days = [ms.Day(date(2026, 10, n), ms.DUTY, 13, {"國際線": 6.5, "A1站": 6.5}, unsure=1, partial=n < 20)
                for n in range(1, 32)]
        card = overtime_card("歐陽小明明", ms.Month(2026, 10, days), 9999.99)
        self.assertLess(size(card["contents"]), 25_000)                    # LINE: 30 KB a bubble
        self.assertLessEqual(len(card["altText"]), 400)
        self.assertIn("$1,549,998", flex_texts(card["contents"]))
        self.assertEqual(len(panel(card, "🗓️ 每天的時數")[3]["contents"]), 31)

    def test_february_has_its_own_length(self):
        card = overtime_card("林小明", ms.Month(2026, 2, [ms.Day(date(2026, 2, 1), ms.DUTY, 9)]), None)
        days = panel(card, "🗓️ 每天的時數")
        self.assertEqual(len(days[3]["contents"]), 28)
        self.assertEqual(flex_texts(days[0])[1], "2 月 1–28 日")

    def test_a_month_without_duties(self):
        month = ms.Month(2026, 10, [ms.Day(date(2026, 10, 1), ms.MISSING), ms.Day(date(2026, 10, 2), ms.OFF)])
        card = overtime_card("林小明", month, 200)
        texts = flex_texts(card["contents"])
        self.assertIn("🌱 這段期間的班表上沒有找到你的勤務。", texts)
        self.assertIn("$0", texts)
        self.assertIn("💤 休假", texts)                                   # no kind of leave on the roster

    def test_no_overtime_is_cheered_and_the_statistics_say_nothing_of_it(self):
        month = ms.Month(2026, 10, [ms.Day(date(2026, 10, n), ms.DUTY, 8, {"國際線": 8.0}) for n in (1, 2)])
        self.assertIn("到 10/2 每天都沒有超過 8 小時 👍", flex_texts(overtime_card("林小明", month, None)["contents"]))
        self.assertFalse(any("加班" in text for text in flex_texts(stats_card("林小明", month)["contents"])))

    def test_the_help_explains_it(self):
        texts = flex_texts(help_card(advanced=True)["contents"])
        self.assertIn("時薪 200", texts)
        self.assertIn("加班", texts)
        self.assertTrue(any("加班結算" in text for text in flex_texts(help_card()["contents"])))



class WageInMySettingsTests(BotCase):
    settings = {"notify": False}

    def test_my_settings_shows_the_wage_once_set(self):
        self.register("U1", "林小明")
        self.assertNotIn("時薪", self.ask("我的設定"))
        self.ask("時薪 200")
        self.assertIn("時薪：$200", self.ask("我的設定"))


if __name__ == "__main__":
    unittest.main()
