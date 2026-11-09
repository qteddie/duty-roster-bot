"""「我的班表」: one card for the next hour, today and tomorrow, in place of 今天班表／明天班表／下個時段."""
import json
import unittest
from datetime import date, datetime
from unittest.mock import patch

from all_staff import rich_menu
from all_staff.bot import (FLEX_BUBBLE_LIMIT, MY_DAY, SLOT_UI, TAIPEI, MyDay, flex_size, my_day_card, next_hour)
from test_all_staff_bot import BotCase, FakeAnalyzer, flex_nodes, flex_texts, postback_event, slot

TODAY, TOMORROW = date(2026, 10, 8), date(2026, 10, 9)
AREA = 2   # a 國際 column; the card does not care which


def at(hour, minute=0):
    return datetime(2026, 10, 8, hour, minute, tzinfo=TAIPEI)


def person(code, *stretches):
    """``stretches``: (first hour, hour it ends, duty[, confidence]) → that person's slots, an hour each."""
    return {"code": code, "slots": [slot(hour - 1, AREA, duty, *confidence)
                                     for first, end, duty, *confidence in stretches for hour in range(first, end)]}


def day(when, who=None, code=None, leave=None, **result):
    """A day whose roster is uploaded: ``who`` (a person) is on duty, or ``code`` is theirs and they are off."""
    data = {"people": [who] if who else [], "leave_table": leave or {"available": False}, **result}
    return MyDay(when, {"run_id": f"run-{when.day}", "roster_date": when.isoformat()}, data,
                 who["code"] if who else code, who)


WORK = person("72", (5, 7, "國際線"), (7, 8, "國內線一線", .5), (10, 13, "國際線備勤"), (15, 18, "國際線"))
NEXT = person("15", (6, 9, "國內線一線"), (13, 16, "國際線"))


def words(now, first, second=None):
    return next_hour(now, first, second or MyDay(TOMORROW))


def card_texts(card):
    return flex_texts(card["contents"])


class NextHourTests(unittest.TestCase):
    """The band at the top: what the next hour is, in so many words."""

    def test_on_duty_now_and_the_next_hour(self):
        said = words(at(10, 42), day(TODAY, WORK))
        self.assertEqual((said["label"], said["value"], said["duty"]), ("⏭️ 下個小時 11–12", "國際備勤", True))
        self.assertEqual(said["now"], "▶️ 現在 10–11：國際備勤")
        self.assertIsNone(said["then"])

    def test_the_next_hour_without_duty_says_when_the_next_one_starts(self):
        said = words(at(8, 15), day(TODAY, WORK))
        self.assertEqual((said["label"], said["value"], said["duty"]), ("⏭️ 下個小時 09–10", "沒有勤務", False))
        self.assertIsNone(said["now"])
        self.assertEqual(said["then"], "👉 下一段 10–13：國際備勤")

    def test_on_duty_now_and_a_break_next(self):
        said = words(at(12, 5), day(TODAY, WORK))
        self.assertEqual((said["value"], said["now"], said["then"]),
                         ("休息 ☕", "▶️ 現在 12–13：國際備勤", "👉 下一段 15–18：國際線"))

    def test_after_the_last_duty_it_looks_at_tomorrow(self):
        said = words(at(21, 30), day(TODAY, WORK), day(TOMORROW, NEXT))
        self.assertEqual((said["label"], said["value"]), ("⏭️ 下個小時 22–23", "沒有勤務"))
        self.assertEqual(said["then"], "👉 明天第一段 06–09：國內一線")
        self.assertEqual(words(at(17, 10), day(TODAY, WORK), day(TOMORROW, NEXT))["value"], "下班 🎉")

    def test_near_midnight_the_next_hour_is_tomorrows(self):
        late = day(TODAY, person("72", (20, 24, "國際線")))
        said = words(at(23, 20), late, day(TOMORROW, NEXT))
        self.assertEqual((said["label"], said["value"]), ("⏭️ 下個小時 00–01（明天）", "下班 🎉"))
        self.assertEqual(said["now"], "▶️ 現在 23–24：國際線")
        self.assertEqual(said["then"], "👉 明天第一段 06–09：國內一線")
        self.assertEqual(words(at(23, 20), day(TODAY, WORK), MyDay(TOMORROW))["then"], "🌙 明天的班表還沒上傳")

    def test_before_the_first_duty_of_the_morning(self):
        said = words(at(4, 50), day(TODAY, WORK))
        self.assertEqual((said["label"], said["value"]), ("⏭️ 下個小時 05–06", "國際線"))

    def test_why_the_next_hour_is_not_known(self):
        self.assertEqual(words(at(9), MyDay(TODAY), day(TOMORROW, NEXT))["value"], "今天的班表還沒上傳")
        self.assertEqual(words(at(9), MyDay(TODAY), day(TOMORROW, NEXT))["then"], "👉 明天第一段 06–09：國內一線")
        self.assertEqual(words(at(9), day(TODAY))["value"], "今天的班表上找不到你")
        self.assertEqual(words(at(9), day(TODAY), day(TOMORROW))["then"], "🔍 明天的班表上找不到你")

    def test_a_day_off_is_named(self):
        rest = {"available": True, "rest": ["15"], "leave": []}
        leave = {"available": True, "rest": [], "leave": [{"code": "15", "kind": "事假"}]}
        off = day(TODAY, code="72", leave={"available": True, "rest": ["72"], "leave": []})
        self.assertEqual(words(at(9), off)["value"], "沒有勤務")
        self.assertEqual(words(at(9), off, day(TOMORROW, code="15", leave=rest))["then"], "💤 明天輪休")
        self.assertEqual(words(at(9), off, day(TOMORROW, code="15", leave=leave))["then"], "🌴 明天事假")
        self.assertEqual(words(at(9), off, day(TOMORROW, code="15"))["then"], "🍃 明天沒有排勤務")

    def test_an_unsure_duty_keeps_its_question_mark(self):
        said = words(at(6, 10), day(TODAY, WORK))
        self.assertEqual((said["value"], said["now"]), ("國內一線 ？", "▶️ 現在 06–07：國際線"))


class MyDayCardTests(unittest.TestCase):
    def card(self, now, first, second=None, **extra):
        made = my_day_card("蕭志明", now, first, second or MyDay(TOMORROW), **extra)
        self.assertLessEqual(len(made["altText"]), 400)
        self.assertTrue(all(node["text"] for node in flex_nodes(made) if node.get("type") in ("text", "span")))
        self.assertLess(flex_size(made["contents"]), FLEX_BUBBLE_LIMIT)
        return made

    def test_one_giga_bubble_with_the_next_hour_in_the_band(self):
        made = self.card(at(10, 42), day(TODAY, WORK), day(TOMORROW, NEXT), photo=TODAY)
        bubble = made["contents"]
        self.assertEqual((bubble["type"], bubble["size"]), ("bubble", "giga"))
        self.assertEqual(bubble["header"]["background"]["type"], "linearGradient")
        band = flex_texts(bubble["header"])
        self.assertEqual(band[:5], ["📅 蕭志明的班表", "10/8（四）・現在 10:42", "⏭️ 下個小時 11–12", "國際備勤",
                                    "▶️ 現在 10–11：國際備勤"])
        self.assertTrue(made["altText"].startswith("📅 蕭志明｜10/8（四）｜⏭️ 下個小時 11–12：國際備勤｜"))
        self.assertIn("📷 下方是 10/8（四）的標示圖：藍框是你的勤務", "\n".join(card_texts(made)))

    def test_today_is_a_timeline_ringed_now_greyed_when_over(self):
        made = self.card(at(10, 42), day(TODAY, WORK), day(TOMORROW, NEXT))
        texts = card_texts(made)
        self.assertEqual(texts[texts.index("☀️ 今天 10/8（四）") + 1], "代號 72・共 9 小時")
        tiles = [node for node in flex_nodes(made["contents"]["body"])
                 if node.get("type") == "box" and node.get("cornerRadius") == "12px" and node.get("paddingAll")]
        shown = [flex_texts(tile) for tile in tiles]
        self.assertEqual(shown[:4], [["✓ 05–07", "國際線", "2 小時"], ["✓ 07–08", "國內一線 ？", "1 小時"],
                                     ["10–13", "國際備勤", "▶ 現在", "3 小時"], ["15–18", "國際線", "⏭️ 下一段", "3 小時"]])
        self.assertEqual(tiles[2]["borderColor"], SLOT_UI["now"])
        self.assertEqual(tiles[0]["contents"][1]["color"], SLOT_UI["past"])
        self.assertIn("☕ 08–10 休息 2 小時", texts)
        self.assertIn("▶ 現在　✓ 已結束　？ 待確認，請對照原圖", "\n".join(texts))   # a legend for the marks

    def test_a_break_under_way_says_so(self):
        texts = card_texts(self.card(at(8, 15), day(TODAY, WORK)))
        self.assertIn("▶ 現在 ☕ 08–10 休息 2 小時", texts)

    def test_tomorrow_is_a_few_lines(self):
        made = self.card(at(10, 42), day(TODAY, WORK), day(TOMORROW, NEXT))
        texts = card_texts(made)
        start = texts.index("🌙 明天 10/9（五）")
        self.assertEqual(texts[start + 1:start + 8], ["代號 15・共 6 小時", "06–09", "國內一線", "3 小時",
                                                      "13–16", "國際線", "3 小時"])

    def test_tomorrow_not_uploaded_yet_is_a_faint_placeholder(self):
        made = self.card(at(10, 42), day(TODAY, WORK))
        texts = card_texts(made)
        self.assertIn("🌙", texts)
        self.assertIn("明天的班表還沒上傳", texts)
        self.assertIn("上傳後再點「我的班表」就看得到。", texts)
        self.assertTrue(made["altText"].endswith("明天：班表還沒上傳"))

    def test_today_not_uploaded_but_tomorrow_is(self):
        made = self.card(at(9), MyDay(TODAY), day(TOMORROW, NEXT),
                         latest={"run_id": "run-1", "roster_date": "2026-10-01"})
        texts = card_texts(made)
        self.assertIn("今天的班表還沒上傳", texts)
        self.assertIn("👉 明天第一段 06–09：國內一線", texts)
        self.assertIn("06–09", texts)
        link = next(node for node in flex_nodes(made) if node.get("text") == "👉 先看最新的一張：10/1（四）的班表")
        self.assertEqual(link["action"]["data"], "mine|run-1")
        self.assertEqual(link["action"]["displayText"], "我的班表 10/1")

    def test_days_off_and_a_leave_table_that_was_not_read(self):
        rest = day(TODAY, code="72", leave={"available": True, "rest": ["72"], "leave": []})
        kinds = {"available": True, "rest": [], "leave": [{"code": "15", "kind": "身心調適假"}]}
        texts = card_texts(self.card(at(9), rest, day(TOMORROW, code="15", leave=kinds)))
        self.assertIn("今天輪休", texts)
        self.assertIn("班表右下角的休假區記載：輪休。", texts)
        self.assertIn("明天身心調適假", texts)
        self.assertEqual(texts[texts.index("☀️ 今天 10/8（四）") + 1], "代號 72")
        unknown = {"available": True, "rest": [], "leave": [{"code": "72", "kind": None}]}
        self.assertIn("班表右下角記載請假，假別沒有辨識出來。", card_texts(self.card(at(9), day(TODAY, code="72",
                                                                                          leave=unknown))))
        unread = card_texts(self.card(at(9), day(TODAY, code="72")))   # the leave table was not recognised
        self.assertIn("今天沒有排勤務", unread)
        self.assertIn("班表上沒有你的勤務；休假區沒有辨識出來，是輪休還是請假請對照原圖。", unread)
        silent = card_texts(self.card(at(9), day(TODAY, code="72", leave={"available": True, "rest": [], "leave": []})))
        self.assertIn("班表上沒有你的勤務，休假區也沒有寫到你。", silent)

    def test_a_name_not_on_the_name_table_is_said_for_each_day(self):
        made = self.card(at(9), day(TODAY), day(TOMORROW))
        texts = card_texts(made)
        self.assertIn("今天的班表上找不到你", texts)
        self.assertIn("明天的班表上找不到你", texts)
        self.assertIn("姓名對照表上沒有「蕭志明」（可能休假，或字沒辨識清楚）。", texts)
        self.assertIn("✏️ 有上班就傳「我是 代號 10/8」", texts)
        self.assertIn("✏️ 有上班就傳「我是 代號 10/9」", texts)
        fills = [node["action"]["fillInText"] for node in flex_nodes(made)
                 if node.get("action", {}).get("inputOption") == "openKeyboard"]
        self.assertEqual(fills, ["我是 10/8 ", "我是 10/9 "])   # the date first: the code is typed after it

    def test_an_unsure_hour_has_a_warm_question_mark_and_a_legend(self):
        made = self.card(at(5, 10), day(TODAY, WORK))
        unsure = next(node for node in flex_nodes(made) if node.get("text") == "國內一線 ？")
        self.assertEqual(unsure["contents"][-1], {"type": "span", "text": " ？", "color": SLOT_UI["now"]})
        self.assertIn("？ 待確認，請對照原圖", "\n".join(card_texts(made)))
        self.assertNotIn("？ 待確認", "\n".join(card_texts(self.card(at(5), day(TODAY, NEXT)))))

    def test_an_old_roster_that_was_not_rechecked_says_so_once(self):
        error = "這張舊班表的代號尚未複核，1／11、2／22、3／33 請對照原圖。"
        made = self.card(at(9), day(TODAY, WORK, duty_recheck_error=error), day(TOMORROW, NEXT, duty_recheck_error=error))
        self.assertEqual("\n".join(card_texts(made)).count(error), 1)

    def test_even_a_new_duty_every_hour_on_both_days_fits_one_bubble(self):
        names = ("國際1、2號託運", "國內工作管制門", "A1A3站帶勤", "國際線（二）", "國內貨物安檢", "內部管理")
        busy = lambda code: person(code, *((hour, hour + 1, f"{names[hour % 6]}{hour}", .5) for hour in range(5, 24)))
        made = self.card(at(14, 10), day(TODAY, busy("72")), day(TOMORROW, busy("15")), photo=TODAY)
        self.assertLess(flex_size(made["contents"]), 25_000)
        self.assertEqual(made["contents"]["type"], "bubble")
        texts = card_texts(made)
        self.assertIn("▶ 14–15", texts)   # today in lines now, still marked
        self.assertIn("✓ 05–06", texts)

    def test_today_turns_into_lines_only_when_the_tiles_would_not_fit(self):
        first, second = day(TODAY, WORK), day(TOMORROW, NEXT)
        roomy = my_day_card("蕭志明", at(10, 42), first, second)
        with patch("all_staff.bot.MY_DAY_BUDGET", flex_size(roomy["contents"]) - 1):
            tight = my_day_card("蕭志明", at(10, 42), first, second)
        self.assertNotIn("▶ 10–13", card_texts(roomy))
        self.assertIn("▶ 10–13", card_texts(tight))
        self.assertIn("✓ 05–07", card_texts(tight))
        self.assertLess(flex_size(tight["contents"]), flex_size(roomy["contents"]))
        self.assertEqual(tight["altText"], roomy["altText"])


class DayAnalyzer(FakeAnalyzer):
    """One result per run id; the photo of a person is there when they have duties on that run."""

    def __init__(self, results):
        super().__init__()
        self.results, self.leave_reads = results, []

    def result(self, run_id):
        return self.results[run_id]

    def person_image(self, run_id, code):
        person = next((p for p in self.results[run_id]["people"] if p["code"] == code), None)
        return (f"{run_id}/people/{code}.jpg", f"{run_id}/people/{code}-preview.jpg") if person else None

    def ensure_leave(self, run_id):
        self.leave_reads.append(run_id)
        return {"available": True, "rest": ["15"], "leave": []}


def roster(run, when, people, names):
    return {"run": {"id": run, "codes": len(people), "roster_date": when},
            "sheets": [{"cells": [], "warnings": []}], "people": people,
            "names": {code: [name] for code, name in names.items()}}


class MyRosterBotTests(BotCase):
    settings = {"public_base": "https://bot.example", "notify": False}

    def setUp(self):
        super().setUp()
        self.analyzer = DayAnalyzer({
            "today": roster("today", "2026-10-08", [WORK, person("26", (5, 7, "國內線一線"))],
                            {"72": "林小明", "26": "王大同"}),
            "tomorrow": roster("tomorrow", "2026-10-09", [person("15", (9, 12, "國際線")), person("72", (5, 7, "國內線一線"))],
                               {"15": "林小明", "72": "陳小華"}),
            "old": roster("old", "2026-10-01", [person("72", (5, 7, "國際線"))], {"72": "林小明"})})
        self.bot.analyzer = self.analyzer
        self.register("U1", "林小明")
        for run, when in (("old", "2026-10-01"), ("today", "2026-10-08"), ("tomorrow", "2026-10-09")):
            self.store.add_roster("Cgroup", run, "U2", 3, when)

    def show(self, hour, minute=0, command="我的班表", user="U1"):
        with self.at(at(hour, minute)):
            self.ask(command, user)
        return self.transport.raw[-1]

    def test_every_old_word_opens_the_card_and_dates_keep_the_duty_card(self):
        for command in MY_DAY:
            messages = self.show(10, command=command)
            self.assertEqual(flex_texts(messages[0]["contents"]["header"])[0], "📅 林小明的班表", command)
        for command, expected in (("今天", "林小明（72）　10/8（四）"), ("明天", "林小明（15）　10/9（五）"),
                                  ("10/1", "林小明（72）　10/1（四）"), ("我的班表 10/9", "林小明（15）　10/9（五）"),
                                  ("查 72 10/8", "林小明（72）　10/8（四）")):
            with self.at(at(10)):
                self.assertIn(expected, self.ask(command), command)
        self.assertIn("設定 你的姓名", self.ask("我的班表", "U3"))

    def test_the_photo_is_of_the_next_duty_and_nothing_pops_up_under_it(self):
        card, photo = self.show(10)
        self.assertEqual(photo["originalContentUrl"], "https://bot.example/runs/today/people/72.jpg")
        self.assertNotIn("quickReply", card)
        self.assertNotIn("quickReply", photo)                      # no options offered under the answer
        with self.at(at(10)):
            self.assertIn("林小明（72）　10/8（四）", self.ask("查 72"))   # still looks in the roster just shown
        _, photo = self.show(19)                                     # nothing left today: tomorrow's photo
        self.assertEqual(photo["originalContentUrl"], "https://bot.example/runs/tomorrow/people/15.jpg")
        self.assertIn("📷 下方是 10/9（五）的標示圖：藍框是你的勤務", "\n".join(flex_texts(self.show(19)[0]["contents"])))

    def test_without_photos_only_the_card_is_sent(self):
        self.bot.settings.images = False
        messages = self.show(10)
        self.assertEqual(len(messages), 1)
        self.assertNotIn("quickReply", messages[0])
        self.assertNotIn("📷 下方是", "\n".join(flex_texts(messages[0]["contents"])))

    def test_a_day_button_opens_that_photos_duty_card(self):
        self.show(10)
        self.bot.handle(postback_event("U1", "mine|tomorrow"))
        self.assertIn("林小明（15）　10/9（五）", self.reply())
        self.store.add_roster("U2", "private", "U2", 3, "2026-10-08")   # someone else's private photo
        count = len(self.transport.replies)
        self.bot.handle(postback_event("U1", "mine|private"))
        self.assertEqual(len(self.transport.replies), count)

    def test_a_plain_lookup_afterwards_stays_on_the_roster_shown(self):
        self.show(19)   # the next duty is tomorrow's
        with self.at(at(19)):
            self.assertIn("代號 72　10/9（五）", self.ask("查 72"))
        self.show(10)   # and here today's
        with self.at(at(10)):
            self.assertIn("林小明（72）　10/8（四）", self.ask("查 72"))

    def test_off_tomorrow_the_leave_table_is_read_once_and_named(self):
        self.analyzer.results["tomorrow"]["people"] = []
        card = self.show(19)[0]
        self.assertEqual(self.analyzer.leave_reads, ["tomorrow"])
        texts = flex_texts(card["contents"])
        self.assertIn("明天輪休", texts)
        self.assertIn("💤 明天輪休", texts)

    def test_names_not_found_offer_the_claim(self):
        self.analyzer.results["today"]["names"] = {}
        card, photo = self.show(10)   # nothing to photograph today: tomorrow's duty is
        self.assertIn("今天的班表上找不到你", flex_texts(card["contents"]))
        self.assertEqual(photo["originalContentUrl"], "https://bot.example/runs/tomorrow/people/15.jpg")
        self.assertNotIn("quickReply", photo)
        claim = next(node["action"] for node in flex_nodes(card) if node.get("action", {}).get("inputOption"))
        self.assertEqual(claim["fillInText"], "我是 10/8 ")                 # the tile on the card is the way
        # Sent as filled in, the claim is on today's photo, not on tomorrow's where 林小明 is already 15
        with self.at(at(10)):
            self.assertIn("例如「我是 10/8 72」", self.ask("我是 10/8"))   # sent before typing the code
            self.assertIn("10/8（四）班表上你是代號 26", self.ask("我是 10/8 26"))
            self.assertIn("林小明（15）　10/9（五）", self.ask("明天"))
            self.assertIn("林小明（26）　10/8（四）", self.ask("今天"))

    def test_a_date_first_claim_never_becomes_a_name(self):
        with self.at(at(10)):
            for typed, said in (("我是 明天 90", "「90」不是班表上的代號"), ("我是 今天 72號", "「72號」不是班表上的代號"),
                                ("我是 10/8 90", "「90」不是班表上的代號"), ("我是 明天", "例如「我是 明天 72」")):
                self.assertIn(said, self.ask(typed), typed)
            self.assertEqual(self.store.user("U1")["staff_name"], "林小明")   # still their name
            self.assertIn("「9/31」不是看得懂的日期", self.ask("我是 72 9/31"))      # not the newest roster instead
            self.assertIn("10/9（五）班表上你是代號 15", self.ask("我是 明天 15"))
            self.assertIn("已登記：王明天", self.ask("我是 王明天", "U4"))           # a name with 明天 in it is a name

    def test_the_not_found_hint_carries_the_rosters_date(self):
        self.analyzer.results["today"]["names"] = {}
        with self.at(at(10)):
            reply = self.ask("今天")
        self.assertIn("請傳「我是 你的代號 10/8」（例如 我是 72 10/8）", reply)

    def test_a_roster_waiting_for_its_date_is_not_offered(self):
        self.store.db.execute("delete from rosters where run_id in ('today', 'tomorrow')")
        self.store.db.commit()
        self.analyzer.results["held"] = roster("held", None, [person("72", (9, 12, "國際線"))], {"72": "林小明"})
        self.store.add_roster("Cgroup", "held", "U2", 1, None, held=True)   # an administrator still has to date it
        messages = self.show(10)
        sent = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("held", sent)
        self.assertIn("mine|old", sent)


class MenuTests(unittest.TestCase):
    def test_the_roster_row_is_one_button_across_the_band(self):
        self.assertEqual(rich_menu.ROW_COUNTS, (1, 3, 3, 3))
        self.assertEqual(sum(rich_menu.ROW_COUNTS), len(rich_menu.BUTTONS))
        first = rich_menu.areas()[0]
        self.assertEqual(first["bounds"], {"x": rich_menu.CATEGORY_WIDTH, "y": 0,
                                           "width": rich_menu.WIDTH - rich_menu.CATEGORY_WIDTH,
                                           "height": rich_menu.HEIGHT // 4})
        self.assertEqual(first["action"], {"type": "message", "label": "我的班表", "text": "我的班表"})
        self.assertIn("我的班表", MY_DAY)
        menu = json.dumps(rich_menu.menu(), ensure_ascii=False)
        for gone in ("今天班表", "明天班表", "下個時段"):
            self.assertNotIn(gone, menu)
            self.assertIn(gone, MY_DAY)   # menus uploaded before still work


if __name__ == "__main__":
    unittest.main()
