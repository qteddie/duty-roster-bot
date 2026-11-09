"""One day's duty card (``duty_card``) and the date pickers that fix a wrong roster date where it is seen.

The card: a gradient band (who, the day, when it starts and ends, the hours), the stretches of duty as tiles with the
breaks between them (today's under way ringed), one calm tile for a day without duties, the other dates as chips.
A date that is not the one read off the photo says so on the card; an administrator's card can change it, the uploader
of an undated group photo can pick its date while there is no administrator, and the request card of a held photo has
「📅 其他日期」 for a day older than the three it offers.
"""
import json
import re
import time
import unittest
from datetime import date, datetime
from unittest.mock import patch

from all_staff.bot import (FLEX_BUBBLE_LIMIT, MY_DAY_UI, SLOT_UI, STAFF_UI, TAIPEI, date_note, date_request_card,
                           duty_card, flex_size, original_roster_link, roster_summary, undated_notice)
from test_all_staff_bot import (BotCase, HeldCase, date_chips, flex_buttons, flex_nodes, flex_texts, image_event,
                                postback_event, slot)
from test_all_staff_roster_gate import check_line_limits
from test_all_staff_stats import TODAY as MONTH_TODAY
from test_all_staff_stats import MonthCase

ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
DAY = date(2026, 10, 1)
OTHER_DAY = datetime(2026, 10, 3, 9, tzinfo=TAIPEI)   # not the roster's day: no ring, no 今天
LINE_NOTE = "📅 這張的日期是後來設定的"


def on(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TAIPEI)


def person(code, *stretches):
    """``stretches``: (first hour, hour it ends, duty[, confidence]) → that person's slots, an hour each."""
    return {"code": code, "slots": [slot(hour - 1, 2, duty, *confidence)
                                     for first, end, duty, *confidence in stretches for hour in range(first, end)]}


WORK = person("72", (5, 7, "國際線"), (7, 8, "國內線一線", .5), (10, 12, "國際線備勤"))
LEAVE = {"available": True, "rest": ["15"], "leave": [{"code": "9", "kind": "事假"}, {"code": "10"}]}


def reading(*people, leave=None, read="2026-10-01", **extra):
    return {"run": {"id": "run-1", "roster_date": read}, "people": list(people),
            "leave_table": leave or {"available": False}, **extra}


def photo(day="2026-10-01", read="2026-10-01", run="run-1", **extra):
    sent = datetime(2026, 9, 30, 21, 5, tzinfo=TAIPEI).timestamp()
    return {"run_id": run, "chat_id": "Cgroup", "roster_date": day, "read_date": read, "sent_at": sent, **extra}


def pickers(message):
    """Every date picker of a message: on its card, or under it as a quick reply."""
    actions = [node["action"] for node in flex_nodes(message.get("contents", {})) if node.get("action")]
    actions += [item["action"] for item in message.get("quickReply", {}).get("items", [])]
    return [action for action in actions if action["type"] == "datetimepicker"]


def check_limits(test, message):
    """``check_line_limits``, and what LINE's spec (messaging-api.yml) says of a datetimepicker: mode date, an initial
    day as YYYY-MM-DD, data up to 300 characters, a label up to 20."""
    check_line_limits(test, message)
    for picker in pickers(message):
        test.assertEqual(picker["mode"], "date")
        test.assertRegex(picker["initial"], ISO)
        test.assertTrue(picker["label"].strip())
        test.assertLessEqual(len(picker["label"]), 20)
        test.assertLessEqual(len(picker["data"]), 300)
        test.assertEqual(set(picker), {"type", "label", "data", "mode", "initial", "min", "max"})
        test.assertRegex(picker["min"], ISO)
        test.assertRegex(picker["max"], ISO)
        test.assertTrue(picker["min"] <= picker["initial"] <= picker["max"])   # LINE refuses it otherwise


def bare(picker):
    """A date picker without its min and max (``pick_range``, checked by ``check_limits``)."""
    return {key: value for key, value in picker.items() if key not in ("min", "max")}


def tiles(card):
    """The stretches of duty, in order: boxes whose first box holds the hours of one (「05–07」, 「✓ 05–07」)."""
    found = []
    for node in flex_nodes(card["contents"]["body"]):
        first = (node.get("contents") or [{}])[0] if node.get("type") == "box" else {}
        words = (first.get("contents") or [{}])[0].get("text", "") if first.get("type") == "box" else ""
        if node.get("layout") == "horizontal" and re.fullmatch(r"(✓ )?\d\d–\d\d", words):
            found.append(node)
    return found


class DutyCardTests(unittest.TestCase):
    def card(self, code="72", name="林小明", roster=None, result=None, now=OTHER_DAY, **options):
        made = duty_card(code, name, roster or photo(), result or reading(WORK, person("26")), now=now, **options)
        check_limits(self, made)
        return made

    def test_the_band_names_the_person_the_day_and_the_hours(self):
        card = self.card(mine=True)
        bubble = card["contents"]
        self.assertEqual(bubble["size"], "giga")
        self.assertEqual(bubble["header"]["background"]["type"], "linearGradient")
        self.assertEqual(flex_texts(bubble["header"]), ["📋 我的班表", "林小明（72）", "📅 10/1（四）",
                                                        "05:00", "上班", "12:00", "下班", "5 小時", "排定時數"])
        # the words a search, a test or a notification preview finds, as before
        self.assertTrue(card["altText"].startswith("林小明（72）　10/1（四）\n共 5 小時｜1 段待確認\n05–07　國際線\n"
                                                   "07–08　國內一線 ？\n10–12　國際備勤"), card["altText"])
        self.assertEqual(flex_texts(self.card()["contents"]["header"])[0], "🔎 同仁班表")   # someone else's day
        self.assertEqual(flex_texts(self.card(name=None)["contents"]["header"])[1], "代號 72")

    def test_each_stretch_is_a_tile_with_its_hours_duty_and_length(self):
        found = tiles(self.card())
        self.assertEqual([flex_texts(tile) for tile in found],
                         [["05–07", "國際線", "2 小時"], ["07–08", "國內一線 ？", "1 小時"], ["10–12", "國際備勤", "2 小時"]])
        unsure = next(node for node in flex_nodes(found[1]) if node.get("text") == "國內一線 ？")
        self.assertEqual(unsure["contents"][1], {"type": "span", "text": " ？", "color": SLOT_UI["now"]})
        self.assertTrue(all(tile["borderWidth"] == "1px" and tile["backgroundColor"] == STAFF_UI["white"]
                            for tile in found))                                  # another day: none ringed or greyed
        self.assertIn("？ 待確認，請對照原圖", flex_texts(self.card()["contents"]["body"]))

    def test_two_duties_in_one_hour_count_once_and_both_remain_visible(self):
        both = reading({"code": "72", "slots": WORK["slots"] + [slot(4, 9, "國際線備勤")]})
        card = self.card(result=both)
        self.assertIn("5 小時", flex_texts(card["contents"]["header"]))
        self.assertEqual(flex_texts(tiles(card)[0])[1], "國際線／國際備勤")

    def test_on_todays_roster_the_stretch_under_way_is_ringed(self):
        card = self.card(now=on(7, 30))
        self.assertIn("📅 10/1（四）・今天", flex_texts(card["contents"]["header"]))
        done, current, upcoming = tiles(card)
        self.assertEqual((current["borderWidth"], current["borderColor"]), ("2px", SLOT_UI["now"]))
        self.assertIn("▶ 現在", flex_texts(current))
        self.assertEqual((flex_texts(done)[0], done["backgroundColor"]), ("✓ 05–07", MY_DAY_UI["past"]))
        self.assertIn("⏭️ 下一段", flex_texts(upcoming))
        self.assertIn("▶ 現在　✓ 已結束　？ 待確認，請對照原圖", flex_texts(card["contents"]["body"])[-1].splitlines())
        for now, relative in ((on(9, day=date(2026, 9, 30)), "・明天"), (on(9, day=date(2026, 10, 2)), "・昨天")):
            card = self.card(now=now)
            self.assertIn(f"📅 10/1（四）{relative}", flex_texts(card["contents"]["header"]))
            self.assertFalse([tile for tile in tiles(card) if tile["borderWidth"] == "2px"])

    def test_the_breaks_between_stretches_are_shown_and_the_one_under_way_says_so(self):
        texts = flex_texts(self.card()["contents"]["body"])
        self.assertEqual(texts[texts.index("1 小時") + 1], "☕ 08–10 休息 2 小時")
        self.assertIn("▶ 現在 ☕ 08–10 休息 2 小時", flex_texts(self.card(now=on(9))["contents"]["body"]))

    def test_a_day_without_duties_is_one_calm_tile_that_says_why(self):
        result = reading(WORK, leave=LEAVE)
        cases = (("15", "陳小華", True, on(9), result, "今天輪休", "班表右下角的休假區記載：輪休。", MY_DAY_UI["off"]),
                 ("9", "王大同", False, OTHER_DAY, result, "這天事假", "班表右下角的休假區記載：事假。", MY_DAY_UI["off"]),
                 ("10", None, False, OTHER_DAY, result, "這天請假", "班表右下角記載請假，假別沒有辨識出來。",
                  MY_DAY_UI["off"]),
                 ("26", "王大同", False, OTHER_DAY, result, "這天沒有排勤務",
                  "班表上沒有王大同的勤務，休假區也沒有寫到王大同。", MY_DAY_UI["off"]),
                 ("26", "林小明", True, OTHER_DAY, reading(WORK), "這天沒有排勤務",
                  "班表上沒有你的勤務；休假區沒有辨識出來，是輪休還是請假請對照原圖。", MY_DAY_UI["off"]),
                 ("8", None, False, OTHER_DAY, result, "這張班表上沒有辨識到代號 8",
                  "可能休假，或照片中看不清楚；請對照整張班表。", MY_DAY_UI["missing"]))
        for code, name, mine, now, read, title, note, tint in cases:
            with self.subTest(code=code, mine=mine):
                card = self.card(code, name, result=read, now=now, mine=mine)
                body = card["contents"]["body"]["contents"]
                self.assertEqual(flex_texts(body[0])[1:3], [title, note])
                self.assertEqual(body[0]["backgroundColor"], tint)
                self.assertNotIn("上班", flex_texts(card["contents"]["header"]))   # no hours to show
                self.assertEqual(tiles(card), [])

    def test_the_other_dates_are_chips_that_send_the_date(self):
        others = [date(2026, 9, 30), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)]
        card = self.card(now=on(9), mine=True, others=others)
        self.assertEqual(date_chips(card), ["9/30", "10/2", "10/5", "10/6"])
        chips = [node for node in flex_nodes(card["contents"]) if node.get("action", {}).get("type") == "message"]
        self.assertEqual([flex_texts(chip) for chip in chips[:3]],
                         [["9/30", "週三・昨天"], ["10/2", "週五・明天"], ["10/5", "週一"]])
        self.assertEqual(chips[0]["action"]["label"], "9/30")
        rows = [node for node in flex_nodes(card["contents"]["body"])
                if node.get("layout") == "horizontal" and chips[3] in node.get("contents", [])]
        self.assertEqual([part["type"] for part in rows[0]["contents"]], ["box", "filler", "filler"])  # rows of three
        self.assertIn("📆 其他日期", flex_texts(card["contents"]))
        # someone else's day follows their name; a code alone changes every day, so it gets none
        self.assertEqual(date_chips(self.card(others=others[:2])), ["查 林小明 9/30", "查 林小明 10/2"])
        bare = self.card(name=None, others=others)
        self.assertEqual(date_chips(bare), [])
        self.assertNotIn("📆 其他日期", flex_texts(bare["contents"]))

    def test_the_recheck_warning_stays_on_the_card(self):
        warning = "這張舊班表的代號尚未複核，1／11、2／22、3／33 請對照原圖。"
        card = self.card(result=reading(WORK, duty_recheck_error=warning))
        box = next(node for node in flex_nodes(card["contents"]) if node.get("backgroundColor") == STAFF_UI["warn"])
        self.assertEqual(flex_texts(box), [f"⚠️ {warning}"])

    def test_a_push_says_what_is_new_and_how_the_day_changed(self):
        card = self.card(mine=True, news="🔄 班表更新", change="你的勤務與上一版不同")
        texts = flex_texts(card["contents"]["header"])
        self.assertEqual(texts[:2], ["🔄 班表更新", "林小明（72）"])
        self.assertIn("🔁 你的勤務與上一版不同", texts)
        self.assertTrue(card["altText"].startswith("🔄 班表更新｜林小明（72）・你的勤務與上一版不同　10/1（四）"))

    def test_the_whole_photo_and_the_outlined_one(self):
        card = self.card(show_original=True, photo=True)
        self.assertEqual(card["contents"]["footer"], original_roster_link(photo()))
        self.assertIn("📷 下方是標示圖：藍框是勤務格", "\n".join(flex_texts(card["contents"]["body"])).splitlines())
        self.assertNotIn("footer", self.card())

    def test_every_hour_another_duty_still_fits_one_bubble(self):
        busy = {"code": "甲", "slots": [slot(row, column, f"第{row}欄{column}號非常長的工作名稱（備註）")
                                        for row in range(4, 23) for column in (2, 17)]}
        options = dict(mine=True, news="🔄 班表更新", change="上一版沒有你的勤務", redate=True, photo=True,
                       show_original=True, others=[date(2026, 9, day) for day in range(24, 30)])
        result = reading(busy, read="2026-09-29", duty_recheck_error="這張舊班表的代號尚未複核，請對照原圖。")
        roomy = self.card("甲", "歐陽娜娜娜", photo(read="2026-09-29"), result, now=on(12), **options)
        self.assertLessEqual(flex_size(roomy["contents"]), FLEX_BUBBLE_LIMIT)
        self.assertEqual(len(tiles(roomy)), 19)
        with patch("all_staff.bot.MY_DAY_BUDGET", flex_size(roomy["contents"]) - 1):   # tiles become lines
            tight = self.card("甲", "歐陽娜娜娜", photo(read="2026-09-29"), result, now=on(12), **options)
        self.assertLess(flex_size(tight["contents"]), flex_size(roomy["contents"]))
        self.assertEqual(tiles(tight), [])
        texts = flex_texts(tight["contents"]["body"])
        self.assertEqual([text for text in texts if re.fullmatch(r"(▶ |✓ )?\d\d–\d\d", text)][:3],
                         ["✓ 05–06", "✓ 06–07", "✓ 07–08"])
        self.assertIn("▶ 12–13", texts)
        self.assertEqual(tight["altText"], roomy["altText"])
        self.assertIn("📅 改這張的日期", texts)


class DateLineTests(unittest.TestCase):
    """A date that is not the one read off the photo says so, in the band, next to the date."""

    def line(self, roster, result=None):
        card = duty_card("72", "林小明", roster, result or reading(WORK), now=OTHER_DAY)
        return [text for text in flex_texts(card["contents"]["header"]) if text.startswith("📅 這張")]

    def test_set_later_or_read_off_the_photo(self):
        self.assertEqual(self.line(photo()), [])                                       # the date read: nothing to say
        self.assertEqual(self.line(photo(read="2026-09-29")), [f"{LINE_NOTE}（照片上讀到 9/29）"])
        self.assertEqual(self.line(photo(read=None), reading(WORK, read=None)), [f"{LINE_NOTE}（照片上沒有讀到日期）"])

    def test_a_row_from_before_read_date_existed_uses_the_reading_kept_with_the_result(self):
        self.assertEqual(self.line(photo(read=None), reading(WORK, read="2026-09-29")), [f"{LINE_NOTE}（照片上讀到 9/29）"])
        self.assertEqual(self.line(photo(read=None), reading(WORK)), [])

    def test_a_photo_without_a_date_has_nothing_to_compare(self):
        self.assertEqual(date_note(photo(day=None, read=None)), "")
        card = duty_card("72", "林小明", photo(day=None, read=None), reading(WORK, read=None), now=OTHER_DAY)
        self.assertIn("📅 班表日期未辨識", flex_texts(card["contents"]["header"]))


class AdministratorTileTests(unittest.TestCase):
    def test_only_an_administrators_card_opens_the_date_picker_on_that_photo(self):
        card = duty_card("72", "林小明", photo(read="2026-09-29"), reading(WORK), now=OTHER_DAY, redate=True)
        check_limits(self, card)
        self.assertEqual([bare(picker) for picker in pickers(card)],
                         [{"type": "datetimepicker", "label": "改這張的日期", "data": "redate|run-1", "mode": "date",
                           "initial": "2026-10-01"}])
        tile = next(node for node in flex_nodes(card["contents"]) if node.get("action", {}).get("data") == "redate|run-1")
        self.assertEqual(flex_texts(tile), ["📅 改這張的日期", "只改這一張｜管理員才看得到"])
        self.assertEqual(pickers(duty_card("72", "林小明", photo(), reading(WORK), now=OTHER_DAY)), [])
        undated = duty_card("72", "林小明", photo(day=None, read=None), reading(WORK), now=OTHER_DAY, redate=True)
        self.assertEqual(pickers(undated)[0]["initial"], "2026-09-30")                  # the day it was sent


class LimitsCase(BotCase):
    """Everything the bot sends in these tests is within what LINE accepts."""

    def tearDown(self):
        for messages in self.transport.raw + [messages for _, messages in self.transport.raw_pushes]:
            for message in messages:
                check_limits(self, message)
        super().tearDown()

    def picked(self, data, day, user):
        """A day picked on LINE's date picker (``params``), or ``day=None`` for a postback without one."""
        count = len(self.transport.replies)
        event = postback_event(user, data)
        if day is not None:
            event["postback"]["params"] = {"date": day}
        self.bot.handle(event)
        return self.reply() if len(self.transport.replies) > count else None

    def card(self):
        return next(message for message in self.transport.raw[-1] if message["type"] == "flex")


class AdministratorCardTests(LimitsCase):
    friends = {"U1", "U2", "U9"}
    settings = {"admins": ("U9",), "notify": False}

    def setUp(self):
        super().setUp()
        self.register("U1", "林小明")
        self.store.add_roster("Cgroup", "run-1", "U2", 5, "2026-10-01")

    def test_the_administrator_changes_that_photos_date_from_its_card(self):
        self.ask("查 72 10/1", "U9")
        self.assertEqual([picker["data"] for picker in pickers(self.card())], ["redate|run-1"])
        self.ask("10/1", "U1")                                                   # nobody else gets the tile
        self.assertEqual(pickers(self.card()), [])
        self.assertNotIn("📅 改這張的日期", self.reply())
        reply = self.picked("redate|run-1", "2026-10-02", "U1")                  # nor can use an old one
        self.assertIsNone(reply)
        self.assertEqual(self.store.roster_by_run("run-1")["roster_date"], "2026-10-01")
        self.assertIn("日期已改為 10/2（五）", self.picked("redate|run-1", "2026-10-02", "U9"))
        self.assertIn("照片上辨識到的日期是 10/1（四）。如果改錯了，再點一次「📅 改這張的日期」改回來。", self.reply())
        self.assertEqual(self.store.roster_by_run("run-1")["roster_date"], "2026-10-02")
        self.ask("10/2", "U1")                                                   # and everyone sees it was set later
        self.assertIn(f"{LINE_NOTE}（照片上讀到 10/1）", flex_texts(self.card()["contents"]["header"]))
        self.assertTrue(self.card()["altText"].startswith("林小明（72）　10/2（五）"))
        self.assertTrue(self.store.roster_on("U1", date(2026, 10, 2)))
        self.assertIsNone(self.store.roster_on("U1", date(2026, 10, 1)))

    def test_their_own_private_photo_too_but_nobody_elses(self):
        self.store.add_roster("U9", "mine", "U9", 3, "2026-10-03")
        self.store.add_roster("U1", "theirs", "U1", 3, "2026-10-03")
        self.assertIn("你私訊", self.picked("redate|mine", "2026-10-04", "U9"))
        self.assertEqual(self.store.roster_by_run("mine")["roster_date"], "2026-10-04")
        for day in ("2026-10-04", "2026-10-4", "", "tomorrow", None):
            self.assertIsNone(self.picked("redate|theirs", day, "U9"), day)
        for day in ("2026-10-5", "20261005", "", None):                          # a picker sends YYYY-MM-DD
            self.assertIsNone(self.picked("redate|mine", day, "U9"), day)
        self.assertEqual(self.store.roster_by_run("theirs")["roster_date"], "2026-10-03")
        self.assertEqual(self.store.roster_by_run("mine")["roster_date"], "2026-10-04")


class ReviewFixTests(LimitsCase):
    friends = {"U1", "U2", "U9"}
    settings = {"admins": ("U9",), "notify": False}

    def setUp(self):
        super().setUp()
        self.register("U1", "林小明")
        self.store.add_roster("Cgroup", "run-1", "U2", 5, "2026-10-01")

    def test_a_day_far_from_when_the_photo_was_sent_is_ignored(self):
        for day in ("2027-10-01", "2100-12-31", "2025-10-01"):                    # a year typo on the picker
            self.assertIsNone(self.picked("redate|run-1", day, "U9"), day)
        self.assertEqual(self.store.roster_by_run("run-1")["roster_date"], "2026-10-01")
        self.ask("查 72 10/1", "U9")
        picker, = pickers(self.card())
        self.assertTrue(picker["min"] <= "2026-10-01" <= picker["max"])

    def test_a_name_missing_from_a_redated_photo_still_shows_the_date_and_the_picker(self):
        self.store.add_roster("Cgroup", "run-0", "U2", 5, "2026-09-30")          # another day to point to
        self.store.set_roster_date("run-1", "2026-10-02")                        # read 10/1, set to 10/2 later
        self.register("U2", "張大千")                                             # not on this name table
        self.ask("10/2", "U2")
        self.assertIn(f"{LINE_NOTE}（照片上讀到 10/1）", self.reply())
        self.assertNotIn("datetimepicker", str(self.transport.raw[-1]))         # only for administrators
        self.ask("查 張大千 10/2", "U9")
        self.assertIn(f"{LINE_NOTE}（照片上讀到 10/1）", self.reply())
        self.assertIn("直接輸入「查 張大千 9/30」，就能查看", self.reply())         # the lookup, not one's own day
        picker = next(item["action"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]
                      if item["action"]["type"] == "datetimepicker")
        self.assertEqual((picker["data"], picker["label"]), ("redate|run-1", "📅 改這張的日期"))


class UploaderPickerTests(LimitsCase):
    """Without administrators, the uploader of a group photo whose date could not be read picks it for that photo."""
    friends = {"U1", "U2", "U3"}
    settings = {"notify": False}

    def setUp(self):
        super().setUp()
        self.register("U1", "林小明"); self.register("U2", "陳小華")
        self.analyzer.data["run"]["roster_date"] = None
        self.sent = time.time() - 3600

    def upload(self, user="U2", run="run-1"):
        self.analyzer.data["run"]["id"] = run
        event = image_event(user, "Cgroup")
        event["timestamp"] = int(self.sent * 1000)
        self.bot.handle(event)

    def notice(self, user="U2"):
        return [messages for to, messages in self.transport.raw_pushes if to == user][-1][-1]

    def test_the_uploader_picks_the_date_of_that_very_photo(self):
        self.upload()
        notice = self.notice()
        self.assertIn("表頭的日期沒有讀出來", notice["text"])
        self.assertIn("「📅 選這張的日期」", notice["text"])
        self.assertNotRegex(notice["text"], r"日期 \d")                         # no real-looking date to copy
        sent = datetime.fromtimestamp(self.sent, TAIPEI).date()
        self.assertEqual([bare(picker) for picker in pickers(notice)],
                         [{"type": "datetimepicker", "label": "📅 選這張的日期", "data": "redate|run-1",
                           "mode": "date", "initial": sent.isoformat()}])
        self.upload(user="U1", run="run-2")                                       # a later photo by someone else
        self.assertIsNone(self.picked("redate|run-1", "2026-10-08", "U1"))      # not theirs to date
        self.assertIsNone(self.picked("redate|run-2", "2026-10-08", "U2"))      # nor this one U2's
        self.assertIsNone(self.store.roster_by_run("run-1")["roster_date"])
        self.assertIn("日期已改為 10/8（四）", self.picked("redate|run-1", "2026-10-08", "U2"))
        self.assertEqual(self.store.roster_by_run("run-1")["roster_date"], "2026-10-08")
        self.assertIsNone(self.store.roster_by_run("run-2")["roster_date"])      # the later photo untouched
        self.ask("10/8", "U2")
        self.assertIn(f"{LINE_NOTE}（照片上沒有讀到日期）", flex_texts(self.card()["contents"]["header"]))

    def test_with_administrators_the_uploaders_picker_is_ignored(self):
        self.upload()
        self.bot.settings.admins = ("U9",)                                        # administrators set up since
        self.assertIsNone(self.picked("redate|run-1", "2026-10-08", "U2"))
        self.assertIsNone(self.store.roster_by_run("run-1")["roster_date"])

    def test_a_photo_with_a_date_needs_no_picker(self):
        self.analyzer.data["run"]["roster_date"] = "2026-10-01"
        self.upload()
        self.assertEqual([to for to, _ in self.transport.raw_pushes], [])


class OtherDatePickerTests(HeldCase):
    """「📅 其他日期」 on the card that asks an administrator which day a held photo is."""

    def tearDown(self):
        for messages in self.transport.raw + [messages for _, messages in self.transport.raw_pushes]:
            for message in messages:
                check_limits(self, message)
        super().tearDown()

    def pick(self, day, user="U9", run="run-no-date"):
        event = postback_event(user, f"date|{run}|pick")
        if day is not None:
            event["postback"]["params"] = {"date": day}
        self.bot.handle(event)

    def test_the_card_offers_any_other_day_for_that_photo(self):
        self.send("no-date", 0)
        card = self.transport.raw_pushes[0][1][-1]
        sent = datetime.fromtimestamp(self.base, TAIPEI).date()
        self.assertEqual([bare(picker) for picker in pickers(card)],
                         [{"type": "datetimepicker", "label": "其他日期", "data": "date|run-no-date|pick",
                           "mode": "date", "initial": sent.isoformat()}])
        self.assertIn("📅 其他日期", flex_texts(card["contents"]))
        self.assertEqual(len(flex_buttons(card)), 4)                              # the three days and 略過這張 stay
        self.assertNotRegex(json.dumps(card, ensure_ascii=False), r"日期 \d")
        self.ask("日期 2026/10/5", "U9")                                           # the typed date's confirmation
        self.assertEqual(pickers(self.transport.raw[-1][0]), [])

    def test_a_day_picked_there_confirms_that_photo_and_notifies_as_a_button_does(self):
        self.send("no-date", 0)
        self.quiet()
        self.pick("2026-09-29")
        self.assertIn("日期已設為 9/29（二）", self.reply())
        self.assertIn("現在通知已開通知的 1 人", self.reply())
        self.assertIn("📌 新班表｜林小明（72）　9/29（二）", self.told("U1")[-1][0])
        roster = self.roster("run-no-date")
        self.assertEqual((roster["roster_date"], roster["held"]), ("2026-09-29", 0))
        self.pick("2026-09-28")                                                   # spent, like the buttons
        self.assertIn("已經確認過日期：9/29（二）", self.reply())
        self.assertEqual(self.roster("run-no-date")["roster_date"], "2026-09-29")

    def test_a_malformed_day_or_anyone_elses_pick_is_ignored(self):
        self.send("no-date", 0)
        self.quiet()
        for day, user in (("2026-9-29", "U9"), ("", "U9"), ("tomorrow", "U9"), (None, "U9"), ("2026-02-30", "U9"),
                          ("2026-09-29", "U2"), ("2026-09-29", "U1")):
            self.pick(day, user)
        self.pick("2026-09-29", run="run-unknown")
        self.assertEqual(self.transport.replies, [])
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual((self.roster("run-no-date")["roster_date"], self.roster("run-no-date")["held"]), (None, 1))


class CalendarDateLineTests(MonthCase):
    """The case that was reported: a 9/29 photo set to 10/1 with a typed 「日期 10/1」 opens from 10/1 on 班表月曆."""

    def test_a_calendar_tap_on_a_photo_dated_later_says_so(self):
        self.add("run-929", "2026-09-29", {"72": [slot(4, 2, "國際線")]}, {"72": "林小明"})   # the newest photo
        with patch("all_staff.bot.today", return_value=MONTH_TODAY):
            self.ask("日期 10/1")
            self.bot.handle(postback_event("U1", "cal|2026-10-01"))
        card, again = self.transport.raw[-1]                       # no photos here: the card, then the calendar
        for message in (card, again):
            check_limits(self, message)
        self.assertTrue(card["altText"].startswith("林小明（72）　10/1（四）"))
        self.assertIn(f"{LINE_NOTE}（照片上讀到 9/29）", flex_texts(card["contents"]["header"]))
        with patch("all_staff.bot.today", return_value=MONTH_TODAY):
            self.bot.handle(postback_event("U1", "cal|2026-10-07"))
        self.assertFalse([text for text in flex_texts(self.transport.raw[-1][0]["contents"]) if LINE_NOTE in text])

    def test_an_administrators_card_keeps_its_date_picker_on_the_way_back_to_the_calendar(self):
        self.bot.settings.admins = ("U1",)
        self.bot.settings.public_base = "https://bot.example"
        with patch("all_staff.bot.today", return_value=MONTH_TODAY):
            self.bot.handle(postback_event("U1", "cal|2026-10-06"))
            reply = self.transport.raw[-1]
            photo, card, mine, again = reply[-4:]   # an administrator may get the 意見 waiting for them first
            self.assertLessEqual(len(reply), 5)
            for message in reply:
                check_limits(self, message)
            self.assertEqual([(picker["label"], picker["data"]) for picker in pickers(card)],
                             [("改這張的日期", "redate|run-6b")])
            self.assertIn("📅 改這張的日期", flex_texts(card["contents"]))
            self.assertEqual(date_chips(card), [])
            self.assertNotIn("📆 其他日期", flex_texts(card["contents"]))
            self.assertEqual(flex_texts(again["contents"]["header"])[-1], "剛看完 10/6 的班表，點其他日期繼續看 🐰")
            self.bot.handle(postback_event("U1", "cal|2026-10-05"))   # not on its name table: in the quick replies
            photo, words, again = self.transport.raw[-1][-3:]
            check_limits(self, again)
            self.assertNotIn("其他日期", words["text"])
            self.assertEqual([action["data"] for action in pickers(again)], ["redate|run-5"])
            self.ask("我的班表 10/6")                                   # elsewhere the dates stay, the picker too
            card = self.transport.raw[-1][0]
            self.assertIn("📆 其他日期", flex_texts(card["contents"]))
            self.assertEqual([action["data"] for action in pickers(card)], ["redate|run-6b"])


class WordingTests(unittest.TestCase):
    """Where a picker now exists, no literal 「日期 10/1」 invites copying a real-looking date."""

    def test_no_example_date_to_copy(self):
        held = photo(day=None, read=None)
        for message in (date_request_card(held, 2), undated_notice(held)):
            check_limits(self, message)
            self.assertNotRegex(json.dumps(message, ensure_ascii=False), r"日期 \d")
        summary = roster_summary({"sheets": [{"cells": [], "warnings": []}], "people": []}, held)
        self.assertIn("「日期 月/日」", summary)

    def test_the_notice_names_the_photo_by_when_it_was_sent(self):
        notice = undated_notice(photo(day=None, read=None))
        self.assertTrue(notice["text"].startswith("⚠ 你在群組 9/30 21:05 傳的班表，日期未辨識"))
        self.assertEqual(pickers(notice)[0]["initial"], "2026-09-30")


if __name__ == "__main__":
    unittest.main()
