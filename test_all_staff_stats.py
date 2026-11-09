"""「本月統計」: one person's planned hours and places over a month of recognised rosters."""
import calendar
import time
import types
import unittest
from datetime import date
from unittest.mock import patch

from all_staff import monthly_stats as ms
from all_staff.bot import (CALENDAR_CELL, CALENDAR_START, CALENDAR_UI, CUTE, FLEX_BUBBLE_LIMIT, absence_kind,
                           calendar_card, earlier_month, flex_size, stats_card)
from all_staff.dataset import GRID_VERSION
from all_staff.lines import DUTY_VERSION
from all_staff.name_table import NAMES_VERSION
from test_all_staff_bot import (BotCase, FakeAnalyzer, date_chips, flex_nodes, flex_texts, postback_event, slot,
                                tappable)
from test_all_staff_roster_gate import check_line_limits

TODAY = date(2026, 10, 7)


def roster(run, day, people, names, leave=None):
    """A recognised roster: ``people`` is code → slots, ``names`` code → the name read off the name table."""
    data = {"run": {"id": run, "codes": len(people), "roster_date": day},
            "sheets": [{"cells": [], "warnings": []}],
            "people": [{"code": code, "slots": slots} for code, slots in people.items()],
            "names": {code: [name] for code, name in names.items()}}
    if leave is not None:
        data["leave_table"] = {"available": True, "rest": [], "leave": [], **leave}
    return data


class MonthAnalyzer(FakeAnalyzer):
    """Saved rosters by run id; a run that is not there cannot be opened."""

    def __init__(self):
        super().__init__()
        self.results = {}

    def result(self, run_id):
        if run_id not in self.results:
            raise OSError("result.json is gone")
        return self.results[run_id]

    def person_image(self, run_id, code):
        person = next((p for p in self.result(run_id)["people"] if p["code"] == code), None)
        return (f"{run_id}/people/{code}.jpg", f"{run_id}/people/{code}-preview.jpg") if person else None


class MonthArithmeticTests(unittest.TestCase):
    def test_commands_name_a_month(self):
        for command in ("本月統計", "月統計", "統計", "本月 統計", "時數統計"):
            self.assertEqual(ms.which_month(command, TODAY), (2026, 10), command)
        for command in ("上月統計", "上個月統計"):
            self.assertEqual(ms.which_month(command, TODAY), (2026, 9), command)
        self.assertEqual(ms.which_month("9月統計", TODAY), (2026, 9))
        self.assertEqual(ms.which_month("統計 9月", TODAY), (2026, 9))
        self.assertEqual(ms.which_month("9月份的時數", TODAY), (2026, 9))
        self.assertEqual(ms.which_month("2025年12月統計", TODAY), (2025, 12))

    def test_a_month_without_a_year_is_the_latest_one_that_has_begun(self):
        self.assertEqual(ms.which_month("10月統計", TODAY), (2026, 10))
        self.assertEqual(ms.which_month("11月統計", TODAY), (2025, 11))
        self.assertEqual(ms.which_month("上月統計", date(2026, 1, 3)), (2025, 12))

    def test_other_text_is_not_a_statistics_command(self):
        for command in ("今天班表", "13月統計", "0月統計", "9/30統計", "統計一下", "我的設定", ""):
            self.assertIsNone(ms.which_month(command, TODAY), command)

    def test_a_month_is_counted_up_to_today(self):
        self.assertEqual(ms.days_of(2026, 10, TODAY), [date(2026, 10, n) for n in range(1, 8)])
        self.assertEqual(len(ms.days_of(2026, 9, TODAY)), 30)
        self.assertEqual(ms.days_of(2024, 2, TODAY)[-1], date(2024, 2, 29))
        self.assertEqual(ms.days_of(2026, 11, TODAY), [])

    def test_an_hour_shared_by_two_duties_counts_once_and_is_split(self):
        person = {"slots": [slot(4, 2, "國際線"), slot(4, 31, "A1站"), slot(5, 2, "國際線"), slot(5, 3, "國際線")]}
        hours, places, unsure = ms.person_day(person)
        self.assertEqual(hours, 2)
        self.assertEqual(places, {"國際線": 1.5, "A1站": 0.5})
        self.assertEqual(sum(places.values()), hours)
        self.assertEqual(unsure, 0)

    def test_an_hour_with_a_weak_reading_is_counted_as_unsure_once(self):
        person = {"slots": [slot(4, 2, "國際線", .5), slot(4, 31, "A1站", .4), slot(5, 2, "國際線")]}
        self.assertEqual(ms.person_day(person)[2], 1)
        self.assertEqual(ms.person_day(person, threshold=.3)[2], 0)

    def test_places_are_ranked_by_hours_then_name(self):
        month = ms.Month(2026, 10, [ms.Day(date(2026, 10, 1), ms.DUTY, 3, {"乙": 1.0, "甲": 2.0}),
                                    ms.Day(date(2026, 10, 2), ms.DUTY, 2, {"乙": 1.0, "丙": 1.0}),
                                    ms.Day(date(2026, 10, 3), ms.MISSING)])
        self.assertEqual(month.places(), [("乙", 2.0), ("甲", 2.0), ("丙", 1.0)])
        self.assertEqual(month.hours, 5)
        self.assertEqual([d.day.day for d in month.of(ms.MISSING)], [3])

    def test_hours_are_written_without_a_needless_decimal(self):
        self.assertEqual([ms.hours_text(v) for v in (24, 6.5, 1 / 3, 23.999999, 0)], ["24", "6.5", "0.3", "24", "0"])
        self.assertEqual(ms.share(1, 3), 33)
        self.assertEqual(ms.share(1, 0), 0)

    def test_the_leave_table_names_the_kind_of_day_off(self):
        table = {"leave_table": {"available": True, "rest": ["5"], "leave": [{"code": "7", "kind": "補休"},
                                                                             {"code": "8", "kind": ""}]}}
        self.assertEqual([absence_kind(code, table) for code in ("5", "7", "8", "9")], ["輪休", "補休", "請假", ""])
        self.assertEqual(absence_kind("5", {"leave_table": {"available": False, "rest": ["5"]}}), "")
        self.assertEqual(absence_kind("5", {}), "")


class MonthCase(BotCase):
    """林小明's October so far (today is 10/7): on duty 10/1, 10/2, 10/6 (two photos) and 10/7; 輪休 on 10/4; not on
    the name table of 10/5; no roster on 10/3; tomorrow's (10/8) already up."""

    def setUp(self):
        super().setUp()
        self.analyzer = MonthAnalyzer()
        self.bot.analyzer = self.analyzer
        self.register("U1", "林小明")
        me = {"72": "林小明"}
        self.add("run-1", "2026-10-01", {"72": [slot(4, 2, "國際線"), slot(5, 2, "國際線"),
                                                slot(6, 17, "國內線一線", .5)]}, me)
        # the code changes every day; one hour is shared between two posts
        self.add("run-2", "2026-10-02", {"甲": [slot(row, 9, "國際線備勤") for row in range(8, 13)]
                                         + [slot(12, 31, "A1站")]}, {"甲": "林小明"})
        self.add("run-4", "2026-10-04", {"9": [slot(4, 2, "國際線")]}, {"5": "林小明", "9": "陳小華"},
                 leave={"rest": ["5"]})
        self.add("run-5", "2026-10-05", {"26": [slot(4, 2, "國際線"), slot(5, 2, "國際線")]}, {"9": "陳小華"})
        self.add("run-6a", "2026-10-06", {"72": [slot(row, 2, "國際線") for row in range(4, 14)]}, me, sent=100)
        self.add("run-6b", "2026-10-06", {"72": [slot(4, 2, "國際線"), slot(5, 2, "國際線")]}, me, sent=200)
        self.add("run-7", "2026-10-07", {"72": [slot(4, 3, "國際線"), slot(5, 3, "國際線")]}, me)
        self.add("run-8", "2026-10-08", {"72": [slot(row, 2, "國際線") for row in range(4, 14)]}, me)

    def add(self, run, day, people, names, *, chat="Cteam", leave=None, sent=None, by="U9"):
        self.analyzer.results[run] = roster(run, day, people, names, leave)
        self.store.add_roster(chat, run, by, len(people), day, sent)

    def stats(self, command="本月統計", user="U1"):
        with patch("all_staff.bot.today", return_value=TODAY):
            reply = self.ask(command, user)
        return reply, self.transport.raw[-1][-1]


class MonthlyStatsTests(MonthCase):
    def test_a_month_adds_up_hours_days_and_places(self):
        reply, card = self.stats()
        self.assertEqual(card["type"], "flex")
        self.assertIn("林小明 的 10 月", reply)
        self.assertIn("10/1（四）–10/7（三）", reply)
        texts = flex_texts(card["contents"])
        # 3 + 5 + 2 + 2: the 8th is tomorrow, the 6th counts once; then the days on duty and their average
        self.assertEqual([texts[texts.index(label) - 1] for label in ("排定時數", "上班天數", "平均一天")],
                         ["12 小時", "4 天", "3 小時"])
        self.assertIn("最常在國際線", reply)                    # the place with the most hours
        self.assertEqual(card["altText"], "10 月統計｜林小明｜排定 12 小時、上班 4 天｜最久：國際線 6 小時")

    def test_places_are_listed_longest_first_with_their_share(self):
        texts = flex_texts(self.stats()[1]["contents"])
        start = texts.index("長條越長，時數越多")
        self.assertEqual(texts[start + 1:start + 13],
                         ["國際線", "6 小時", "50%", "國際備勤", "4.5 小時", "38%",
                          "國內一線", "1 小時", "8%", "A1站", "0.5 小時", "4%"])

    def test_the_notes_say_which_days_the_total_does_not_cover(self):
        reply, _ = self.stats()
        self.assertIn("這 7 天裡，有 6 天的班表", reply)
        self.assertIn("沒有班表：10/3。", reply)
        self.assertIn("有班表但沒找到你：10/5。", reply)
        self.assertIn("「我是 代號 10/5」", reply)
        self.assertIn("班表上沒有你的勤務：10/4（輪休）。", reply)
        self.assertIn("其中 1 小時的辨識信心較低", reply)
        self.assertIn("不是正式出勤紀錄", reply)
        self.assertNotIn("10/8", reply)

    def test_claiming_a_code_puts_that_day_into_the_total(self):
        with patch("all_staff.bot.today", return_value=TODAY):
            self.assertIn("你是代號 26", self.ask("我是 26 10/5"))
        reply, _ = self.stats()
        self.assertIn("14 小時", reply)
        self.assertIn("5 天", reply)
        self.assertNotIn("沒找到你", reply)

    def test_a_private_upload_counts_for_its_sender_only(self):
        self.register("U2", "陳小華")   # a name belongs to one LINE account, so the sender is someone else
        self.add("private", "2026-10-03", {"9": [slot(4, 2, "國際線")]}, {"9": "陳小華"}, chat="U2", by="U2")
        self.assertIn("沒有班表：10/3。", self.stats()[0])
        mine, _ = self.stats(user="U2")
        self.assertIn("陳小華 的 10 月", mine)
        self.assertIn("排了 2 小時", mine)                    # 10/4 from the group, 10/3 from their own photo
        self.assertIn("這 7 天裡，有 7 天的班表", mine)

    def test_someone_else_gets_their_own_month(self):
        self.register("U2", "陳小華")
        reply, _ = self.stats(user="U2")
        self.assertIn("陳小華 的 10 月", reply)
        self.assertIn("排了 1 小時", reply)                    # only 10/4, as 9
        self.assertIn("1 天", reply)

    def test_only_this_month_can_be_seen(self):
        self.add("sep", "2026-09-30", {"72": [slot(4, 2, "國際線")]}, {"72": "林小明"})
        for command in ("上月統計", "9月統計", "統計 9月", "2025年12月統計"):
            reply, card = self.stats(command)
            self.assertIn("統計只看得到這個月的喔", reply, command)
            self.assertIn("林小明 的 10 月", reply)
            self.assertNotIn("9/30", reply)
            self.assertEqual(flex_texts(card["contents"])[0], "🐰 本月統計")
        for command in ("本月統計", "10月統計"):
            self.assertNotIn("只看得到這個月", self.stats(command)[0], command)

    def test_the_buttons_lead_to_rosters_not_to_other_months(self):
        _, card = self.stats()
        actions = [item["action"] for item in card["quickReply"]["items"]]
        self.assertEqual([(a["label"], a["type"]) for a in actions],
                         [("我的班表", "message"), ("查看工作", "message"), ("💰 加班結算", "message")])
        self.assertEqual(actions[-1]["text"], "加班結算")   # this month too: 加班結算 has no other

    def test_buttons_of_old_cards_show_this_month(self):
        with patch("all_staff.bot.today", return_value=TODAY):
            for data in ("stats|2026-09", "stats|2026-11"):
                self.bot.handle(postback_event("U1", data))
                self.assertIn("統計只看得到這個月的喔", self.reply(), data)
                self.assertIn("林小明 的 10 月", self.reply())
            self.bot.handle(postback_event("U1", "stats|2026-10"))
            self.assertNotIn("只看得到這個月", self.reply())
            count = len(self.transport.replies)
            for data in ("stats|2026-13", "stats|2026-00", "stats|abcd-ef", "stats|2026-10|x"):
                self.bot.handle(postback_event("U1", data))
            self.assertEqual(len(self.transport.replies), count)

    def test_without_a_name_the_reply_offers_to_register_one(self):
        reply, message = self.stats(user="U3")
        self.assertIn("設定 你的姓名", reply)
        action = message["quickReply"]["items"][0]["action"]
        self.assertEqual((action["label"], action["inputOption"], action["fillInText"]),
                         ("設定姓名", "openKeyboard", "設定 "))

    def test_my_own_roster_offers_the_month_and_someone_elses_does_not(self):
        with patch("all_staff.bot.today", return_value=TODAY):
            self.ask("今天")   # that day's duty card
            mine = [item["action"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
            self.ask("查 72")
            other = [item["action"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertEqual(mine[0]["label"], "查其他人")
        self.assertEqual((mine[-1]["label"], mine[-1]["text"]), ("本月統計", "本月統計"))
        self.assertNotIn("本月統計", [action["label"] for action in other])

    def test_a_month_without_any_roster(self):
        self.store.db.execute("delete from rosters")
        reply, _ = self.stats()
        self.assertIn("這段期間的班表上沒有找到你的勤務", reply)
        self.assertIn("這 7 天裡，有 0 天的班表", reply)

    def test_a_roster_that_cannot_be_opened_is_named_not_counted(self):
        del self.analyzer.results["run-7"]
        reply, _ = self.stats()
        self.assertIn("10 小時", reply)
        self.assertIn("班表資料讀不出來：10/7。", reply)

    def test_many_places_end_in_one_line_for_the_rest(self):
        duties = ["國際線", "國內線一線", "A1站", "內機", "內部管理", "全區帶勤", "中科物流", "登機管制門"]
        self.analyzer.results["run-7"] = roster(
            "run-7", "2026-10-07", {"72": [slot(4 + n, 2 + n, duty) for n, duty in enumerate(duties)]},
            {"72": "林小明"})
        texts = flex_texts(self.stats()[1]["contents"])
        self.assertTrue(any(t.startswith("其他 ") and t.endswith(" 個地點") for t in texts), texts)

    def test_the_card_stays_within_what_line_accepts(self):
        _, card = self.stats()
        self.assertLessEqual(len(card["altText"]), 400)
        self.assertLessEqual(len(card["quickReply"]["items"]), 13)
        self.assertEqual(card["contents"]["type"], "bubble")

    def test_the_calendar_has_a_cell_for_each_day(self):
        card = self.stats()[1]
        days = panel(card, "🗓️ 班表月曆")
        self.assertEqual(flex_texts(days[0]), ["🗓️ 班表月曆", "點有顏色的日期看當天班表"])
        self.assertEqual(flex_texts(days[1]), list("日一二三四五六"))   # Sunday first
        cells = calendar_cells(card)
        self.assertEqual((len(cells), cells[:4]), (35, [None] * 4))   # 10/1/2026 is a Thursday, 10/31 a Saturday
        dated = [cell for cell in cells if cell]
        self.assertEqual(len(dated), 31)
        self.assertEqual([flex_texts(cell) for cell in dated[:8]],
                         [["1", "3h"], ["2", "5h"], ["3", "　"], ["4", "💤"], ["5", "❓"], ["6", "2h"], ["7", "2h"],
                          ["8", "　"]])
        self.assertEqual([cell.get("backgroundColor") for cell in dated[:8]],
                         [CALENDAR_UI[kind] for kind in ("duty", "duty", "missing", "off", "uncounted", "duty", "duty")]
                         + [None])
        self.assertEqual(dated[2]["contents"][0]["color"], CALENDAR_UI["missing_ink"])   # no roster: grey
        self.assertEqual(dated[7]["contents"][0]["color"], CALENDAR_UI["future_ink"])    # still to come: faint
        # a tap for each date with a roster up to today: not 10/3 (none), nor tomorrow's 10/8
        self.assertEqual([box["action"]["data"] for box in tappable(days, "cal|")],
                         ["cal|2026-10-01", "cal|2026-10-02", "cal|2026-10-04", "cal|2026-10-05", "cal|2026-10-06",
                          "cal|2026-10-07"])
        self.assertEqual(dated[4]["action"]["displayText"], "10/5 的班表")
        self.assertEqual([cell for cell in dated if cell.get("borderWidth")], [dated[6]])   # today is ringed
        self.assertEqual((dated[6]["borderColor"], dated[6]["contents"][0]["weight"]), (CUTE["today"], "bold"))
        # the legend names every tint and mark on it, and nothing else
        self.assertEqual(flex_texts(days)[-5:], ["8h 排定時數", "💤 輪休", "❓ 沒找到你", "沒有班表", "今天"])

    def test_the_places_are_drawn_as_bars_against_the_longest(self):
        rows = panel(self.stats()[1], "🍰 各地點時數")[1:]
        self.assertEqual([row["contents"][1]["contents"][0]["width"] for row in rows], ["100%", "75%", "17%", "8%"])


def panel(card, title):
    """The contents of the card's panel headed ``title``: its heading row first."""
    for node in card["contents"]["body"]["contents"]:
        if node.get("type") == "box" and node["contents"] and flex_texts(node["contents"][0])[:1] == [title]:
            return node["contents"]
    raise AssertionError(f"no panel {title}")


def calendar_weeks(card, title="🗓️ 班表月曆"):
    """The week rows of a card's calendar, Sunday first: seven boxes each."""
    return [row for row in panel(card, title)[2:] if len(row.get("contents", [])) == 7
            and all(cell["type"] == "box" for cell in row["contents"])]


def blank(cell) -> bool:
    """A day of another month filling a week: an empty box as wide as a date."""
    return cell["contents"] == [{"type": "filler"}]


def calendar_cells(card, title="🗓️ 班表月曆"):
    """The cells of a card's calendar week by week, Sunday first: a date's box, or None for a blank day."""
    return [None if blank(cell) else cell for row in calendar_weeks(card, title) for cell in row["contents"]]


class OldRostersInAMonthTests(BotCase):
    """Rosters saved by an older reader are read again on demand: slow, so only as many as fit in one reply."""

    def setUp(self):
        super().setUp()

        class Rereading(MonthAnalyzer):
            reread = []

            def ensure_names(inner, run_id):
                inner.reread.append(run_id)
                inner.results[run_id]["names_version"] = NAMES_VERSION
                return {"names": inner.results[run_id]["names"], "names_version": NAMES_VERSION}

            def ensure_duties(inner, run_id):
                inner.results[run_id]["duty_version"] = DUTY_VERSION
                return inner.results[run_id]

        self.analyzer = Rereading()
        self.analyzer.reread = []
        self.bot.analyzer = self.analyzer
        self.register("U1", "林小明")
        for day in (1, 2):
            run = f"old-{day}"
            self.analyzer.results[run] = roster(run, f"2026-10-0{day}", {"72": [slot(4, 2, "國際線")]},
                                                {"72": "林小明"})
            self.store.add_roster("Cteam", run, "U9", 1, f"2026-10-0{day}")

    def stats(self):
        with patch("all_staff.bot.today", return_value=date(2026, 10, 2)):
            return self.ask("本月統計")

    def test_old_rosters_are_read_again_and_counted(self):
        reply = self.stats()
        self.assertEqual(self.analyzer.reread, ["old-1", "old-2"])
        self.assertIn("2 小時", reply)
        self.assertNotIn("重新讀取", reply)
        self.assertEqual(self.transport.loadings[0], "U1")     # the typing indicator shows while that happens

    def test_what_does_not_fit_in_one_reply_is_left_for_the_next(self):
        self.bot.STATS_BUDGET = -1     # no time left at all
        reply = self.stats()
        self.assertEqual(self.analyzer.reread, [])
        self.assertIn("舊班表還在重新讀取，這次沒算：10/1、10/2。", reply)
        self.assertIn("這段期間的班表上沒有找到你的勤務", reply)
        self.bot.STATS_BUDGET = 30
        self.assertIn("2 小時", self.stats())

    def test_current_rosters_are_never_read_again(self):
        for data in self.analyzer.results.values():
            data.update(names_version=NAMES_VERSION, duty_version=DUTY_VERSION)
        self.assertIn("2 小時", self.stats())
        self.assertEqual(self.analyzer.reread, [])
        self.assertFalse(getattr(self.transport, "loadings", []))


class OldGridInAMonthTests(BotCase):
    """A roster still read with the old grid lacks the last four columns: the card says which days."""

    def setUp(self):
        super().setUp()

        class Regridding(MonthAnalyzer):
            def ensure_grid(inner, run_id, force=False):   # the production analyzer has this; the fakes do not
                raise AssertionError("statistics never start a re-reading of the grid")

        self.analyzer = Regridding()
        self.bot.analyzer = self.analyzer
        self.register("U1", "林小明")
        for day, grid in ((1, None), (2, GRID_VERSION)):
            run = f"run-{day}"
            data = roster(run, f"2026-10-0{day}", {"72": [slot(4, 2, "國際線")]}, {"72": "林小明"})
            if grid:
                data["grid_version"] = grid
            self.analyzer.results[run] = data
            self.store.add_roster("Cteam", run, "U9", 1, f"2026-10-0{day}")

    def test_days_read_with_the_old_grid_are_named(self):
        with patch("all_staff.bot.today", return_value=date(2026, 10, 2)):
            reply = self.ask("本月統計")
        self.assertIn("2 小時", reply)                          # they still count
        self.assertIn("最右四欄（A1A3站、內機、內部管理、全區帶勤）沒有讀到，時數可能偏少：10/1。", reply)

    def test_fakes_without_a_grid_are_not_flagged(self):
        self.bot.analyzer = MonthAnalyzer()
        self.bot.analyzer.results = self.analyzer.results
        with patch("all_staff.bot.today", return_value=date(2026, 10, 2)):
            self.assertNotIn("最右四欄", self.ask("本月統計"))


class StatsCardTests(unittest.TestCase):
    def test_a_card_without_duties_still_explains_its_data(self):
        month = ms.Month(2026, 10, [ms.Day(date(2026, 10, 1), ms.MISSING), ms.Day(date(2026, 10, 2), ms.UNKNOWN)])
        card = stats_card("林小明", month)
        texts = "\n".join(flex_texts(card["contents"]))
        self.assertIn("沒有找到你的勤務", texts)
        self.assertIn("沒有班表：10/1。", texts)
        self.assertIn("傳「我是 代號 10/2」補登", texts)
        self.assertEqual(card["altText"], "10 月統計｜林小明｜沒有找到勤務")
        self.assertEqual([box["action"]["data"] for box in tappable(card, "cal|")], ["cal|2026-10-02"])   # its roster
        self.assertNotIn("🍰 各地點時數", texts)
        self.assertNotIn("排定時數", texts)

    def test_without_any_roster_there_is_nothing_to_tap(self):
        card = stats_card("林小明", ms.Month(2026, 10, [ms.Day(date(2026, 10, 1), ms.MISSING)]))
        self.assertEqual(tappable(card, "cal|"), [])
        self.assertEqual(flex_texts(panel(card, "🗓️ 班表月曆"))[-2:], ["沒有班表", "今天"])

    def test_weeks_start_on_sunday_and_end_on_saturday(self):
        november = [ms.Day(date(2026, 11, n), ms.DUTY, 3, {"國際線": 3.0}) for n in range(1, 31)]
        cells = calendar_cells(stats_card("林小明", ms.Month(2026, 11, november)))   # 11/1/2026 is a Sunday
        self.assertEqual([flex_texts(cell)[0] if cell else None for cell in cells],
                         [str(n) for n in range(1, 31)] + [None] * 5)
        self.assertEqual(cells[29]["borderColor"], CUTE["today"])                     # the last day is today
        february = [ms.Day(date(2027, 2, 1), ms.DUTY, 3, {"國際線": 3.0})]           # a Monday, in a short month
        cells = calendar_cells(stats_card("林小明", ms.Month(2027, 2, february)))
        self.assertEqual((len(cells), cells[0], flex_texts(cells[1]), flex_texts(cells[28])[0], cells[29:]),
                         (35, None, ["1", "3h"], "28", [None] * 6))
        self.assertEqual(len(tappable(cells, "cal|")), 1)                            # the 27 days to come are faint

    def test_a_day_over_eight_hours_is_a_deeper_pink_and_says_so(self):
        days = [ms.Day(date(2026, 10, 1), ms.DUTY, 8, {"國際線": 8.0}),
                ms.Day(date(2026, 10, 2), ms.DUTY, 10, {"國際線": 10.0}),
                ms.Day(date(2026, 10, 3), ms.OFF, note="身心調適假"), ms.Day(date(2026, 10, 4), ms.OFF),
                ms.Day(date(2026, 10, 5), ms.PENDING), ms.Day(date(2026, 10, 6), ms.UNREADABLE)]
        card = stats_card("林小明", ms.Month(2026, 10, days))
        dated = [cell for cell in calendar_cells(card) if cell]
        self.assertEqual([flex_texts(cell)[1] for cell in dated[:6]], ["8h", "10h", "🌴", "🍃", "⏳", "⚠️"])
        self.assertEqual([dated[n]["backgroundColor"] for n in (0, 1)], [CALENDAR_UI["duty"], CALENDAR_UI["long"]])
        # an old roster still being read again opens; one that cannot be opened has nothing to show
        self.assertEqual([box["action"]["data"][-2:] for box in tappable(card, "cal|")], ["01", "02", "03", "04", "05"])
        self.assertEqual(flex_texts(panel(card, "🗓️ 班表月曆"))[-7:],
                         ["8h 排定時數", "超過 8 小時", "🌴 請假", "🍃 沒有勤務", "⏳ 讀取中", "⚠️ 讀不出來", "今天"])

    def test_a_long_full_month_stays_well_under_the_size_line_accepts(self):
        places = ["國際線", "國內線一線", "A1站", "內機", "內部管理", "全區帶勤", "中科物流", "登機管制門", "國際1、2號託運"]
        days = [ms.Day(date(2026, 10, n), ms.DUTY, 12, {places[n % 9]: 6.0, places[(n + 1) % 9]: 6.0}, unsure=1,
                       partial=n < 20) for n in range(1, 32)]
        card = stats_card("歐陽小明明", ms.Month(2026, 10, days))
        self.assertEqual(len(tappable(card, "cal|")), 31)
        self.assertLess(flex_size(card["contents"]), 25_000)   # LINE: 30 KB

    def test_the_worst_month_still_fits_one_bubble(self):
        """31 tappable days in six weeks (January 2027 starts on a Friday): long days, a long leave, names not found and
        rosters still being read again, so the legend and the notes are at their longest; every place, a long name and
        the tile for last month."""
        places = ["國際1、2號託運", "國內工作管制門", "A1A3站帶勤", "國際線（二）", "國內貨物安檢", "內部管理", "全區帶勤",
                  "中科物流", "登機管制門"]
        kinds = (lambda when: ms.Day(when, ms.DUTY, 19, {places[when.day % 9]: 10.0, places[(when.day + 4) % 9]: 9.0},
                                     unsure=19, partial=True),
                 lambda when: ms.Day(when, ms.OFF, note="身心調適假", partial=True),
                 lambda when: ms.Day(when, ms.UNKNOWN), lambda when: ms.Day(when, ms.PENDING))
        month = ms.Month(2027, 1, [kinds[n % 4](date(2027, 1, n)) for n in range(1, 32)])
        card = stats_card("歐陽小明明明", month)
        self.assertEqual(card["contents"]["type"], "bubble")
        self.assertEqual(len(calendar_cells(card)), 42)
        self.assertEqual(len(tappable(card, "cal|2027-01-")), 31)
        self.assertTrue(tappable(card, "cal|2026-12"))
        self.assertLess(flex_size(card["contents"]), FLEX_BUBBLE_LIMIT)
        self.assertTrue(all(node["text"] for node in flex_nodes(card) if node.get("type") in ("text", "span")))


class CalendarStartTests(unittest.TestCase):
    """The calendar begins in October 2026: no month or date before it is ever offered."""

    def test_last_month_is_offered_from_november_on(self):
        self.assertEqual(CALENDAR_START, date(2026, 10, 1))
        self.assertEqual([earlier_month(*month) for month in ((2026, 10), (2026, 11), (2027, 1))],
                         [None, (2026, 10), (2026, 12)])
        october = stats_card("林小明", ms.Month(2026, 10, [ms.Day(date(2026, 10, 7), ms.DUTY, 3, {"國際線": 3.0})]))
        self.assertEqual(tappable(october, "cal|2026-09"), [])
        self.assertNotIn("◀ 9 月的班表", flex_texts(october["contents"]))
        november = stats_card("林小明", ms.Month(2026, 11, [ms.Day(date(2026, 11, 3), ms.DUTY, 3, {"國際線": 3.0})]))
        tile, = tappable(november, "cal|2026-10")
        self.assertEqual(tile["action"]["data"], "cal|2026-10")
        self.assertEqual((flex_texts(tile)[0], tile["action"]["displayText"]), ("◀ 10 月的班表", "10 月的班表"))

    def test_a_month_before_it_has_no_calendar(self):
        card = stats_card("林小明", ms.Month(2026, 9, [ms.Day(date(2026, 9, 30), ms.DUTY, 3, {"國際線": 3.0})]))
        self.assertNotIn("🗓️ 班表月曆", flex_texts(card["contents"]))
        self.assertEqual(tappable(card, "cal|"), [])

    def test_dates_before_it_are_faint(self):
        days = [ms.Day(date(2026, 10, n), ms.DUTY, 3, {"國際線": 3.0}) for n in range(1, 8)]
        with patch("all_staff.bot.CALENDAR_START", date(2026, 10, 5)):
            card = stats_card("林小明", ms.Month(2026, 10, days))
        self.assertEqual([box["action"]["data"][-2:] for box in tappable(card, "cal|")], ["05", "06", "07"])
        first = next(cell for cell in calendar_cells(card) if cell)
        self.assertEqual((first.get("backgroundColor"), first["contents"][0]["color"]),
                         (None, CALENDAR_UI["future_ink"]))


class CalendarCardTests(unittest.TestCase):
    def test_last_month_alone_with_no_statistics(self):
        days = [ms.Day(date(2026, 10, n), ms.DUTY if n % 2 else ms.MISSING, 9 if n % 2 else 0,
                       {"國際線": 9.0} if n % 2 else {}) for n in range(1, 32)]
        card = calendar_card("林小明", ms.Month(2026, 10, days))
        bubble = card["contents"]
        self.assertEqual(flex_texts(bubble["header"]),
                         ["🗓️ 班表月曆", "林小明 的 10 月", "10 月有 16 天的班表。點日期，看當天的原圖和你的勤務 🐰"])
        self.assertEqual(bubble["header"]["background"]["type"], "linearGradient")
        texts = flex_texts(bubble["body"])
        self.assertEqual(texts[:2], ["🗓️ 2026 年 10 月", "點有顏色的日期看當天班表"])
        self.assertEqual(len(tappable(card, "cal|2026-10-")), 16)
        self.assertFalse([cell for cell in calendar_cells(card, "🗓️ 2026 年 10 月") if cell and cell.get("borderWidth")])
        self.assertNotIn("今天", texts)
        self.assertNotIn("排定時數", texts)                       # the statistics stay this month's
        back = next(node for node in flex_nodes(bubble["body"]) if node.get("action", {}).get("type") == "message")
        self.assertEqual((flex_texts(back)[0], back["action"]["text"]), ("📊 回到本月統計", "本月統計"))
        self.assertEqual(card["altText"], "🗓️ 10 月的班表月曆｜林小明｜16 天有班表，點日期看當天的班表")
        self.assertLess(flex_size(bubble), FLEX_BUBBLE_LIMIT)

    def test_a_month_without_rosters_says_so(self):
        card = calendar_card("林小明", ms.Month(2026, 10, [ms.Day(date(2026, 10, n), ms.MISSING) for n in range(1, 32)]))
        self.assertEqual(flex_texts(card["contents"]["header"])[-1], "10 月沒有班表可以看。")
        self.assertEqual(tappable(card, "cal|"), [])


class CalendarAlignmentTests(unittest.TestCase):
    """With flex alone LINE's app sizes the cells of a week from what they hold, then shares out the room left: every
    cell of the calendar, the weekday names and the days of other months included, has one fixed width instead, and
    each row spreads them out, so what a cell holds (a blank day, a wide 「10h」) moves no column."""

    def months(self):
        """From October 2026 on, until a month has begun on every day of the week: (year, month, its calendar)."""
        kinds = (ms.DUTY, ms.OFF, ms.MISSING, ms.UNKNOWN, ms.PENDING, ms.UNREADABLE)
        year, month, starts = 2026, 10, set()
        while len(starts) < 7:
            length = calendar.monthrange(year, month)[1]
            days = [ms.Day(date(year, month, n), kinds[n % 6], 12 if n % 4 == 0 else 9, {"國際線": 9.0})
                    for n in range(1, length + 1)]
            yield year, month, calendar_card("林小明", ms.Month(year, month, days), today=date(year, month, 7))
            starts.add(date(year, month, 1).weekday())
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    def test_every_cell_has_the_same_fixed_width(self):
        endings = set()
        for year, month, card in self.months():
            title = f"🗓️ {year} 年 {month} 月"
            weeks = calendar_weeks(card, title)
            names = panel(card, title)[1]
            for row in [names, *weeks]:
                self.assertEqual(row["justifyContent"], "space-between", (year, month))
                self.assertNotIn("spacing", row)
                self.assertEqual({(cell.get("flex"), cell.get("width")) for cell in row["contents"]},
                                 {(0, CALENDAR_CELL)}, (year, month))
            self.assertLessEqual(7 * float(CALENDAR_CELL.rstrip("%")), 96)   # room left for the gaps
            cells = [cell for week in weeks for cell in week["contents"]]
            self.assertEqual(len(cells), 7 * len(weeks))
            leading = next(index for index, cell in enumerate(cells) if not blank(cell))
            trailing = len(cells) - 1 - max(index for index, cell in enumerate(cells) if not blank(cell))
            self.assertEqual(leading, (date(year, month, 1).weekday() + 1) % 7, (year, month))
            endings.add(trailing)
        self.assertGreater(len(endings), 2)   # months ending mid-week, at the end of it and early in it

    def test_the_days_of_other_months_do_not_show(self):
        for year, month, card in self.months():
            for cell in [cell for week in calendar_weeks(card, f"🗓️ {year} 年 {month} 月") for cell in week["contents"]
                         if blank(cell)]:
                self.assertFalse({"backgroundColor", "action", "borderWidth"} & set(cell))
        october = stats_card("林小明", ms.Month(2026, 10, [ms.Day(date(2026, 10, 7), ms.DUTY, 3, {"國際線": 3.0})]))
        self.assertEqual(calendar_cells(october)[:4], [None] * 4)          # on 「本月統計」 too
        self.assertEqual(len([cell for cell in calendar_cells(october) if cell]), 31)


class CalendarTapTests(MonthCase):
    """A date tapped on 「班表月曆」: the whole photo of that day, then one's own duty card with the blue outline, then
    that month's calendar again, to tap the next date straight away."""
    settings = {"public_base": "https://bot.example"}

    def tap(self, data, user="U1", on=TODAY):
        count = len(self.transport.raw)
        with patch("all_staff.bot.today", return_value=on):
            self.bot.handle(postback_event(user, data))
        return self.transport.raw[-1] if len(self.transport.raw) > count else None

    def again(self, card, seen, dates):
        """``card`` is the calendar of ``seen``'s month headed with that date, the days in ``dates`` (two digits each)
        a tap away; the reply it ends fits LINE, with the quick replies under it alone."""
        reply = self.transport.raw[-1]
        self.assertIs(reply[-1], card)
        self.assertLessEqual(len(reply), 5)
        for message in reply:
            check_line_limits(self, message)
        self.assertEqual([message for message in reply[:-1] if "quickReply" in message], [])
        self.assertEqual(flex_texts(card["contents"]["header"]),
                         ["🗓️ 班表月曆", f"林小明 的 {seen.month} 月", f"剛看完 {seen.month}/{seen.day} 的班表，點其他日期繼續看 🐰"])
        self.assertEqual(card["altText"],
                         f"🗓️ {seen.month} 月的班表月曆｜林小明｜剛看完 {seen.month}/{seen.day} 的班表，點其他日期繼續看")
        prefix = f"cal|{seen.year}-{seen.month:02d}-"
        self.assertEqual([box["action"]["data"] for box in tappable(card, prefix)], [prefix + day for day in dates])
        back = next(node for node in flex_nodes(card["contents"]["body"])
                    if node.get("action", {}).get("type") == "message")
        self.assertEqual((flex_texts(back)[0], back["action"]["text"]), ("📊 回到本月統計", "本月統計"))
        return calendar_cells(card, f"🗓️ {seen.year} 年 {seen.month} 月")

    def test_a_date_sends_the_whole_photo_my_duty_card_my_outlined_cells_then_the_calendar_again(self):
        photo, card, mine, again = self.tap("cal|2026-10-06")   # the later of that day's two photos, as counted
        self.assertEqual(photo["originalContentUrl"], "https://bot.example/runs/run-6b/roster-v1.jpg")
        self.assertEqual(card["type"], "flex")
        self.assertTrue(card["altText"].startswith("林小明（72）　10/6（二）\n共 2 小時"))
        self.assertNotIn("footer", card["contents"])      # the whole photo is right above: no button for it
        self.assertEqual(mine["originalContentUrl"], "https://bot.example/runs/run-6b/people/72.jpg")
        cells = self.again(again, date(2026, 10, 6), ["01", "02", "04", "05", "06", "07"])   # not tomorrow's 10/8
        ringed, = [cell for cell in cells if cell and cell.get("borderWidth")]   # today, as on 「本月統計」
        self.assertEqual(flex_texts(ringed), ["7", "2h"])
        self.assertEqual(tappable(again, "cal|2026-09"), [])                    # no month before October
        actions = [item["action"]["label"] for item in again["quickReply"]["items"]]
        self.assertEqual((actions[0], actions[-1]), ("查其他人", "本月統計"))
        with patch("all_staff.bot.today", return_value=TODAY):
            self.assertIn("代號 26　10/6（二）", self.ask("查 26"))   # a plain lookup now looks at that day

    def test_the_card_has_no_other_dates_the_calendar_is_for_that(self):
        photo, card, mine, again = self.tap("cal|2026-10-06")
        self.assertEqual(date_chips(card), [])
        self.assertNotIn("📆 其他日期", flex_texts(card["contents"]))
        with patch("all_staff.bot.today", return_value=TODAY):   # every other way to a day keeps them
            for command, chip in (("我的班表 10/6", "10/7"), ("查 林小明 10/6", "查 林小明 10/7")):
                self.ask(command)
                card, mine = self.transport.raw[-1]                    # no calendar after them
                self.assertEqual(mine["type"], "image", command)
                self.assertIn("📆 其他日期", flex_texts(card["contents"]), command)
                self.assertIn(chip, date_chips(card), command)
            self.assertIn("其他日期：", self.ask("我的班表 10/5"))     # not on that day's name table: in words

    def test_a_day_off_still_shows_the_photo_and_what_the_roster_says(self):
        messages = self.tap("cal|2026-10-04")             # 輪休: no cells of mine to outline
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["originalContentUrl"], "https://bot.example/runs/run-4/roster-v1.jpg")
        texts = flex_texts(messages[1]["contents"])
        self.assertIn("這天輪休", texts)                                 # a calm tile, as on 「我的班表」
        self.assertIn("班表右下角的休假區記載：輪休。", texts)
        self.again(messages[2], date(2026, 10, 4), ["01", "02", "04", "05", "06", "07"])

    def test_without_photos_only_the_duty_card(self):
        self.bot.settings.images = False
        card, again = self.tap("cal|2026-10-07")
        self.assertTrue(card["altText"].startswith("林小明（72）　10/7（三）"))
        self.assertNotIn("footer", card["contents"])
        self.again(again, date(2026, 10, 7), ["01", "02", "04", "05", "06", "07"])

    def test_a_name_not_on_that_days_table_gets_the_dated_claim(self):
        photo, words, again = self.tap("cal|2026-10-05")
        self.assertEqual(photo["originalContentUrl"], "https://bot.example/runs/run-5/roster-v1.jpg")
        self.assertIn("在 10/5（一）的班表上沒有找到「林小明」", words["text"])
        self.assertIn("請傳「我是 你的代號 10/5」（例如 我是 72 10/5）", words["text"])
        self.assertNotIn("其他日期", words["text"])
        self.again(again, date(2026, 10, 5), ["01", "02", "04", "05", "06", "07"])
        self.assertEqual([item["action"]["label"] for item in again["quickReply"]["items"]], ["查其他人"])

    def test_a_date_without_a_roster_says_which_dates_have_one(self):
        words, = self.tap("cal|2026-10-03")
        self.assertIn("沒有 10/3（六）的班表", words["text"])

    def test_someone_elses_private_photo_is_never_shown(self):
        self.register("U2", "陳小華")
        self.add("mine-3", "2026-10-03", {"9": [slot(4, 2, "國際線")]}, {"9": "陳小華"}, chat="U2", by="U2")
        self.add("mine-7", "2026-10-07", {"9": [slot(4, 2, "國際線")]}, {"9": "陳小華"}, chat="U2", by="U2", sent=9e9)
        self.assertIn("沒有 10/3（六）的班表", self.tap("cal|2026-10-03")[0]["text"])
        self.assertEqual(self.tap("cal|2026-10-07")[0]["originalContentUrl"],
                         "https://bot.example/runs/run-7/roster-v1.jpg")
        self.assertEqual(len(tappable(self.transport.raw[-1][-1], "cal|")), 6)   # nor counted on the calendar after
        # the sender sees their own, the newer photo of that day
        self.assertEqual(self.tap("cal|2026-10-07", "U2")[0]["originalContentUrl"],
                         "https://bot.example/runs/mine-7/roster-v1.jpg")
        self.assertEqual(len(tappable(self.stats(user="U2")[1], "cal|")), 7)   # 10/3 is a date for its sender only

    def test_dates_not_offered_are_answered_in_words(self):
        self.add("sep", "2026-09-30", {"72": [slot(4, 2, "國際線")]}, {"72": "林小明"})
        self.assertIn("10/8 還沒到喔", self.tap("cal|2026-10-08")[0]["text"])     # tomorrow's roster is up already
        self.assertIn("班表月曆從 10/1 開始", self.tap("cal|2026-09-30")[0]["text"])
        self.assertIn("設定 你的姓名", self.tap("cal|2026-10-06", "U3")[0]["text"])
        for data in ("cal|2026-13-01", "cal|2026-10-32", "cal|2026-1-5", "cal|20261005", "cal|abc", "cal|",
                     "cal|2026-10-05|x", "cal"):
            self.assertIsNone(self.tap(data), data)

    def test_last_months_calendar_is_a_tap_away(self):
        nov = date(2026, 11, 3)
        self.add("nov-2", "2026-11-02", {"72": [slot(4, 2, "國際線")]}, {"72": "林小明"})
        with patch("all_staff.bot.today", return_value=nov):
            self.ask("本月統計")
        tile, = tappable(self.transport.raw[-1][-1], "cal|2026-10")
        card, = self.tap(tile["action"]["data"], on=nov)
        self.assertEqual(flex_texts(card["contents"]["header"])[1], "林小明 的 10 月")
        self.assertEqual([box["action"]["data"][-2:] for box in tappable(card, "cal|")],
                         ["01", "02", "04", "05", "06", "07", "08"])          # October is over: 10/8 is a date now
        self.assertEqual([item["action"]["text"] for item in card["quickReply"]["items"]], ["本月統計", "我的班表"])
        photo, duty, mine, again = self.tap("cal|2026-10-08", on=nov)
        self.assertEqual(photo["originalContentUrl"], "https://bot.example/runs/run-8/roster-v1.jpg")
        cells = self.again(again, date(2026, 10, 8), ["01", "02", "04", "05", "06", "07", "08"])   # all of October
        self.assertEqual([cell for cell in cells if cell and cell.get("borderWidth")], [])   # today is not in it
        self.assertNotIn("今天", flex_texts(again["contents"]))
        self.assertEqual(tappable(again, "cal|2026-09"), [])
        # a date this month: November's calendar, with October a tap away as on 「本月統計」
        photo, duty, mine, again = self.tap("cal|2026-11-02", on=nov)
        cells = self.again(again, date(2026, 11, 2), ["02"])
        self.assertEqual([flex_texts(cell)[0] for cell in cells if cell and cell.get("borderWidth")], ["3"])
        tile, = tappable(again, "cal|2026-10")
        self.assertEqual(flex_texts(tile)[0], "◀ 10 月的班表")

    def test_an_older_month_has_no_calendar_after_it(self):
        dec = date(2026, 12, 5)   # an old card: 班表月曆 offers November and December only
        photo, duty, mine = self.tap("cal|2026-10-06", on=dec)
        self.assertTrue(duty["altText"].startswith("林小明（72）　10/6（二）"))
        self.assertEqual(mine["quickReply"]["items"][-1]["action"]["text"], "本月統計")
        self.assertEqual(date_chips(duty), [])

    def test_a_day_whose_roster_cannot_be_read_any_more_is_answered_in_words(self):
        on = date(2026, 10, 10)
        del self.analyzer.results["run-7"]                                      # result.json went missing
        *_, words, again = self.tap("cal|2026-10-07", on=on)
        self.assertIn("班表資料讀不出來", words["text"])
        cells = self.again(again, date(2026, 10, 7), ["01", "02", "04", "05", "06", "08"])
        seventh = next(cell for cell in cells if cell and flex_texts(cell)[0] == "7")
        self.assertEqual((flex_texts(seventh)[1], seventh["backgroundColor"]), ("⚠️", CALENDAR_UI["missing"]))

    def test_the_calendar_never_reads_an_old_roster_again(self):
        """Old rosters wait for 「本月統計」 (⏳, still a tap away): the reply cannot wait for them."""
        reread = []

        def ensure_names(run_id):
            reread.append(run_id)
            self.analyzer.results[run_id]["names_version"] = NAMES_VERSION
            return {"names": self.analyzer.results[run_id]["names"], "names_version": NAMES_VERSION}

        def ensure_duties(run_id):
            self.analyzer.results[run_id]["duty_version"] = DUTY_VERSION
            return self.analyzer.results[run_id]

        self.analyzer.ensure_names, self.analyzer.ensure_duties = ensure_names, ensure_duties
        *_, again = self.tap("cal|2026-10-06")
        self.assertEqual(reread, ["run-6b"])                  # the day tapped, read again as any lookup of it is
        cells = self.again(again, date(2026, 10, 6), ["01", "02", "04", "05", "06", "07"])
        self.assertEqual([flex_texts(cell)[1] for cell in cells if cell][:7], ["⏳", "⏳", "　", "⏳", "⏳", "2h", "⏳"])
        self.assertEqual(self.transport.pushes, [])

    def test_with_the_reply_window_nearly_spent_the_day_goes_out_without_the_calendar(self):
        self.bot.CALENDAR_MARGIN = self.bot.settings.reply_window + 1
        photo, card, mine = self.tap("cal|2026-10-06")
        self.assertEqual(date_chips(card), [])
        self.assertEqual(mine["quickReply"]["items"][-1]["action"]["text"], "本月統計")
        photo, words = self.tap("cal|2026-10-05")
        self.assertEqual(words["quickReply"]["items"][0]["action"]["label"], "查其他人")
        self.assertEqual(self.transport.pushes, [])                 # a reply, never a push for it

    def test_a_slow_read_of_the_tapped_day_leaves_the_calendar_out(self):
        """The clock starts when the tap arrives, so reading the tapped day's roster again counts: once it leaves less
        than CALENDAR_MARGIN of the reply window the calendar is left out, and the day still goes out as a reply."""
        clock, window = [1000.0], self.bot.settings.reply_window

        def read_again(seconds):
            def ensure_duties(run_id):
                clock[0] += seconds
                self.analyzer.results[run_id]["duty_version"] = DUTY_VERSION
                return self.analyzer.results[run_id]
            return ensure_duties

        fake = types.SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
        self.analyzer.ensure_duties = read_again(window - self.bot.CALENDAR_MARGIN + 5)   # 45 of the 50 s
        with patch("all_staff.bot.time", fake):
            reply = self.tap("cal|2026-10-06")
        self.assertEqual(len(reply), 3)                              # photo, card, blue photo: no calendar
        self.assertEqual([m for m in reply if "班表月曆" in m.get("altText", "")], [])
        self.assertEqual(reply[-1]["quickReply"]["items"][-1]["action"]["text"], "本月統計")
        self.assertEqual(self.transport.pushes, [])
        self.analyzer.results["run-6b"].pop("duty_version")
        self.analyzer.ensure_duties = read_again(window - self.bot.CALENDAR_MARGIN - 5)   # 35 s: room for it
        with patch("all_staff.bot.time", fake):
            reply = self.tap("cal|2026-10-06")
        self.assertEqual(len(reply), 4)
        self.assertIn("班表月曆", reply[-1]["altText"])
        self.assertEqual(self.transport.pushes, [])

    def test_other_months_are_not_offered(self):
        nov, dec = date(2026, 11, 3), date(2026, 12, 5)
        self.assertIn("班表月曆從 2026 年 10 月開始", self.tap("cal|2026-09", on=nov)[0]["text"])
        self.assertIn("班表月曆從 2026 年 10 月開始", self.tap("cal|0000-10", on=nov)[0]["text"])   # a forged year 0
        self.assertIn("只看得到這個月和上個月", self.tap("cal|2026-10", on=dec)[0]["text"])
        self.assertIn("只看得到這個月和上個月", self.tap("cal|2027-01", on=dec)[0]["text"])
        card, = self.tap("cal|2026-12", on=dec)                                  # this month: 本月統計, calendar and all
        self.assertEqual(flex_texts(card["contents"])[0], "🐰 本月統計")
        self.assertIn("設定 你的姓名", self.tap("cal|2026-10", "U3", on=nov)[0]["text"])
        for data in ("cal|2026-00", "cal|2026-13", "cal|26-10"):
            self.assertIsNone(self.tap(data, on=nov), data)


if __name__ == "__main__":
    unittest.main()
