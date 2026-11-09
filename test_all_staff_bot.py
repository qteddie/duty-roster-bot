"""Tests for the V2 LINE roster bot (transport-agnostic logic + webhook signature)."""
import copy
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from all_staff.bot import (STAFF_UI, TAIPEI, RosterBot, Settings, Store, duty_change, duty_signature,
                           format_duties, parse_code, who_is_on)


def slot(row, column, duty, conf=.99):
    return {"row": row, "time": f"{row + 1:02d}-{row + 2:02d}", "column": column, "duty": duty,
            "confidence": conf, "needs_review": conf < .75}


# Codes rotate daily: the name table says who is who on this roster.
RESULT = {
    "run": {"id": "run-1", "codes": 5, "roster_date": "2026-10-01"},
    "sheets": [{"cells": [{"pred": [{"code": "72", "conf": .99}]}] * 3, "warnings": []}],
    "people": [
        {"code": "72", "slots": [slot(4, 2, "國際線"), slot(5, 2, "國際線"), slot(6, 17, "國內線一線", .5)]},
        {"code": "甲", "slots": [slot(8, 9, "國際線備勤")]},
        {"code": "26", "slots": [slot(4, 3, "國際線"), slot(4, 31, "A1站")]},
        {"code": "5", "slots": [slot(4, 20, "國內線二線")]},
    ],
    "names": {"72": ["林小明", "林小朋"], "甲": ["陳小華", ""], "26": ["王大同", "王大"], "5": ["某某某"]},
}


class FakeAnalyzer:
    def __init__(self, result=RESULT):
        self.calls, self.data = 0, result

    def analyze(self, payload, name):
        self.calls += 1
        return self.data

    def result(self, run_id):
        return self.data

    def person_image(self, run_id, code):
        person = next((p for p in self.data["people"] if p["code"] == code), None)
        return (f"{run_id}/people/{code}.jpg", f"{run_id}/people/{code}-preview.jpg") if person else None

    def roster_image(self, run_id):
        return f"{run_id}/roster-v1.jpg", f"{run_id}/roster-v1-preview.jpg"

    def slot_image(self, run_id, row, area):
        stem = f"{run_id}/slots/h{row + 1:02d}-{row + 2:02d}-{area}-v1"
        return stem + ".jpg", stem + "-preview.jpg"

    def staff_table_image(self, run_id):
        return f"{run_id}/staff-table-v1.jpg", f"{run_id}/staff-table-v1-preview.jpg"


def flex_texts(node):
    if isinstance(node, list):
        return [t for child in node for t in flex_texts(child)]
    if isinstance(node, dict):
        return ([node['text']] if node.get('type') == 'text' else
                [t for child in node.values() for t in flex_texts(child)])
    return []


def flex_nodes(node):
    """Every box, text and span of a Flex message, depth first."""
    if isinstance(node, list):
        return [n for child in node for n in flex_nodes(child)]
    if isinstance(node, dict):
        return [node] * ("type" in node) + [n for child in node.values() for n in flex_nodes(child)]
    return []


def cells_of(node):
    """Every text built of spans (a code in bold, then a name) in reading order."""
    if isinstance(node, list):
        return [cell for child in node for cell in cells_of(child)]
    if isinstance(node, dict):
        if node.get("type") == "text" and node.get("contents"):
            return [node]
        return [cell for child in node.values() for cell in cells_of(child)]
    return []


def tappable(node, prefix):
    """The boxes whose postback data starts with ``prefix``, in reading order."""
    if isinstance(node, list):
        return [box for child in node for box in tappable(child, prefix)]
    if isinstance(node, dict):
        if node.get("type") == "box" and str(node.get("action", {}).get("data", "")).startswith(prefix):
            return [node]
        return [box for child in node.values() for box in tappable(child, prefix)]
    return []


def date_chips(message):
    """What the 「📆 其他日期」 chips of a duty card send when tapped, in reading order: a date, or 「查 姓名 日期」."""
    return [node["action"]["text"] for node in flex_nodes(message.get("contents", {}))
            if node.get("type") == "box" and node.get("action", {}).get("type") == "message"]


def hour_chips(node):
    """The tappable hours of a 「查看時段」 card in reading order: 國際 05–06 … 23–24, then 國內."""
    if isinstance(node, list):
        return [chip for child in node for chip in hour_chips(child)]
    if isinstance(node, dict):
        if node.get("type") == "box" and str(node.get("action", {}).get("data", "")).startswith("area|"):
            return [node]
        return [chip for child in node.values() for chip in hour_chips(child)]
    return []


def show(message):
    if message["type"] == "image":
        return f"[image] {message['originalContentUrl']}"
    if message["type"] == "flex":
        return f"[flex] {message['altText']}\n" + '\n'.join(flex_texts(message['contents']))
    return message["text"]


class FakeTransport:
    def __init__(self, friends=()):
        self.replies, self.pushes, self.friends, self.raw, self.raw_pushes = [], [], set(friends), [], []

    def reply(self, token, messages):
        self.raw.append(messages)
        self.replies.append((token, [show(m) for m in messages]))

    def push(self, to, messages):
        if to.startswith("U") and to not in self.friends:
            raise RuntimeError("LINE API 400: not a friend")
        self.raw_pushes.append((to, messages))
        self.pushes.append((to, [show(m) for m in messages]))

    def content(self, message_id):
        return b"jpeg"

    def loading(self, chat_id, seconds):
        self.loadings = getattr(self, "loadings", []) + [chat_id]

    def display_name(self, user_id, chat_id):
        return {"U1": "明明", "U2": "華華"}.get(user_id)


def text_event(user, value, group=None):
    source = {"type": "group", "groupId": group, "userId": user} if group else {"type": "user", "userId": user}
    return {"type": "message", "replyToken": f"t-{time.time_ns()}", "source": source,
            "message": {"type": "text", "id": "m", "text": value}}


def image_event(user, group=None):
    event = text_event(user, "", group)
    event["message"] = {"type": "image", "id": "img-1"}
    return event


def postback_event(user, data):
    return {"type": "postback", "replyToken": f"t-{time.time_ns()}", "source": {"type": "user", "userId": user},
            "postback": {"data": data}}


class BotCase(unittest.TestCase):
    """A bot with fakes around it.

    Automatic notifications are off unless ``Settings.notify`` says otherwise (every push counts against the
    LINE quota). Most tests here are about those notifications, so the case turns them on;
    ``QuietByDefaultTests`` and any class with ``settings = {"notify": False}`` run the way production does.
    """
    friends = {"U1", "U2", "U3"}
    settings = {}

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "bot.sqlite3"
        self.transport = FakeTransport(friends=self.friends)
        self.analyzer = FakeAnalyzer(copy.deepcopy(RESULT))
        self.store = Store(self.path)
        self.bot = RosterBot(self.analyzer, self.store, self.transport, Settings(**{"notify": True, **self.settings}))

    def tearDown(self):
        self.folder.cleanup()

    def ask(self, value, user="U1"):
        self.bot.handle(text_event(user, value))
        return self.reply()

    def reply(self):
        return "\n".join(self.transport.replies[-1][1])

    def register(self, user, name, notify=False):
        self.bot.handle(text_event(user, f"設定 {name}"))
        if notify:
            self.bot.handle(text_event(user, "通知 開"))

    @staticmethod
    def at(when):
        """Taiwan time is ``when`` (a datetime, or a date at noon) inside the block: 「我的班表」 reads the clock."""
        if not isinstance(when, datetime):
            when = datetime(when.year, when.month, when.day, 12, tzinfo=TAIPEI)
        return patch("all_staff.bot.taipei_now", return_value=when)


class BotTests(BotCase):
    def test_parse_code_accepts_full_width_and_hash(self):
        self.assertEqual(parse_code("７２"), "72")
        self.assertEqual(parse_code("#07"), "7")
        self.assertEqual(parse_code("k"), "K")
        self.assertEqual(parse_code("甲"), "甲")
        self.assertIsNone(parse_code("99")); self.assertIsNone(parse_code("O"))

    def test_register_name_before_any_roster(self):
        self.assertIn("已登記：林小明", self.ask("設定 林小明"))
        self.assertIn("還沒有班表", self.reply())
        self.assertIn("還沒有班表", self.ask("我的班表"))

    def test_unregistered_user_is_asked_for_a_name(self):
        self.assertIn("設定 你的姓名", self.ask("我的班表", "U3"))

    def test_codes_are_not_accepted_as_a_binding(self):
        reply = self.ask("設定 72")
        self.assertIn("請改用姓名登記", reply)
        self.assertIn("我是 72", reply)
        self.assertIsNone((self.store.user("U1") or {}).get("staff_name"))
        self.assertIn("2–5 個中文字", self.ask("設定 abc"))

    def test_todays_code_is_found_through_the_name_table(self):
        self.bot.handle(image_event("U2", "Cgroup"))
        with self.at(datetime(2026, 10, 1, 5, 30, tzinfo=TAIPEI)):
            reply = self.ask("設定 林小明")                       # answered with the 我的班表 card
        self.assertIn("☀️ 今天 10/1（四）\n代號 72・共 3 小時", reply)
        self.assertIn("05–07\n國際線", reply)
        with self.at(date(2026, 10, 1)):
            self.assertIn("代號 72", self.ask("我的班表"))
        self.assertIn("林小明（72）　10/1（四）", self.ask("10/1"))

    def test_next_day_the_same_name_has_another_code(self):
        self.register("U1", "林小明")
        self.bot.handle(image_event("U2", "Cgroup"))
        nextday = copy.deepcopy(RESULT)
        nextday["run"] = {"id": "run-2", "codes": 5, "roster_date": "2026-10-02"}
        nextday["names"] = {"甲": ["林小明"], "72": ["陳小華"]}
        self.analyzer.data = nextday
        self.bot.handle(image_event("U2", "Cgroup"))
        self.assertIn("林小明（甲）　10/2（五）", self.ask("我的班表 10/2"))
        self.assertIn("09–10　國際備勤", self.reply())
        with self.at(date(2026, 10, 2)):
            reply = self.ask("我的班表")
        self.assertIn("☀️ 今天 10/2（五）\n代號 甲", reply)
        self.assertIn("09–10\n國際備勤", reply)

    def test_group_upload_is_silent_and_notifications_default_off(self):
        self.register("U1", "林小明")
        self.transport.replies.clear(); self.transport.pushes.clear()
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertEqual(self.analyzer.calls, 1)
        self.assertEqual(self.transport.replies, [])
        self.assertEqual(self.transport.pushes, [])
        self.assertFalse(getattr(self.transport, "loadings", []))

    def test_subscribers_get_their_own_duties_by_name(self):
        self.register("U1", "林小明", notify=True)
        self.register("U2", "陳小華", notify=True)
        self.transport.pushes.clear(); self.transport.replies.clear()
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertEqual([p[0] for p in self.transport.pushes], ["U1", "U2"])
        self.assertIn("📌 新班表｜林小明（72）", self.transport.pushes[0][1][0])
        self.assertIn("共 3 小時", self.transport.pushes[0][1][0])
        self.assertIn("國際備勤", self.transport.pushes[1][1][0])
        self.assertEqual(self.transport.replies, [])

    def test_subscriber_missing_from_the_name_table_is_told_how_to_claim(self):
        self.register("U3", "張小美", notify=True)
        self.transport.pushes.clear()
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertIn("沒有找到「張小美」", self.transport.pushes[0][1][0])
        self.assertIn("我是 72", self.transport.pushes[0][1][0])

    def test_claiming_a_code_fixes_that_roster(self):
        self.bot.handle(image_event("U1", "Cgroup"))
        with self.at(date(2026, 10, 1)):
            self.register("U3", "張小美")
        self.assertIn("今天的班表上找不到你", self.reply())                 # the card says so for the day
        self.assertIn("✏️ 有上班就傳「我是 代號 10/1」", self.reply())
        reply = self.ask("我是 26", "U3")
        self.assertIn("你是代號 26", reply)
        self.assertIn("張小美（26）", reply)
        with self.at(date(2026, 10, 1)):
            self.assertIn("代號 26", self.ask("我的班表", "U3"))
        self.assertIn("26 張小美", self.ask("05-06 國際"))          # the claim replaces the OCR name
        self.assertNotIn("王大同", self.reply())

    def test_claim_needs_a_name_first(self):
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertIn("設定 你的姓名", self.ask("我是 26", "U3"))

    def test_legacy_code_bindings_are_asked_once_to_register_a_name(self):
        self.store.bind("U2", "72", "華華"); self.store.set_notify("U2", True)
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertEqual([p[0] for p in self.transport.pushes], ["U2"])
        self.assertIn("改用姓名登記", self.transport.pushes[0][1][0])
        self.transport.pushes.clear()
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertEqual(self.transport.pushes, [])

    def test_failed_delivery_does_not_prevent_other_subscribers(self):
        self.transport.friends.discard("U1")
        self.register("U1", "林小明", notify=True)
        self.register("U2", "陳小華", notify=True)
        self.transport.pushes.clear()
        self.bot.handle(image_event("U2", "Cgroup"))
        self.assertEqual([p[0] for p in self.transport.pushes], ["U2"])

    def test_slow_recognition_falls_back_to_push(self):
        self.bot.settings.reply_window = 0
        self.bot.handle(image_event("U2"))
        self.assertEqual(self.transport.pushes[0][0], "U2")
        self.assertEqual(self.transport.loadings[0], "U2")

    def test_any_group_photo_becomes_the_team_roster(self):
        self.bot.handle(image_event("U1", "Cbig"))
        self.assertEqual(self.analyzer.calls, 1)
        self.assertEqual(self.store.team_roster()["chat_id"], "Cbig")

    def test_query_by_code_or_name(self):
        self.register("U2", "陳小華")
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertIn("陳小華（甲）", self.ask("查 甲"))
        self.assertIn("陳小華（甲）", self.ask("查 陳小華"))
        self.assertIn("代號 5", self.ask("查 5"))                 # unreadable name: code only
        self.assertIn("沒有找到「李四四」", self.ask("查 李四四"))

    def test_group_small_talk_is_ignored(self):
        self.bot.handle(text_event("U1", "今天好熱", "Cgroup"))
        self.assertEqual(self.transport.replies, [])

    def test_notify_and_unregister(self):
        self.assertIn("請先輸入「設定 你的姓名」", self.ask("通知 開"))
        self.register("U2", "陳小華", notify=True)
        self.assertIn("不再私訊", self.ask("通知 關", "U2"))
        self.transport.pushes.clear()
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertEqual(self.transport.pushes, [])
        self.ask("解除", "U2")
        self.assertIn("設定 你的姓名", self.ask("我的班表", "U2"))

    def test_format_merges_hours_and_marks_low_confidence(self):
        roster = {"created_at": time.time(), "roster_date": "2026-10-01"}
        message = format_duties("72", "林小明", roster, RESULT)
        self.assertIn("林小明（72）　10/1（四）", message)
        self.assertIn("共 3 小時｜1 段待確認", message)
        self.assertIn("05–07　國際線", message)
        self.assertIn("07–08　國內一線 ？", message)
        self.assertIn("沒有辨識到", format_duties("8", None, roster, RESULT))


class StaffDirectoryTests(BotCase):
    def setUp(self):
        super().setUp()
        directory = Path(self.folder.name) / "staff.txt"
        directory.write_text("# 名單\n林小明\n陳小華  # 備註\n王大同\n", encoding="utf-8")
        self.bot.settings.staff_names = directory
        self.store.add_officer("U1", "隊長")   # nobody registers: the 名冊守門員 lets a 幹部 in all the same

    def test_directory_names_show_without_anyone_registering(self):
        self.bot.handle(image_event("U1", "Cgroup"))
        listing = self.ask("05-06 國際")
        self.assertIn("國際線\n2 人\n72 林小明\n26 王大同", listing)   # the duty pill, then its people

    def test_uncertain_readings_are_not_guessed(self):
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertNotIn("某某某", self.ask("05-06 國內"))
        self.assertIn("國內二線\n1 人\n5", self.reply())


class FixedStaffCodeTests(BotCase):
    def setUp(self):
        import json
        super().setUp()
        directory = Path(self.folder.name) / "staff_names.txt"
        directory.write_text("蕭志明\n彭志明\n陳志明\n林小明\n", encoding="utf-8")
        fixed = Path(self.folder.name) / "fixed_staff_codes.json"
        fixed.write_text(json.dumps({"己": "蕭志明", "乙": "彭志明", "甲": "陳志明"}), encoding="utf-8")
        self.bot.settings.staff_names = directory
        self.bot.settings.fixed_staff_codes = fixed
        self.analyzer.data["names"] = {"乙": ["蕭志明"], "72": ["蕭志明"]}
        self.analyzer.data["people"].append({"code": "己", "slots": [slot(4, 9, "國際線備勤")]})

    def test_registration_and_personal_query_use_fixed_code_with_wrong_ocr(self):
        self.bot.handle(image_event("U1", "Cgroup"))
        with self.at(date(2026, 10, 1)):
            self.assertIn("☀️ 今天 10/1（四）\n代號 己", self.ask("設定 蕭志明"))
        self.assertIn("蕭志明（己）", self.ask("10/1"))
        self.assertIn("國際備勤", self.reply())
        self.assertIn("彭志明（乙）", self.ask("查 彭志明"))

    def test_slot_list_labels_fixed_codes_without_ocr(self):
        self.analyzer.data["names"] = {}
        self.store.set_staff_name("U1", "林小明")   # a colleague on the 名冊, whom the 名冊守門員 lets in
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertIn("己 蕭志明", self.ask("05-06 國際"))

    def test_private_notification_uses_fixed_code_without_ocr(self):
        self.analyzer.data["names"] = {}
        self.register("U1", "蕭志明", notify=True)
        self.bot.handle(image_event("U2", "Cgroup"))
        self.assertEqual(self.transport.pushes[-1][0], "U1")
        self.assertIn("蕭志明（己）", "\n".join(self.transport.pushes[-1][1]))

    def test_old_claim_cannot_move_a_fixed_name_to_a_numeric_code(self):
        self.register("U1", "蕭志明")
        self.bot.handle(image_event("U1", "Cgroup"))
        self.store.claim("run-1", "U1", "72")
        names = self.bot.names_for(self.store.team_roster(), self.analyzer.data)
        self.assertEqual(names["己"], "蕭志明")
        self.assertNotIn("蕭志明", [n for c, n in names.items() if c != "己"])
        self.assertEqual(self.store.claims("run-1"), {"72": "蕭志明"})  # keep the old record for audit

    def test_new_claim_cannot_change_a_fixed_name_or_take_someones_fixed_code(self):
        self.register("U1", "蕭志明")
        self.register("U2", "林小明")
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertIn("固定代號是「己」", self.ask("我是 72"))
        self.assertIn("代號 己 是「蕭志明」", self.ask("我是 己", "U2"))
        self.assertEqual(self.store.claims("run-1"), {})
        self.assertIn("你是代號 己", self.ask("我是 己"))
        self.assertEqual(self.store.claims("run-1"), {"己": "蕭志明"})


class SlotTests(BotCase):
    def setUp(self):
        super().setUp()
        self.register("U1", "林小明"); self.register("U2", "陳小華")
        self.bot.handle(image_event("U1", "Cgroup"))

    def test_listing_groups_by_duty_and_ignores_a1(self):
        roster = self.store.team_roster()
        names = self.bot.names_for(roster, RESULT)
        intl = who_is_on(RESULT, 4, "intl", names, roster)
        self.assertTrue(intl.startswith("10/1（四）班表\n05–06 國際"))
        self.assertIn("國際線：72 林小明、26", intl)
        self.assertNotIn("A1", intl)
        self.assertIn("共 2 人", intl)
        dom = who_is_on(RESULT, 6, "dom", names, roster)
        self.assertIn("國內一線：72 林小明 ？", dom)
        self.assertIn("沒有辨識到人員", who_is_on(RESULT, 20, "dom", names, roster))

    def test_typed_slot_queries(self):
        self.assertIn("05–06 國際", self.ask("05-06 國際"))
        self.assertIn("05–06 國內", self.ask("5 國內"))
        self.assertIn("國內二線\n1 人\n5", self.reply())
        for command in ("查看時段 7-8 國內線", "同時段 7-8 國內線"):
            self.assertIn("07–08 國內", self.ask(command))
        self.assertIn("一個小時", self.ask("10-12 國際"))
        self.assertIn("沒有 9/1（二）的班表", self.ask("10-11 國際 9/1"))

    def test_card_separates_duties_and_people_without_losing_confidence_or_names(self):
        from all_staff.bot import slot_card
        result = copy.deepcopy(RESULT)
        result["people"][0]["slots"].append(slot(4, 9, "國際線備勤", .5))
        card = slot_card(result, 4, "intl", {"72": "林小明"}, self.store.team_roster(), has_image=True)
        self.assertIn("共 2 人", card["altText"])  # one person on two duties still counts once
        self.assertIn("國際線：72 林小明、26｜國際備勤：72 林小明 ？", card["altText"])
        header = flex_texts(card["contents"]["header"])
        self.assertEqual(header[:2], ["✈️ 05–06 國際線", "10/1（四）班表"])
        self.assertIn("👥 共 2 人", header)
        groups = [n for n in card["contents"]["body"]["contents"] if n.get("layout") == "horizontal"]
        pills = [g["contents"][0]["contents"][0]["contents"] for g in groups]   # the duty pill: its name, its head count
        self.assertEqual([[t["text"] for t in pill] for pill in pills], [["國際線", "2 人"], ["國際備勤", "1 人"]])
        self.assertTrue(all(pill[0]["weight"] == "bold" for pill in pills))
        self.assertEqual([n["text"] for n in groups[0]["contents"][1]["contents"]], ["72 林小明", "26"])
        self.assertEqual([n["text"] for n in groups[1]["contents"][1]["contents"]], ["72 林小明 ？"])
        code, name = groups[0]["contents"][1]["contents"][0]["contents"]   # the code in bold, then the name
        self.assertEqual((code["text"], code["weight"], name["text"]), ("72", "bold", " 林小明"))
        visible = "\n".join(flex_texts(card["contents"]))
        self.assertNotIn("A1", visible)
        for phrase in ("勤務待確認", "姓名未辨識", "本時段原圖"):
            self.assertIn(phrase, visible)

    def test_empty_slot_card_still_shows_date_and_hour(self):
        from all_staff.bot import slot_card
        card = slot_card(RESULT, 20, "dom", {}, self.store.team_roster())
        self.assertIn("10/1（四）班表｜21–22 國內｜共 0 人", card["altText"])
        self.assertIn("這個時段沒有辨識到人員。", flex_texts(card["contents"]))

    def test_menu_flow_picker_then_area_then_listing(self):
        self.ask("查看時段")
        flex = self.transport.raw[-1][0]
        self.assertEqual(flex["type"], "flex")
        self.assertEqual(flex["contents"]["type"], "bubble")   # one card for the whole day, nothing to swipe
        chips = hour_chips(flex["contents"])
        hours = [next(t for t in flex_texts(chip) if "–" in t) for chip in chips]
        every_hour = [f"{hour:02d}–{hour + 1:02d}" for hour in range(5, 24)]
        self.assertEqual(hours, every_hour * 2)   # 國際, then 國內: empty hours too, so each keeps its place
        self.assertEqual([chip["action"]["data"] for chip in (chips[0], chips[18], chips[19], chips[-1])],
                         ["area|run-1|4|intl", "area|run-1|22|intl", "area|run-1|4|dom", "area|run-1|22|dom"])
        self.assertEqual(chips[19]["action"]["displayText"], "05–06 國內")
        self.assertEqual(flex_texts(flex["contents"])[:2], ["🕐 查看時段", "點一格，看那個小時誰在哪裡"])
        self.assertTrue({"✈️ 國際線", "🏠 國內線"} <= set(flex_texts(flex["contents"])))
        self.assertNotIn("footer", flex["contents"])
        self.bot.handle(postback_event("U1", chips[19]["action"]["data"]))
        answer = self.transport.raw[-1][0]
        self.assertEqual(answer["type"], "flex")
        self.assertIn("05–06 國內", answer["altText"])
        self.assertEqual(answer["quickReply"]["items"][0]["action"]["label"], "改看國際")

    def test_picker_counts_people_and_shades_busier_hours_darker(self):
        from all_staff.bot import SLOT_UI, TAIPEI, slot_counts, slot_picker
        card = slot_picker(self.store.team_roster(), RESULT, now=datetime(2026, 9, 30, 10, tzinfo=TAIPEI))
        chips = hour_chips(card["contents"])
        intl, dom = chips[:19], chips[19:]
        # 05–06 國際: 72 and 26 (26's A1 is not 國際); 06–07: 72; 09–10: 甲 on 國際備勤
        self.assertEqual([flex_texts(chip)[-1] for chip in intl[:6]], ["2 人", "1 人", "0 人", "0 人", "1 人", "0 人"])
        self.assertEqual([flex_texts(chip)[-1] for chip in dom[:3]], ["1 人", "0 人", "1 人"])
        self.assertEqual(intl[0]["backgroundColor"], SLOT_UI["intl"][-1])   # the area's busiest hour: darkest
        self.assertEqual(intl[1]["backgroundColor"], SLOT_UI["intl"][1])    # half as many: two shades lighter
        self.assertEqual(dom[0]["backgroundColor"], SLOT_UI["dom"][-1])     # each area has its own hue and scale
        self.assertEqual((intl[2]["backgroundColor"], intl[2]["borderWidth"]), ("#FFFFFF", "1px"))   # nobody
        self.assertEqual(flex_texts(card["contents"]["body"])[1], "最多 2 人・05–06")
        self.assertIn("國際最多 2 人（05–06）｜國內最多 1 人（05–06）", card["altText"])
        self.assertIn("🎨 顏色越深，那個小時人越多", flex_texts(card["contents"]))
        self.assertNotIn("勤務人員對照", flex_texts(card["contents"]))
        result = copy.deepcopy(RESULT)
        result["people"][0]["slots"].append(slot(4, 9, "國際線備勤"))
        self.assertEqual(slot_counts(result, "intl")[4], 2)   # one person on two duties counts once

    def test_picker_rings_the_current_hour_only_on_todays_roster(self):
        from all_staff.bot import SLOT_UI, TAIPEI, slot_picker
        roster = self.store.team_roster()   # 10/1
        card = slot_picker(roster, RESULT, now=datetime(2026, 10, 1, 10, 25, tzinfo=TAIPEI))
        chips = hour_chips(card["contents"])
        ringed = [chip for chip in chips if chip.get("borderColor") == SLOT_UI["now"]]
        self.assertEqual([chip["action"]["displayText"] for chip in ringed], ["10–11 國際", "10–11 國內"])
        self.assertTrue(all(flex_texts(chip)[0] == "現在" for chip in ringed))
        self.assertTrue({"📅 10/1（四）・今天", "⏰ 現在 10–11"} <= set(flex_texts(card["contents"]["header"])))
        self.assertIn("現在 10–11：國際 0 人、國內 0 人", card["altText"])
        over, later = chips[0], chips[8]   # 05–06 has ended; 13–14 is still to come
        self.assertEqual(over["backgroundColor"], SLOT_UI["intl"][-1] + SLOT_UI["fade"])
        self.assertEqual(over["contents"][0]["color"], SLOT_UI["past"])
        self.assertNotEqual(later["contents"][0]["color"], SLOT_UI["past"])
        self.assertIn("灰字＝已經過了", flex_texts(card["contents"]))
        last = slot_picker(roster, RESULT, now=datetime(2026, 10, 1, 23, 30, tzinfo=TAIPEI))
        self.assertEqual([chip["action"]["displayText"] for chip in hour_chips(last["contents"])
                          if chip.get("borderColor") == SLOT_UI["now"]], ["23–24 國際", "23–24 國內"])
        for now in (datetime(2026, 10, 2, 10, tzinfo=TAIPEI), datetime(2026, 9, 30, 10, tzinfo=TAIPEI),
                    datetime(2026, 10, 1, 3, tzinfo=TAIPEI)):   # another day, or today before 05:00
            with self.subTest(now=now):
                card = slot_picker(roster, RESULT, now=now)
                self.assertFalse(any(chip.get("borderColor") == SLOT_UI["now"]
                                     for chip in hour_chips(card["contents"])))
                self.assertNotIn("現在", "".join(flex_texts(card["contents"])) + card["altText"])
                self.assertNotIn("灰字＝已經過了", flex_texts(card["contents"]))
        self.assertIn("📅 10/1（四）・明天", flex_texts(slot_picker(
            roster, RESULT, now=datetime(2026, 9, 30, 22, tzinfo=TAIPEI))["contents"]["header"]))

    def test_picker_fits_line_limits_on_a_busy_day(self):
        import json
        from all_staff.bot import TAIPEI, slot_card, slot_picker
        columns = [*range(2, 17), *range(17, 31)]   # every 國際 and 國內 column, every hour
        people = [{"code": str(n), "slots": [slot(row, column, "國際線" if column < 17 else "國內線一線")
                                             for row in range(4, 23)]} for n, column in enumerate(columns, 1)]
        result = {"people": people}
        roster = {"run_id": "20261008-091512-a1b2c3", "roster_date": "2026-10-08"}
        picker = slot_picker(roster, result, now=datetime(2026, 10, 8, 14, 5, tzinfo=TAIPEI))
        names = {str(n): "歐陽小明" for n in range(1, 30)}
        listing = slot_card(result, 10, "intl", names, roster, has_image=True)

        def nodes(node):
            if isinstance(node, list):
                return [found for child in node for found in nodes(child)]
            if isinstance(node, dict):
                return [node] + [found for child in node.values() for found in nodes(child)]
            return []

        for card in (picker, listing):
            size = len(json.dumps(card["contents"], ensure_ascii=False, separators=(",", ":")).encode())
            self.assertLess(size, 25_000)
            self.assertLessEqual(len(card["altText"]), 400)
            self.assertTrue(all(node["text"] for node in nodes(card) if node.get("type") in ("text", "span")))
        actions = [node["action"] for node in nodes(picker) if "action" in node]
        self.assertEqual(len(actions), 38)
        self.assertTrue(all(len(a["data"]) <= 300 and len(a["label"]) <= 20 for a in actions))
        self.assertIn("15 人", flex_texts(hour_chips(picker["contents"])[0]))

    def test_picker_counts_the_same_duties_as_the_listing(self):
        rechecked = copy.deepcopy(RESULT)   # an old roster's codes read again (ensure_duties) before counting
        rechecked["people"].append({"code": "9", "slots": [slot(4, 5, "國際線")]})
        self.analyzer.ensure_duties = lambda run_id: rechecked
        self.ask("查看時段")
        self.assertEqual(flex_texts(hour_chips(self.transport.raw[-1][0]["contents"])[0])[-1], "3 人")

    def test_older_hour_button_still_offers_area_choice(self):
        self.bot.handle(postback_event("U1", "slot|run-1|4"))
        question = self.transport.raw[-1][0]
        self.assertIn("要看國際還是國內", question["text"])
        actions = [item["action"] for item in question["quickReply"]["items"]]
        self.assertEqual([action["label"] for action in actions], ["國際", "國內"])

    def test_stale_or_fill_postbacks_are_ignored(self):
        count = len(self.transport.replies)
        for data in ("fill:設定", "slot|missing|4", "area|run-1|x|intl", "nonsense"):
            self.bot.handle(postback_event("U1", data))
        self.assertEqual(len(self.transport.replies), count)

    def test_change_hour_keeps_the_selected_roster_date(self):
        self.ask("5 國際 10/1")
        action = self.transport.raw[-1][-1]["quickReply"]["items"][1]["action"]
        newer = copy.deepcopy(RESULT)
        newer["run"]["id"] = "newer"
        newer["run"]["roster_date"] = "2026-10-02"
        self.store.add_roster("Cgroup", "newer", "U1", 5, "2026-10-02")
        self.bot.handle(postback_event("U1", action["data"]))
        picker = self.transport.raw[-1][0]
        self.assertIn("10/1（四）", picker["altText"])


class NameMatchingTests(unittest.TestCase):
    def test_repeated_name_confusion_requires_two_exact_directory_votes(self):
        from all_staff.name_table import match_names
        self.assertEqual(match_names({'35': ['沈姻姻', '沈姻姻', '沈媽刀']}, ['朱志明']), {'35': '朱志明'})
        self.assertEqual(match_names({'35': ['沈姻姻']}, ['朱志明']), {})
        self.assertEqual(match_names({'35': ['沈姻姻', '沈姻姻']}, ['朱志明', '沈姻姻']), {'35': '沈姻姻'})

    def test_name_confusion_never_overrides_strict_match_or_duplicate_owner(self):
        from all_staff.name_table import match_names
        self.assertEqual(match_names({'35': ['沈姻姻', '沈姻姻', '王小明']}, ['朱志明', '王小明']),
                         {'35': '王小明'})
        self.assertEqual(match_names({'35': ['沈姻姻', '沈姻姻'], '34': ['朱志明']}, ['朱志明']), {})

    def test_fixed_codes_survive_missing_and_misaligned_ocr(self):
        from all_staff.name_table import match_names
        fixed = {"己": "蕭志明", "乙": "彭志明"}
        directory = [*fixed.values(), "賴志明"]
        self.assertEqual(match_names({}, directory, fixed_codes=fixed), fixed)
        self.assertEqual(match_names(
            {"己": ["张遠仁"], "乙": ["蕭志明"], "72": ["蕭志明"], "1": ["賴志明"]},
            directory, fixed_codes=fixed), {"1": "賴志明", **fixed})

    def test_numeric_and_english_names_rotate_independently_of_fixed_codes(self):
        from all_staff.name_table import match_names
        fixed = {"己": "蕭志明"}
        directory = ["蕭志明", "賴志明", "王雅婷"]
        self.assertEqual(match_names({"1": ["賴志明"], "A": ["王雅婷"]}, directory, fixed_codes=fixed),
                         {"1": "賴志明", "A": "王雅婷", **fixed})
        self.assertEqual(match_names({"A": ["賴志明"], "1": ["王雅婷"]}, directory, fixed_codes=fixed),
                         {"A": "賴志明", "1": "王雅婷", **fixed})

    def test_fixed_code_directory_validation(self):
        import json
        from all_staff.name_table import load_fixed_codes
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixed.json"
            self.assertEqual(load_fixed_codes(path), {})
            path.write_text(json.dumps({"己": "蕭志明"}), encoding="utf-8")
            self.assertEqual(load_fixed_codes(path), {"己": "蕭志明"})
            for bad in [{"72": "蕭志明"}, {"己": "U1"}, {"己": "蕭志明", "乙": "蕭志明"}, []]:
                path.write_text(json.dumps(bad), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_fixed_codes(path)

    def test_official_directory_corrects_previous_name_spellings(self):
        from all_staff.name_table import load_directory, match_names
        directory = load_directory(Path(__file__).resolve().parent / "configs/staff_names.txt")
        self.assertEqual(match_names({"1": ["王璟濱"], "2": ["施芳榆"], "3": ["鍾文君"]}, directory),
                         {"1": "胡志明", "2": "葉志明", "3": "謝雅婷"})

    def test_variants_merge_and_disagreements_are_dropped(self):
        from all_staff.name_table import match_names
        names = ["蕭志明", "羅志明", "蘇雅婷", "陳志明"]
        readings = {"己": ["林子軸", "蕭志明"], "32": ["林承汗", ""], "27": ["陳美公", "何翊廢"], "甲": ["陳志明"]}
        self.assertEqual(match_names(readings, names), {"己": "蕭志明", "32": "羅志明", "甲": "陳志明"})

    def test_ambiguous_and_duplicate_readings(self):
        from all_staff.name_table import match_names
        self.assertEqual(match_names({"1": "王小"}, ["王小明", "王小華"]), {})        # no clear winner
        self.assertEqual(match_names({"1": "王小明", "2": "王小明"}, ["王小明"]), {})  # one name, two codes
        self.assertEqual(match_names({"1": "王小明"}, []), {})

    def test_directory_file(self):
        from all_staff.name_table import load_directory
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "names.txt"
            path.write_text("# 說明\n蕭志明\n\n 蘇雅婷 # 註\n", encoding="utf-8")
            self.assertEqual(load_directory(path), ["蕭志明", "蘇雅婷"])
        self.assertEqual(load_directory(None), [])

    def test_code_layout_covers_the_printed_table(self):
        from all_staff.name_table import COLUMNS, ROWS
        from all_staff.schema import ALLOWED_CODE_SET
        codes = [c for column in COLUMNS for c in column]
        self.assertTrue(all(len(column) == ROWS for column in COLUMNS))
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(set(codes) <= ALLOWED_CODE_SET)


class NameCellRecoveryTests(unittest.TestCase):
    def test_recovery_preserves_known_names_and_skips_empty_ruled_cells(self):
        from PIL import Image, ImageDraw
        from unittest import mock
        from all_staff.name_table import read_name_table
        page = Image.new("L", (400, 70), 255)
        draw = ImageDraw.Draw(page)
        boxes = {"1": (0, 0, 120, 50), "2": (130, 0, 250, 50), "3": (260, 0, 380, 50)}
        draw.rectangle((10, 15, 100, 35), fill=0)
        draw.rectangle((145, 15, 235, 35), fill=0)
        draw.rectangle(boxes["3"], outline=0, width=3)
        with mock.patch("all_staff.name_table.shutil.which", return_value="tesseract"), \
             mock.patch("all_staff.name_table.warp_page", return_value=page), \
             mock.patch("all_staff.name_table.locate", return_value=(boxes, [])), \
             mock.patch("all_staff.name_table.read_names", return_value={"1": "林小明"}), \
             mock.patch("all_staff.name_table._read_name_cell", return_value="陳小華") as single:
            raw = read_name_table(Path("photo.jpg"), None, directory=["林小明", "陳小華"])
        self.assertEqual(raw["1"], ["林小明"] * 4)
        self.assertEqual(raw["2"][-4:], ["陳小華"] * 4)
        self.assertNotIn("3", raw)
        self.assertEqual(single.call_count, 4)
        self.assertTrue(all(call.args[1] == boxes["2"] for call in single.call_args_list))

    def test_second_framing_can_read_small_text_after_full_cell_readings_fail(self):
        from PIL import Image, ImageDraw
        from unittest import mock
        from all_staff.name_table import match_names, read_name_table
        page = Image.new("L", (120, 50), 255)
        ImageDraw.Draw(page).rectangle((20, 15, 100, 35), fill=0)
        boxes = {"1": (0, 0, 120, 50)}
        def batch(*args, **kwargs):
            return {} if kwargs["boxes"] == boxes else {"1": "林小明"}
        with mock.patch("all_staff.name_table.shutil.which", return_value="tesseract"), \
             mock.patch("all_staff.name_table.warp_page", return_value=page), \
             mock.patch("all_staff.name_table.locate", return_value=(boxes, [])), \
             mock.patch("all_staff.name_table.read_names", side_effect=batch), \
             mock.patch("all_staff.name_table._read_name_cell", return_value=""):
            raw = read_name_table(Path("photo.jpg"), None, directory=["林小明"])
        self.assertEqual(match_names(raw, ["林小明"]), {"1": "林小明"})

    def test_a_table_that_cannot_be_aligned_says_so_in_the_log_and_yields_no_names(self):
        import contextlib
        import io
        from PIL import Image
        from unittest import mock
        from all_staff.name_table import read_name_table
        page = Image.new("L", (400, 70), 255)
        report = [{"column": 0, "first": "1", "ok": False, "hits": 1, "runner_up": 1, "support": 3, "rival": 2}]
        for located, shown in ((({}, report), "整張表對到 3／次佳 2"), (({}, []), "整張表對到 0／次佳 0")):
            out = io.StringIO()
            with mock.patch("all_staff.name_table.shutil.which", return_value="tesseract"), \
                 mock.patch("all_staff.name_table.warp_page", return_value=page), \
                 mock.patch("all_staff.name_table.locate", return_value=located), \
                 contextlib.redirect_stdout(out):
                raw = read_name_table(Path("photo.jpg"), None, directory=["林小明"])
            self.assertEqual(raw, {})
            self.assertIn(shown, out.getvalue())
            self.assertNotIn("失敗", out.getvalue())

    def test_one_cell_timeout_does_not_abort_other_name_readings(self):
        import subprocess
        from PIL import Image, ImageDraw
        from unittest import mock
        from all_staff.name_table import _read_name_cell
        page = Image.new("L", (120, 50), 255)
        ImageDraw.Draw(page).rectangle((20, 15, 100, 35), fill=0)
        with mock.patch("all_staff.name_table._tesseract_tsv", side_effect=subprocess.TimeoutExpired("tesseract", 10)):
            self.assertEqual(_read_name_cell(page, (0, 0, 120, 50), 64, False), "")


class NameTableLocationTests(unittest.TestCase):
    def test_oct3_table_recovers_the_reported_missing_daily_names(self):
        import shutil
        from PIL import Image
        from unittest import mock
        from all_staff.name_table import load_directory, load_fixed_codes, match_names, read_name_table
        root = Path(__file__).resolve().parent
        fixture = root / "assets/test_fixtures/name-table-2026-10-03.png"
        if not shutil.which("tesseract"):
            self.skipTest("tesseract not available")
        with Image.open(fixture) as image:
            page = image.copy()
        with mock.patch("all_staff.name_table.warp_page", return_value=page):
            raw = read_name_table(fixture, None)
        names = match_names(raw, load_directory(root / "configs/staff_names.txt"),
                            fixed_codes=load_fixed_codes(root / "configs/fixed_staff_codes.json"))
        expected = {"1": "楊志明", "2": "游志明", "4": "王志明", "5": "鄭志明", "10": "江雅婷",
                    "14": "徐雅婷", "17": "蔡志明", "20": "吳志明", "28": "何雅婷", "G": "高志明"}
        self.assertEqual({code: names.get(code) for code in expected}, expected)

    def test_code_cell_readings(self):
        from all_staff.name_table import _code_matches
        self.assertTrue(_code_matches("|l11", "11"))
        self.assertTrue(_code_matches("110", "10"))          # a cell rule read as an extra 1
        self.assertFalse(_code_matches("11", "1"))           # …but never for one-digit codes
        self.assertTrue(_code_matches("|丙", "丙"))
        self.assertTrue(_code_matches("BR", "B"))
        self.assertFalse(_code_matches("8", "18"))

    def test_a_run_of_faint_rules_does_not_cost_the_column_its_rows(self):
        """Glare hid 5–7 rules in a row on the 9/12 and 9/24 photos; the whole column of 14 names was dropped."""
        import numpy as np
        from all_staff.name_table import _row_candidates
        pitch, width = 50, 200
        ink = np.zeros((1100, width), np.float32)
        for n in range(17):
            if not 5 <= n <= 10:                         # six rules in a row are not detected
                ink[100 + n * pitch:102 + n * pitch, :] = 1
        rows = _row_candidates(ink, 0, width, 50, 1000, pitch)
        mids = [(a + b) / 2 for a, b in rows]
        self.assertGreaterEqual(len(mids), 16)
        self.assertTrue(all(abs((b - a) - pitch) < 6 for a, b in zip(mids, mids[1:])), mids)   # no hole

    def test_a_faint_stroke_inside_a_row_does_not_cut_the_row_in_two(self):
        """10/2 update: a faint line half-way down some rows cut each into two halves, both too short to be a
        row, so the row itself vanished and every code below it landed one row off."""
        import numpy as np
        from all_staff.name_table import _row_candidates
        pitch, width = 50, 200
        ink = np.zeros((1100, width), np.float32)
        for n in range(17):
            ink[100 + n * pitch:102 + n * pitch, :] = 1                  # the printed rules
        for n in (4, 5, 9, 12):
            ink[124 + n * pitch:126 + n * pitch, 20:140] = 1             # a dash over 60 % of the cell, mid-row
        rows = _row_candidates(ink, 0, width, 50, 1000, pitch)
        tops = [a for a, _ in rows]
        self.assertGreaterEqual(len(rows), 16)
        self.assertTrue(all(abs((b - a) - pitch) < 6 for a, b in rows[:16]), rows[:16])      # every row whole
        self.assertTrue(all(abs(tops[n] - (100.5 + n * pitch)) < 4 for n in range(16)), tops)

    def test_faint_printed_rules_are_still_rules(self):
        """Only a faint line *near* a firm rule is a stray stroke: faint rules a whole row apart are real ones."""
        import numpy as np
        from all_staff.name_table import _row_candidates
        pitch, width = 50, 200
        ink = np.zeros((1100, width), np.float32)
        for n in range(17):
            ink[100 + n * pitch:102 + n * pitch, :(width if not 5 <= n <= 10 else 130)] = 1   # six rules are 65 % inked
        rows = _row_candidates(ink, 0, width, 50, 1000, pitch)
        self.assertGreaterEqual(len(rows), 16)
        self.assertTrue(all(abs((b - a) - pitch) < 6 for a, b in rows[:16]), rows[:16])

    def test_the_whole_table_decides_where_the_first_row_is(self):
        """10/2 update: column 1–14 read "1", "2" and "4" ten rows too low (two-digit codes lose a digit), beat
        the true position by 3 to 1 and took its neighbours with it. The columns are one table."""
        from all_staff.name_table import COLUMNS, _first_row, _layouts, _offset_at
        pitch = 50.0
        # 24 bands per column, the table starts at band 5; the sheet is slightly tilted (3 px per column)
        candidates = [[(100 + n * pitch + 3 * k, 100 + (n + 1) * pitch + 3 * k) for n in range(24)] for k in range(7)]
        readings = {(k, 5 + i): code for k in (4, 5, 6) for i, code in enumerate(COLUMNS[k][:6])}
        readings.update({(0, 15): "1", (0, 16): "2", (0, 18): "4"})        # chance matches ten rows below
        layouts = _layouts(candidates, readings)
        self.assertEqual(max(layouts[0].items(), key=lambda item: item[1][1])[0], 15)   # alone, column 0 is fooled
        level, support, rival = _first_row(layouts, pitch)
        self.assertAlmostEqual(level, 100 + 5 * pitch, delta=pitch / 2)
        self.assertEqual(support, 18)
        self.assertEqual(rival, 3)
        self.assertEqual([_offset_at(layouts[k], level, pitch) for k in range(7)], [5] * 7)

    def test_a_leaning_sheet_is_followed_across_the_columns(self):
        """9/30 photo with noise: the rules drift 43 px over the table's width, most of a 54 px row. With one
        height for the first row, a column at the edge sat nearer the header row and went one row too high."""
        from all_staff.name_table import COLUMNS, _drift, _first_row, _layouts, _offset_at
        pitch = 50.0
        candidates = [[(100 + n * pitch - 8 * k, 100 + (n + 1) * pitch - 8 * k) for n in range(24)] for k in range(7)]
        drift = _drift(candidates, pitch)
        for k, shift in enumerate(drift):
            self.assertAlmostEqual(shift, -8 * k, delta=1)
        # only the four right-hand columns read anything: two codes each, at band 3
        readings = {(k, 3 + i): code for k in (3, 4, 5, 6) for i, code in enumerate(COLUMNS[k][:2])}
        layouts = _layouts(candidates, readings)
        level, support, _ = _first_row(layouts, pitch, drift)
        self.assertEqual(support, 8)
        self.assertEqual([_offset_at(layouts[k], level + drift[k], pitch) for k in range(7)], [3] * 7)
        # a column in which no rule was found, and a stray band in another, do not upset the rest
        candidates[2] = []
        top = candidates[4][6][0]
        candidates[4] = candidates[4][:6] + [(top + 20, top + 35)] + candidates[4][6:]
        drift = _drift(candidates, pitch)
        for k in (0, 1, 3, 4, 5, 6):
            self.assertAlmostEqual(drift[k], -8 * k, delta=1)
        self.assertEqual(_drift([[]] * 7, pitch), [0.0] * 7)

    def test_a_table_whose_codes_cannot_be_read_gets_no_names(self):
        from all_staff.name_table import _clear, _first_row, _layouts
        self.assertTrue(_clear(support=13, rival=4))                       # the 10/2 update: weakest real case
        self.assertTrue(_clear(support=5, rival=0))                        # a blurred photo: a handful of codes is enough
        self.assertFalse(_clear(support=4, rival=0))                       # too few codes read (chance gave two at most)
        self.assertFalse(_clear(support=20, rival=12))                     # another height is nearly as good
        self.assertFalse(_clear(support=11, rival=5))
        pitch = 50.0
        candidates = [[(100 + n * pitch, 100 + (n + 1) * pitch) for n in range(24)] for _ in range(7)]
        silent = _first_row(_layouts(candidates, {}), pitch)
        self.assertFalse(_clear(*silent[1:]))                              # nothing read: nothing to put names on
        candidates[2] = []                                                  # a column in which no rule was found
        layouts = _layouts(candidates, {})
        self.assertEqual(layouts[2], {})
        self.assertFalse(_clear(*_first_row(layouts, pitch)[1:]))

    def test_rows_are_followed_by_the_rule_they_share(self):
        from all_staff.name_table import _follow
        pitch = 50.0
        bands = [(100 + n * pitch, 150 + n * pitch) for n in range(24)]
        found = _follow(bands, 5, pitch)
        self.assertEqual(sorted(found), list(range(14)))
        self.assertEqual(found[0], bands[5])
        self.assertEqual(found[13], bands[18])
        # the table starts above the bands that were found: the first two codes have no band
        found = _follow(bands, -2, pitch)
        self.assertEqual(sorted(found), list(range(2, 14)))
        self.assertEqual(found[2], bands[0])
        # a band the detection missed leaves its own row empty; the rows below stay on their own codes
        gap = bands[:9] + bands[10:]
        found = _follow(gap, 5, pitch)
        self.assertNotIn(4, found)
        self.assertEqual(found[3], bands[8])
        self.assertEqual(found[5], bands[10])
        self.assertEqual(found[13], bands[18])
        # something that does not continue the table ends it
        broken = bands[:10] + [(bands[10][0] + 4.5 * pitch, bands[10][1] + 4.5 * pitch)]
        self.assertEqual(sorted(_follow(broken, 5, pitch)), [0, 1, 2, 3, 4])

    def test_leaning_vertical_rules_are_found_and_followed(self):
        import numpy as np
        from all_staff.name_table import _vertical_rules
        ink = np.zeros((900, 1000), np.float32)
        leans = [(100, .05), (250, .04), (400, .03), (550, .02), (700, .01), (850, 0.0)]
        for x0, slope in leans:
            for y in range(100, 800):
                if x0 == 400 and y < 275:                 # one rule is faint over its top quarter
                    continue
                start = round(x0 + slope * (y - 450))
                ink[y, start:start + 4] = 1
        # …and the fixed positions the table was fitted with are 12 px off
        rules = _vertical_rules(ink, [x + 12 for x, _ in leans], 100, 800)
        for (x0, slope), rule in zip(leans, rules):
            self.assertIsNotNone(rule, x0)
            self.assertAlmostEqual(rule["slope"], slope, delta=.006, msg=x0)
            self.assertAlmostEqual(rule["left"], x0, delta=2, msg=x0)

    def test_cell_edges_follow_a_leaning_rule_only_when_it_has_moved_away(self):
        from all_staff.name_table import _edge
        rows = [700.0, 1300.0]
        leaning = {"left": 300, "slope": .04, "y_mid": 1000.0}
        straight = {"left": 300, "slope": .001, "y_mid": 1000.0}
        self.assertEqual(_edge(None, 290, 1000, .5, rows), 290)              # no fit: where it always was
        self.assertEqual(_edge(leaning, 300, 1250, 0, rows), 310)            # 300 + .04 × 250
        self.assertEqual(_edge(leaning, 300, 750, 0, rows), 290)
        self.assertEqual(_edge(straight, 301, 1100, .5, rows), 301)          # a straight photo keeps its positions

    def test_real_photo_cells_follow_a_tilted_table(self):
        """On the 9/13 photo the left columns lean ~3°: the last row sits 30 px right of the first."""
        import shutil
        from all_staff.name_table import locate, warp_page
        root = Path(__file__).resolve().parent
        photo, template = root / "data/schedules/9_13.jpg", root / "configs/all_staff_roster_template.local.json"
        if not (photo.is_file() and template.is_file()):
            self.skipTest("sample photo / template not available")
        from backend.phase1_cell_pipeline import GridTemplate
        boxes, _ = locate(warp_page(photo, GridTemplate.from_json(template)))
        self.assertEqual(len(boxes), 98)
        self.assertGreaterEqual(boxes["14"][2] - boxes["1"][2], 20)
        self.assertGreaterEqual(boxes["14"][0] - boxes["1"][0], 20)
        widths = [x2 - x1 for x1, _, x2, _ in boxes.values()]
        self.assertLess(max(widths) - min(widths), .15 * max(widths))      # every name cell about as wide

    def test_real_photo_rows_follow_the_printed_codes(self):
        """The mis-aligned case: names must land on their own code (己 = 蕭志明 on 9/19), never a neighbour's."""
        import shutil
        from all_staff.name_table import load_directory, match_names, read_name_table
        root = Path(__file__).resolve().parent
        photo, template = root / "data/schedules/9_19.jpg", root / "configs/all_staff_roster_template.local.json"
        if not (photo.is_file() and template.is_file() and shutil.which("tesseract")):
            self.skipTest("sample photo / template / tesseract not available")
        from backend.phase1_cell_pipeline import GridTemplate
        names = match_names(read_name_table(photo, GridTemplate.from_json(template)),
                            load_directory(root / "configs/staff_names.txt"))
        truth = {"1": "陳雅婷", "15": "許雅婷", "甲": "陳志明", "戊": "葉雅婷", "己": "蕭志明", "庚": "莊雅婷",
                 "57": "顏志明", "71": "黃雅婷", "A": "賴志明", "M": "廖雅婷", "33": "朱志明", "乙": "彭志明"}
        wrong = {c: n for c, n in names.items() if c in truth and truth[c] != n}
        self.assertEqual(wrong, {})
        self.assertGreaterEqual(len(names), 55)
        self.assertEqual(names.get("己"), "蕭志明")
        self.assertNotIn("蕭志明", [n for c, n in names.items() if c != "己"])

    def test_real_photo_update_of_10_2_has_every_column_on_the_same_rows(self):
        """The 10/2 update used to put column 1–14 ten rows too low and column 29–35 five rows too low,
        and skipped two columns altogether (66 of 98 cells, most of them on the wrong code)."""
        from all_staff.name_table import COLUMNS, locate, warp_page
        root = Path(__file__).resolve().parent
        photo, template = root / "data/schedules/10_2_v2.jpg", root / "configs/all_staff_roster_template.local.json"
        if not (photo.is_file() and template.is_file()):
            self.skipTest("sample photo / template not available")
        from backend.phase1_cell_pipeline import GridTemplate
        boxes, report = locate(warp_page(photo, GridTemplate.from_json(template)))
        self.assertEqual(len(boxes), 98)
        self.assertTrue(all(r["ok"] for r in report), report)
        for row in (0, 6, 13):            # the same row of all seven columns sits at the same height
            tops = [boxes[column[row]][1] for column in COLUMNS]
            self.assertLess(max(tops) - min(tops), 25, (row, tops))
        # …one row below the other: no overlaps, no gaps
        for column in COLUMNS:
            tops = [boxes[code][1] for code in column]
            self.assertTrue(all(40 < b - a < 65 for a, b in zip(tops, tops[1:])), tops)

    def test_real_photo_update_of_10_2_names_land_on_their_own_codes(self):
        """Same day, second photo: names read from it must agree with the first 10/2 photo, code by code."""
        import shutil
        from all_staff.name_table import load_directory, match_names, read_name_table
        root = Path(__file__).resolve().parent
        photo, template = root / "data/schedules/10_2_v2.jpg", root / "configs/all_staff_roster_template.local.json"
        if not (photo.is_file() and template.is_file() and shutil.which("tesseract")):
            self.skipTest("sample photo / template / tesseract not available")
        from backend.phase1_cell_pipeline import GridTemplate
        names = match_names(read_name_table(photo, GridTemplate.from_json(template)),
                            load_directory(root / "configs/staff_names.txt"))
        truth = {"1": "詹志明", "12": "洪志明", "14": "陳雅婷", "21": "洪雅婷", "29": "胡志明", "34": "邱志明",
                 "57": "顏志明", "71": "黃雅婷", "A": "賴志明", "M": "廖雅婷"}
        self.assertEqual({c: names.get(c) for c in truth}, truth)
        self.assertGreaterEqual(len(names), 70)
        for departed in "癸丑寅卯":       # these four are gone: nobody's name may land on their codes
            self.assertNotIn(departed, names)


class PersonNavigationTests(BotCase):
    def setUp(self):
        super().setUp()
        self.store.add_roster('Cteam', 'run-1', 'U1', 5, '2026-10-01')
        self.analyzer.data['names'].update({'35': ['沈姻姻', '沈姻姻'], '54': ['沈小明']})
        self.names = Path(self.folder.name) / 'names.txt'
        self.names.write_text('林小明\n王大同\n陳小華\n朱志明\n沈小明\n')
        self.bot.settings.staff_names = self.names
        self.store.set_staff_name('U1', '林小明')   # a colleague on the 名冊, whom the 名冊守門員 lets in

    def actions(self):
        return [item['action'] for item in self.transport.raw[-1][-1]['quickReply']['items']]

    def test_navigation_uses_present_codes_in_numeric_order_and_skips_unused_codes(self):
        self.ask('查 35')
        actions = self.actions()
        self.assertEqual([a['data'] for a in actions[-2:]], ['person|run-1|26', 'person|run-1|54'])
        self.assertEqual(actions[0]['label'], '查其他人')
        self.assertEqual(actions[0]['fillInText'], '查 2026/10/1 ')
        self.assertNotIn('勤務人員', [a['label'] for a in actions])
        self.assertNotIn('休假人員', [a['label'] for a in actions])

    def test_navigation_has_no_previous_at_first_or_next_at_last(self):
        self.ask('查 5')
        self.assertEqual([a['data'] for a in self.actions()[1:]], ['person|run-1|26'])
        self.ask('查 甲')
        self.assertEqual([a['data'] for a in self.actions()[1:]], ['person|run-1|72'])

    def test_old_card_stays_on_its_roster_after_a_new_upload(self):
        self.ask('查 35')
        action = self.actions()[-1]
        self.store.add_roster('Cteam', 'run-2', 'U1', 5, '2026-10-02')
        self.bot.handle(postback_event('U1', action['data']))
        self.assertIn('10/1', self.reply())
        self.assertIn('沈小明（54）', self.reply())
        self.assertTrue(all('run-1' in a['data'] for a in self.actions()[1:]))

    def test_lookup_other_person_prefill_retains_displayed_date(self):
        self.ask('查 35')
        prefix = self.actions()[0]['fillInText']
        self.store.add_roster('Cteam', 'run-2', 'U1', 5, '2026-10-02')
        self.assertIn('10/1', self.ask(prefix + '王大同'))
        self.assertIn('王大同（26）', self.reply())

    def test_private_roster_button_cannot_be_used_by_another_user(self):
        self.store.add_roster('U2', 'private', 'U2', 5, '2026-10-02')
        count = len(self.transport.raw)
        self.bot.handle(postback_event('U1', 'person|private|26'))
        self.assertEqual(len(self.transport.raw), count)

    def test_unknown_name_still_has_lookup_other_person(self):
        self.ask('查 不存在')
        self.assertEqual(self.actions()[0]['label'], '查其他人')
        self.assertEqual(len(self.actions()), 1)

    def test_shanshan_name_query_reports_explicit_rest_entry(self):
        self.analyzer.data['leave_table'] = {'available': True, 'rest': ['35'], 'leave': []}
        message = self.ask('查 朱志明')
        self.assertIn('朱志明（35）', message)
        self.assertIn('輪休', message)
        self.assertNotIn('沒有找到', message)

    def test_absent_duty_reads_printed_leave_table_when_not_yet_cached(self):
        self.analyzer.ensure_leave = lambda run: {'available': True, 'rest': [], 'leave': [{'code': '35', 'kind': '補休'}]}
        self.assertIn('補休', self.ask('查 朱志明'))

    def test_missing_old_digit_images_keep_reply_with_warning(self):
        def fail(run):
            raise OSError('cell image missing')
        self.analyzer.ensure_duties = fail
        self.assertIn('尚未複核', self.ask('查 72'))
        self.assertIn('尚未複核', self.ask('05-06 國際'))


class RosterCheckTests(unittest.TestCase):
    def test_fixed_codes_and_conflicting_old_claim_are_reported(self):
        import json
        from unittest import mock
        from all_staff.name_table import NAMES_VERSION
        from all_staff.roster_check import check_rosters
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            store = Store(folder / "bot.sqlite3")
            store.set_staff_name("U1", "蕭志明")
            store.add_roster("Cgroup", "run-1", "U1", 1, "2026-10-01")
            store.claim("run-1", "U1", "72")
            (folder / "staff_names.txt").write_text("蕭志明\n", encoding="utf-8")
            (folder / "fixed_staff_codes.json").write_text(json.dumps({"己": "蕭志明"}), encoding="utf-8")
            (folder / "runs/run-1").mkdir(parents=True)
            (folder / "runs/run-1/result.json").write_text(json.dumps(
                {**RESULT, "names": {}, "names_version": NAMES_VERSION}), encoding="utf-8")
            lines = []
            with mock.patch("backend.phase1_cell_pipeline.GridTemplate.from_json", lambda path: None):
                report = check_rosters(folder, Path("t.json"), folder / "staff_names.txt", out=lines.append)
            self.assertEqual(report[0]["names"], 1)
            self.assertIn("固定 1", lines[0])
            self.assertIn("!固定代號衝突", lines[0])

    def test_backfills_read_dates_and_restores_changed_dates(self):
        import json
        from all_staff.name_table import NAMES_VERSION
        from all_staff.roster_check import check_rosters
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            store = Store(folder / "bot.sqlite3")
            store.add_roster("Cgroup", "run-1", "U1", 3, "2026-09-29")
            store._run("update rosters set read_date=null, roster_date='2026-10-01'")   # an old row, changed by 日期
            (folder / "runs/run-1").mkdir(parents=True)
            (folder / "runs/run-1/result.json").write_text(json.dumps(
                {**RESULT, "run": {"id": "run-1", "roster_date": "2026-09-29"}, "names_version": NAMES_VERSION}),
                encoding="utf-8")
            lines = []
            from unittest import mock
            with mock.patch("backend.phase1_cell_pipeline.GridTemplate.from_json", lambda path: None):
                check_rosters(folder, Path("t.json"), None, out=lines.append)
                self.assertIn("！照片是 2026-09-29", lines[0])
                check_rosters(folder, Path("t.json"), None, restore_dates=True, out=lines.append)
            self.assertEqual(Store(folder / "bot.sqlite3").roster_by_run("run-1")["roster_date"], "2026-09-29")

    def test_a_photo_still_waiting_for_an_administrator_is_marked(self):
        import json
        from unittest import mock
        from all_staff.name_table import NAMES_VERSION
        from all_staff.roster_check import check_rosters
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            store = Store(folder / "bot.sqlite3")
            store.add_roster("Cgroup", "run-1", "U1", 3, None, held=True)
            store.add_roster("Cgroup", "run-2", "U1", 3, None)            # undated, but announced
            for run in ("run-1", "run-2"):
                (folder / "runs" / run).mkdir(parents=True)
                (folder / "runs" / run / "result.json").write_text(json.dumps(
                    {**RESULT, "run": {"id": run, "roster_date": None}, "names_version": NAMES_VERSION}),
                    encoding="utf-8")
            lines = []
            with mock.patch("backend.phase1_cell_pipeline.GridTemplate.from_json", lambda path: None):
                check_rosters(folder, Path("t.json"), None, out=lines.append)
            self.assertIn("日期 未知（等管理員確認，尚未通知）", lines[0])
            self.assertNotIn("等管理員確認", lines[1])


class TeamWorkflowTests(BotCase):
    def test_group_events_commands_and_errors_are_silent_in_the_group(self):
        for kind in ("join", "memberJoined", "leave", "memberLeft"):
            event = text_event("U1", "", "Cgroup"); event["type"] = kind
            self.bot.handle(event)
        for command in ("說明", "設定 林小明", "我的班表", "通知 開", "日期 10/1", "同時段", "10-11 國際"):
            self.bot.handle(text_event("U1", command, "Cgroup"))
        self.bot.settings.reply_window = 0
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertEqual(self.transport.replies, [])
        self.assertEqual(self.transport.pushes, [])
        self.analyzer.analyze = lambda *args: (_ for _ in ()).throw(RuntimeError("failure"))
        self.bot.handle(image_event("U2", "Cgroup"))
        self.assertEqual(self.transport.replies, [])
        self.assertEqual([p[0] for p in self.transport.pushes], ["U2"])   # only the uploader hears
        self.assertIn("沒有更新班表", self.transport.pushes[-1][1][0])
        self.assertEqual(self.store.team_roster()["run_id"], "run-1")

    def test_settings_show_name_and_next_step(self):
        self.assertIn("姓名：尚未登記", self.ask("我的設定"))
        self.assertIn("設定 你的姓名", self.reply())
        self.register("U1", "林小明")
        self.assertIn("姓名：林小明", self.ask("我的設定"))
        self.assertIn("通知 開", self.reply())
        self.ask("通知 開")
        self.assertNotIn("輸入", self.ask("我的設定"))
        for command in ("建立單位 安檢隊", "加入 ABC", "單位資訊"):
            self.assertIn("看不懂", self.ask(command, "U3"))

    def test_latest_roster_from_any_group_is_the_team_roster(self):
        self.store.add_roster("Cfirst", "first", "U1", 1)
        self.store.add_roster("U3", "personal", "U3", 1)
        self.store.add_roster("Csecond", "second", "U2", 1)
        self.assertEqual(self.store.roster_for_user("U3")["run_id"], "second")

    def test_private_uploads_are_used_only_until_a_group_posts(self):
        self.store.add_roster("U3", "personal", "U3", 1)
        self.assertEqual(self.store.roster_for_user("U3")["run_id"], "personal")
        self.assertIsNone(self.store.roster_for_user("U2"))

    def test_private_upload_does_not_publish_or_notify(self):
        self.bot.handle(image_event("U3", "Cgroup"))
        self.register("U2", "林小明", notify=True)
        self.transport.pushes.clear()
        self.analyzer.data = {**RESULT, "run": {**RESULT["run"], "id": "private"}}
        self.bot.handle(image_event("U1"))
        self.assertIn("不會改變群組的班表", self.transport.replies[-1][1][0])
        self.assertEqual(self.store.roster_for_user("U2")["run_id"], "run-1")
        self.assertEqual(self.transport.pushes, [])

    def test_anyone_can_correct_the_team_roster_date(self):
        self.bot.handle(image_event("U1", "Cgroup"))
        self.ask("日期 10/2", "U2")
        self.assertEqual(self.store.team_roster()["roster_date"], "2026-10-02")

    def test_unknown_group_roster_date_tells_the_uploader_only(self):
        self.analyzer.data["run"]["roster_date"] = None
        self.bot.handle(image_event("U2", "Cgroup"))
        self.assertEqual(self.transport.replies, [])
        self.assertEqual([p[0] for p in self.transport.pushes], ["U2"])
        self.assertIn("日期未辨識", self.transport.pushes[0][1][0])

    def test_block_disables_notifications(self):
        self.register("U2", "陳小華", notify=True)
        event = text_event("U2", ""); event["type"] = "unfollow"
        self.bot.handle(event)
        self.assertFalse(self.store.user("U2")["notify"])

    def test_names_claims_and_rosters_persist_across_restart(self):
        self.register("U2", "陳小華", notify=True)
        self.store.add_roster("Cgroup", "run-1", "U1", 3)
        self.store.claim("run-1", "U2", "26")
        reopened = Store(self.path)
        self.assertEqual(reopened.user("U2")["staff_name"], "陳小華")
        self.assertTrue(reopened.user("U2")["notify"])
        self.assertEqual(reopened.claims("run-1"), {"26": "陳小華"})
        self.assertEqual(reopened.team_roster()["run_id"], "run-1")

    def test_old_database_gets_the_new_columns(self):
        import sqlite3
        path = Path(self.folder.name) / "old.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("create table users(user_id text primary key, code text, name text, notify integer default 0, "
                       "updated_at real)")
            db.execute("insert into users values('U9', '72', '舊', 1, 0)")
            db.execute("create table rosters(id integer primary key autoincrement, chat_id text, run_id text, "
                       "uploaded_by text, created_at real, codes integer)")
        store = Store(path)
        store.add_roster("C", "r", "U", 1, "2026-10-01")
        self.assertEqual(store.latest_roster("C")["roster_date"], "2026-10-01")
        self.assertEqual(store.legacy_subscribers()[0]["code"], "72")


class SequenceAnalyzer(FakeAnalyzer):
    """One run per photo, named after its LINE message id; ``dates`` says which day each one prints."""

    def __init__(self, dates, edits=None):
        super().__init__()
        self.dates, self.edits, self.results = dates, edits or {}, {}

    def analyze(self, payload, name):
        self.calls += 1
        message = name[len("line-"):-len(".jpg")]
        data = copy.deepcopy(RESULT)
        data["run"].update(id=f"run-{message}", roster_date=self.dates[message])
        if message in self.edits:
            self.edits[message](data)   # what this photo reads differently from the usual roster
        self.results[data["run"]["id"]] = data
        return data

    def result(self, run_id):
        return self.results[run_id]


class LatestPhotoOfTheDayTests(BotCase):
    """Several photos of one printed day in the group: the one sent last is that day's roster."""

    def setUp(self):
        super().setUp()
        self.analyzer = SequenceAnalyzer({"first": "2026-10-01", "update": "2026-10-01", "third": "2026-10-01",
                                          "next-day": "2026-10-02", "no-date": None})
        self.bot.analyzer = self.analyzer
        self.base = time.time() - 3600
        self.register("U1", "林小明", notify=True)
        self.transport.pushes.clear()

    def send(self, message, sent, user="U2", group="Cgroup"):
        """A photo LINE says was sent ``sent`` seconds after the first one (recognition finishes whenever)."""
        event = image_event(user, group)
        event["message"]["id"] = message
        event["timestamp"] = int((self.base + sent) * 1000)
        self.bot.handle(event)

    def heard(self):
        return [texts for to, texts in self.transport.pushes if to == "U1"]

    def test_a_second_photo_of_the_day_is_announced_as_an_update_and_becomes_the_roster(self):
        self.send("first", 0)
        self.assertIn("📌 新班表｜林小明（72）", self.heard()[-1][0])
        self.send("update", 600)
        self.assertEqual(len(self.heard()), 2)
        self.assertIn("🔄 班表更新｜林小明（72）", self.heard()[-1][0])
        self.assertEqual(self.store.roster_on("U1", date(2026, 10, 1))["run_id"], "run-update")
        self.assertEqual(self.store.team_roster()["run_id"], "run-update")
        self.assertEqual(self.store.roster_for_user("U1")["run_id"], "run-update")
        self.assertIn("林小明（72）　10/1", self.ask("10/1"))

    def test_an_older_photo_that_finishes_recognition_later_changes_nothing(self):
        self.send("update", 600)             # sent second, recognised first
        self.transport.pushes.clear()
        self.send("first", 0)                # sent first, recognised last
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.store.roster_on("U1", date(2026, 10, 1))["run_id"], "run-update")
        self.assertEqual(self.store.team_roster()["run_id"], "run-update")
        self.assertEqual(self.store.roster_by_run("run-first")["roster_date"], "2026-10-01")   # still on file

    def test_a_photo_sent_between_two_known_ones_is_silent_and_the_newest_stays(self):
        self.send("first", 0)
        self.send("update", 1200)
        self.transport.pushes.clear()
        self.send("third", 600)
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.store.roster_on("U1", date(2026, 10, 1))["run_id"], "run-update")

    def test_the_send_time_not_the_arrival_order_decides(self):
        self.send("first", 0)
        self.send("update", 600)
        self.transport.pushes.clear()
        self.send("third", 300)              # arrives last but was sent before the update
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.store.team_roster()["run_id"], "run-update")

    def test_another_day_is_a_new_roster_not_an_update(self):
        self.send("first", 0)
        self.send("next-day", 600)
        self.assertIn("📌 新班表｜林小明（72）", self.heard()[-1][0])
        self.assertNotIn("更新", self.heard()[-1][0])

    def test_a_private_photo_of_the_same_day_does_not_make_the_group_photo_an_update(self):
        self.send("first", 0, user="U3", group=None)
        self.assertEqual(self.heard(), [])
        self.send("update", 600)
        self.assertIn("📌 新班表｜林小明（72）", self.heard()[-1][0])

    def test_a_private_upload_never_silences_a_group_photo(self):
        self.send("update", 600, user="U3", group=None)       # sent later, but only for U3
        self.send("first", 0)
        self.assertIn("📌 新班表｜林小明（72）", self.heard()[-1][0])
        self.assertEqual(self.store.team_roster()["run_id"], "run-first")

    def test_an_unknown_date_cannot_be_compared_so_it_is_announced_as_new(self):
        self.send("first", 0)
        self.send("no-date", 600)
        self.assertIn("📌 新班表", self.heard()[-1][0])
        self.assertEqual([to for to, _ in self.transport.pushes].count("U2"), 1)   # the uploader is asked for the date

    def test_subscribers_not_on_the_update_are_told_with_the_same_label(self):
        self.register("U3", "不存在的人", notify=True)
        self.transport.pushes.clear()
        self.send("first", 0)
        self.send("update", 600)
        texts = [t for to, t in self.transport.pushes if to == "U3"]
        self.assertTrue(texts[0][0].startswith("📌 新班表"))
        self.assertTrue(texts[1][0].startswith("🔄 班表更新"))

    def test_notifying_stops_when_a_newer_photo_of_the_day_turns_up_midway(self):
        self.register("U3", "王大同", notify=True)
        self.transport.pushes.clear()
        push = self.transport.push

        def push_then_a_newer_photo_lands(to, messages):
            push(to, messages)
            if not self.store.roster_by_run("run-newer"):
                self.store.add_roster("Cgroup", "run-newer", "U2", 1, "2026-10-01", self.base + 900)
        self.transport.push = push_then_a_newer_photo_lands
        self.send("first", 0)
        self.assertEqual(len(self.transport.pushes), 1)      # the newer photo notifies everybody else itself

    # -- does the update change my day? ----------------------------------------------------------
    @staticmethod
    def person(data, code):
        return next(p for p in data["people"] if p["code"] == code)

    def test_an_update_says_so_when_your_day_is_the_same(self):
        self.send("first", 0)
        self.assertNotIn("上一版", self.heard()[-1][0])          # nothing to compare a first photo with
        self.send("update", 600)
        self.assertIn("🔄 班表更新｜林小明（72）・你的勤務與上一版相同", self.heard()[-1][0])

    def test_an_update_says_so_when_your_duties_moved(self):
        self.analyzer.edits["update"] = lambda data: self.person(data, "72")["slots"].append(slot(7, 2, "國際線"))
        self.send("first", 0)
        self.send("update", 600)
        self.assertIn("🔄 班表更新｜林小明（72）・你的勤務與上一版不同", self.heard()[-1][0])

    def test_the_comparison_follows_the_name_when_the_code_changed(self):
        def recoded(data):                      # the same person is 26 on the update
            data["names"]["26"] = ["林小明", "林小朋"]
            data["names"]["72"] = ["某人", ""]
            self.person(data, "26")["slots"] = [copy.deepcopy(s) for s in self.person(RESULT, "72")["slots"]]
        self.analyzer.edits["update"] = recoded
        self.send("first", 0)
        self.send("update", 600)
        self.assertIn("🔄 班表更新｜林小明（26）・你的勤務與上一版相同", self.heard()[-1][0])

    def test_an_update_without_your_duties_says_so(self):
        self.analyzer.edits["update"] = lambda data: data["people"].pop(0)       # 72 reads no duty any more
        self.send("first", 0)
        self.send("update", 600)
        message = self.heard()[-1][0]
        self.assertIn("・上一版有你的勤務，這一版沒有", message)
        self.assertIn("這張班表上沒有辨識到代號 72", message)

    def test_an_update_that_no_longer_names_you_warns_before_the_usual_explanation(self):
        self.analyzer.edits["update"] = lambda data: data["names"].pop("72")
        self.send("first", 0)
        self.send("update", 600)
        message = self.heard()[-1][0]
        self.assertTrue(message.startswith("🔄 班表更新\n⚠ 上一版有你的勤務，這一版沒有\n"), message)
        self.assertIn("沒有找到「林小明」", message)

    def test_someone_who_only_appears_on_the_update_is_told_so(self):
        self.analyzer.edits["first"] = lambda data: data["people"].pop(0)
        self.send("first", 0)
        self.send("update", 600)
        self.assertIn("🔄 班表更新｜林小明（72）・上一版沒有你的勤務", self.heard()[-1][0])

    def test_someone_on_neither_version_gets_no_comparison(self):
        self.register("U3", "不存在的人", notify=True)
        self.send("first", 0)
        self.send("update", 600)
        texts = [t for to, t in self.transport.pushes if to == "U3"]
        self.assertNotIn("上一版", texts[-1][0])

    def test_an_earlier_version_that_cannot_be_read_does_not_stop_the_update(self):
        self.send("first", 0)
        original = self.analyzer.result

        def unreadable(run_id):
            if run_id == "run-first":
                raise OSError("result.json is gone")
            return original(run_id)
        self.analyzer.result = unreadable
        self.send("update", 600)
        message = self.heard()[-1][0]
        self.assertIn("🔄 班表更新｜林小明（72）", message)
        self.assertNotIn("上一版", message)

    def test_duty_comparison(self):
        mine = duty_signature(RESULT, "72")
        self.assertEqual([block[:2] for block in mine], [("05", "07"), ("07", "08")])
        self.assertIsNone(duty_signature(RESULT, "99"))
        self.assertIsNone(duty_signature(RESULT, None))
        self.assertEqual(duty_change(mine, list(mine)), "你的勤務與上一版相同")
        self.assertEqual(duty_change(mine, mine[:1]), "你的勤務與上一版不同")
        self.assertEqual(duty_change(None, mine), "上一版沒有你的勤務")
        self.assertEqual(duty_change(mine, None), "上一版有你的勤務，這一版沒有")
        self.assertEqual(duty_change(None, None), "")

    def test_sent_time_comes_from_line_and_falls_back_to_now(self):
        now = time.time()
        stamp = int((now - 120) * 1000)
        self.assertEqual(RosterBot._sent_at({"timestamp": stamp}), stamp / 1000)
        self.assertEqual(RosterBot._sent_at({"timestamp": str(stamp)}), stamp / 1000)
        for event in ({}, {"timestamp": None}, {"timestamp": "soon"}, {"timestamp": 0}, {"timestamp": -5},
                      {"timestamp": int((now + 86400) * 1000)}):      # absent, unreadable or from the future
            self.assertAlmostEqual(RosterBot._sent_at(event), now, delta=5, msg=event)

    def test_same_day_orders_by_send_time_then_by_arrival(self):
        add = self.store.add_roster
        add("Cgroup", "a", "U2", 1, "2026-10-01", 100.0)
        add("Cgroup", "b", "U2", 1, "2026-10-01", 100.0)      # the same instant: arrival order breaks the tie
        add("Cgroup", "c", "U2", 1, "2026-10-01", 50.0)       # sent earlier, recognised last
        add("U3", "mine", "U3", 1, "2026-10-01", 200.0)       # a private photo is not a group roster
        a, b, c, mine = (self.store.roster_by_run(run) for run in ("a", "b", "c", "mine"))
        run = lambda roster: roster and roster["run_id"]
        self.assertEqual(run(self.store.same_day(a, later=True)), "b")
        self.assertIsNone(self.store.same_day(b, later=True))
        self.assertEqual(run(self.store.same_day(b, later=False)), "a")
        self.assertEqual(run(self.store.same_day(c, later=True)), "a")
        self.assertIsNone(self.store.same_day(c, later=False))
        self.assertEqual(run(self.store.same_day(b, later=True, user_id="U3")), "mine")   # one user's own view
        self.assertEqual(run(self.store.same_day(mine, later=False, user_id="U3")), "b")
        self.assertEqual(self.store.team_roster()["run_id"], "b")
        self.assertIsNone(self.store.same_day({**a, "roster_date": None}, later=True))

    def test_rows_saved_before_sent_at_existed_fall_back_to_their_save_time(self):
        import sqlite3
        path = Path(self.folder.name) / "old.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("create table rosters(id integer primary key autoincrement, chat_id text, run_id text, "
                       "uploaded_by text, created_at real, codes integer, roster_date text)")
            db.execute("insert into rosters(chat_id, run_id, uploaded_by, created_at, codes, roster_date) "
                       "values('Cgroup', 'old', 'U2', 500.0, 1, '2026-10-01')")
        store = Store(path)
        store.add_roster("Cgroup", "new", "U2", 1, "2026-10-01", 400.0)
        old, new = store.roster_by_run("old"), store.roster_by_run("new")
        self.assertIsNone(old["sent_at"])
        self.assertEqual(store.team_roster()["run_id"], "old")
        self.assertEqual(store.same_day(new, later=True)["run_id"], "old")
        self.assertEqual(store.same_day(old, later=False)["run_id"], "new")


def flex_buttons(node):
    """The action of every button on a flex card, in reading order."""
    if isinstance(node, list):
        return [action for child in node for action in flex_buttons(child)]
    if isinstance(node, dict):
        if node.get("type") == "button":
            return [node["action"]]
        return [action for child in node.values() for action in flex_buttons(child)]
    return []


class HeldCase(BotCase):
    """A subscriber (U1), an uploader (U2) and an administrator (U9); one run per photo, as in SequenceAnalyzer."""
    friends = {"U1", "U2", "U3", "U9"}
    settings = {"admins": ("U9",)}

    def setUp(self):
        super().setUp()
        self.analyzer = SequenceAnalyzer({"first": "2026-10-01", "later": "2026-10-01", "next-day": "2026-10-02",
                                          "misread": "2026-10-03", "no-date": None, "no-date-2": None, "mine": None})
        self.bot.analyzer = self.analyzer
        self.base = time.time() - 3600
        self.register("U1", "林小明", notify=True)
        self.quiet()

    def quiet(self):
        self.transport.pushes.clear(); self.transport.raw_pushes.clear(); self.transport.replies.clear()

    def send(self, message, sent, user="U2", group="Cgroup"):
        """A photo LINE says was sent ``sent`` seconds after the first one; ``group=None`` sends it privately."""
        event = image_event(user, group)
        event["message"]["id"] = message
        event["timestamp"] = int((self.base + sent) * 1000)
        self.bot.handle(event)

    def told(self, user):
        return [texts for to, texts in self.transport.pushes if to == user]

    def tap(self, run, choice, user="U9"):
        """A button of the request card: a date (ISO) or 「略過這張」 (``skip``)."""
        self.bot.handle(postback_event(user, f"date|{run}|{choice}"))
        return self.reply()

    def roster(self, run):
        return self.store.roster_by_run(run)


class HeldForTheDateTests(HeldCase):
    """With administrators, a group photo whose date cannot be read waits for one of them: nobody hears before."""

    def test_an_undated_group_photo_asks_the_administrator_and_nobody_else_hears(self):
        from all_staff.bot import TAIPEI
        self.send("no-date", 0)
        self.assertEqual([to for to, _ in self.transport.pushes], ["U9"])    # not the subscriber, not the uploader
        self.assertEqual(self.transport.replies, [])
        self.assertTrue(self.roster("run-no-date")["held"])
        card = self.transport.raw_pushes[0][1][-1]
        self.assertEqual(card["type"], "flex")
        texts = flex_texts(card["contents"])
        self.assertIn("⚠ 班表日期讀不到", texts)
        self.assertTrue(any("還沒有通知任何人" in line and "已開通知的 1 人" in line for line in texts), texts)
        sent = datetime.fromtimestamp(self.base, TAIPEI)
        self.assertIn(f"群組 {sent.month}/{sent.day} {sent:%H:%M} 上傳的班表", texts)
        days = [(sent.date() + timedelta(days=offset)).isoformat() for offset in (0, 1, 2)]
        self.assertEqual([action["data"] for action in flex_buttons(card)],
                         [f"date|run-no-date|{day}" for day in days] + ["date|run-no-date|skip"])
        self.assertTrue(all(action["type"] == "postback" for action in flex_buttons(card)))

    def test_the_administrator_names_the_day_and_then_everyone_is_notified_with_it(self):
        self.send("no-date", 0)
        self.quiet()
        reply = self.tap("run-no-date", "2026-10-02")
        self.assertIn("日期已設為 10/2（五）", reply)
        self.assertIn("現在通知已開通知的 1 人", reply)
        self.assertIn("📌 新班表｜林小明（72）　10/2（五）", self.told("U1")[-1][0])
        self.assertEqual(self.told("U2"), [])                                # the uploader is never bothered
        roster = self.roster("run-no-date")
        self.assertEqual((roster["roster_date"], roster["read_date"], roster["held"]), ("2026-10-02", None, 0))
        self.assertEqual(self.store.roster_on("U1", date(2026, 10, 2))["run_id"], "run-no-date")
        self.assertEqual(self.store.held_rosters(), [])

    def test_a_dated_group_photo_is_announced_at_once_as_before(self):
        self.send("first", 0)
        self.assertIn("📌 新班表｜林小明（72）　10/1（四）", self.told("U1")[-1][0])
        self.assertEqual(len(self.told("U9")), 1)              # the administrator: only the report of the upload
        self.assertIn("✅ 群組班表辨識成功", self.told("U9")[0][0])
        self.assertFalse(self.roster("run-first")["held"])

    def test_the_update_of_a_day_is_compared_with_the_earlier_photo_once_its_date_is_known(self):
        self.send("first", 0)
        self.send("no-date", 600)                    # the corrected sheet of 10/1; its header cannot be read
        self.quiet()
        reply = self.tap("run-no-date", "2026-10-01")
        self.assertIn("這是同一天的更新版", reply)
        self.assertIn("🔄 班表更新｜林小明（72）・你的勤務與上一版相同", self.told("U1")[-1][0])
        self.assertEqual(self.store.roster_on("U1", date(2026, 10, 1))["run_id"], "run-no-date")

    def test_a_held_photo_already_replaced_by_a_later_one_of_its_day_is_filed_without_notifying(self):
        self.send("no-date", 0)
        self.send("later", 600)                      # the same day, readable, sent afterwards
        self.assertIn("📌 新班表｜林小明（72）", self.told("U1")[-1][0])
        self.quiet()
        reply = self.tap("run-no-date", "2026-10-01")
        self.assertIn("同一天已經有更晚傳送的班表", reply)
        self.assertIn("沒有另外通知", reply)
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.store.roster_on("U1", date(2026, 10, 1))["run_id"], "run-later")
        self.assertFalse(self.roster("run-no-date")["held"])

    def test_a_second_tap_changes_nothing_and_notifies_nobody_again(self):
        self.send("no-date", 0)
        self.tap("run-no-date", "2026-10-02")
        self.quiet()
        self.assertIn("已經確認過日期：10/2（五）", self.tap("run-no-date", "2026-10-03"))
        self.assertIn("已經確認過日期：10/2（五）", self.tap("run-no-date", "skip"))
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.roster("run-no-date")["roster_date"], "2026-10-02")

    def test_dismissing_keeps_the_photo_and_tells_nobody(self):
        self.send("no-date", 0)
        self.quiet()
        self.assertIn("不會通知，日期維持未辨識", self.tap("run-no-date", "skip"))
        roster = self.roster("run-no-date")
        self.assertEqual((roster["roster_date"], roster["held"]), (None, 0))
        self.assertIn("先前已經略過", self.tap("run-no-date", "2026-10-02"))   # its date buttons are spent too
        self.assertEqual(self.transport.pushes, [])
        self.assertIsNone(self.roster("run-no-date")["roster_date"])

    def test_a_typed_date_asks_for_a_tap_before_anyone_is_notified(self):
        self.send("no-date", 0)
        self.quiet()
        self.ask("日期 2026/10/5", "U9")
        card = self.transport.raw[-1][0]
        self.assertIn("要用這個日期通知嗎？", flex_texts(card["contents"]))
        self.assertEqual([action["data"] for action in flex_buttons(card)],
                         ["date|run-no-date|2026-10-05", "date|run-no-date|skip"])
        self.assertEqual(self.transport.pushes, [])                          # typing alone releases nothing
        roster = self.roster("run-no-date")
        self.assertEqual((roster["roster_date"], roster["held"]), (None, 1))
        self.tap("run-no-date", "2026-10-05")
        self.assertIn("📌 新班表｜林小明（72）　10/5（一）", self.told("U1")[-1][0])

    def test_while_a_photo_waits_a_typed_date_is_about_it_whatever_else_was_sent_since(self):
        self.send("no-date", 0)
        self.send("no-date-2", 300)                      # a second one without a date: the newest comes first
        self.send("misread", 600)                        # somebody's later photo, read as 10/3 and announced
        self.send("mine", 900, user="U9", group=None)    # and the administrator's own private photo
        self.quiet()
        self.ask("日期 2026/10/5", "U9")
        self.assertEqual([action["data"] for action in flex_buttons(self.transport.raw[-1][0])],
                         ["date|run-no-date-2|2026-10-05", "date|run-no-date-2|skip"])
        self.assertEqual([self.roster(run)["roster_date"] for run in ("run-no-date", "run-no-date-2", "run-misread",
                                                                      "run-mine")], [None, None, "2026-10-03", None])
        self.tap("run-no-date-2", "skip")                # once it is out of the way, the older one is next
        self.ask("日期 2026/10/5", "U9")
        self.assertEqual(flex_buttons(self.transport.raw[-1][0])[0]["data"], "date|run-no-date|2026-10-05")
        self.tap("run-no-date", "skip")                  # nothing waits any more: the photo sent last, their own
        self.assertIn("你私訊", self.ask("日期 2026/10/5", "U9"))
        self.assertEqual(self.roster("run-mine")["roster_date"], "2026-10-05")
        self.assertEqual(self.told("U1"), [])

    def test_a_photo_held_for_administrators_who_are_gone_can_be_dated_the_old_way(self):
        self.send("no-date", 0)
        self.quiet()
        self.bot.settings.admins = ()                    # LINE_V2_ADMIN_USER_IDS removed, bot restarted
        self.assertIn("日期已改為 10/2（五）", self.ask("日期 2026/10/2", "U2"))
        roster = self.roster("run-no-date")
        self.assertEqual((roster["roster_date"], roster["held"]), ("2026-10-02", 0))
        self.assertEqual(self.transport.pushes, [])

    def test_with_nothing_waiting_the_administrator_corrects_the_newest_photo_not_an_old_one_of_their_own(self):
        self.send("first", 0, user="U9")                 # the administrator posted 10/1 earlier
        self.send("misread", 600)                        # somebody else's photo, read as 10/3
        self.quiet()
        reply = self.ask("日期 10/2", "U9")
        self.assertEqual(self.roster("run-misread")["roster_date"], "2026-10-02")
        self.assertEqual(self.roster("run-first")["roster_date"], "2026-10-01")
        self.assertIn("照片上辨識到的日期是 10/3（六）", reply)
        self.assertEqual(self.transport.pushes, [])                          # a correction notifies nobody

    def test_nobody_else_can_change_the_date_of_a_group_roster(self):
        self.send("first", 0)                            # posted in the group by U2
        for user in ("U2", "U3"):                        # its uploader, and somebody who never sent a photo
            self.assertIn("群組班表的日期由管理員更正", self.ask("日期 10/9", user))
        self.assertEqual(self.roster("run-first")["roster_date"], "2026-10-01")
        self.send("mine", 600, user="U3", group=None)    # a photo of one's own stays one's own to correct
        self.assertIn("你私訊", self.ask("日期 2026/10/4", "U3"))
        self.assertEqual(self.roster("run-mine")["roster_date"], "2026-10-04")
        self.send("next-day", 1200, user="U3")           # …until the last thing they sent is a group photo
        self.assertIn("群組班表的日期由管理員更正", self.ask("日期 10/9", "U3"))
        self.assertEqual(self.roster("run-mine")["roster_date"], "2026-10-04")
        self.assertEqual(self.roster("run-next-day")["roster_date"], "2026-10-02")

    def test_the_buttons_do_nothing_for_anyone_else_or_with_anything_else(self):
        self.send("no-date", 0)
        self.send("mine", 600, user="U9", group=None)
        self.quiet()
        for user, data in (("U2", "date|run-no-date|2026-10-02"), ("U1", "date|run-no-date|skip"),
                           ("U9", "date|run-no-date|tomorrow"), ("U9", "date|run-unknown|2026-10-02"),
                           ("U9", "date|run-mine|2026-10-02"), ("U9", "date|run-no-date")):
            self.bot.handle(postback_event(user, data))
        self.assertEqual(self.transport.replies, [])
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual((self.roster("run-no-date")["roster_date"], self.roster("run-no-date")["held"]), (None, 1))
        self.assertIsNone(self.roster("run-mine")["roster_date"])

    def test_a_reading_that_cannot_be_loaded_leaves_the_photo_waiting(self):
        self.send("no-date", 0)
        self.quiet()
        reading = self.analyzer.results.pop("run-no-date")
        self.assertIn("辨識結果讀不出來", self.tap("run-no-date", "2026-10-02"))
        self.assertEqual((self.roster("run-no-date")["roster_date"], self.roster("run-no-date")["held"]), (None, 1))
        self.assertEqual(self.transport.pushes, [])
        self.analyzer.results["run-no-date"] = reading
        self.tap("run-no-date", "2026-10-02")                                # the same button works afterwards
        self.assertIn("📌 新班表｜林小明（72）　10/2（五）", self.told("U1")[-1][0])

    def test_an_expired_reply_token_does_not_stop_the_notifications(self):
        self.send("no-date", 0)
        self.quiet()
        def expired(token, messages):
            raise RuntimeError("LINE API 400: Invalid reply token")
        self.transport.reply = expired
        self.bot.handle(postback_event("U9", "date|run-no-date|2026-10-02"))
        self.assertIn("📌 新班表｜林小明（72）　10/2（五）", self.told("U1")[-1][0])

    def test_when_no_administrator_can_be_reached_the_roster_is_announced_undated_rather_than_kept(self):
        import contextlib
        import io
        self.transport.friends.discard("U9")             # blocked the bot, or the configured id is not theirs
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.send("no-date", 0)
        self.assertIn("📌 新班表｜林小明（72）　（日期未辨識）", self.told("U1")[-1][0])
        self.assertEqual(self.told("U2"), [])            # the uploader could not set the date, so is not asked to
        self.assertFalse(self.roster("run-no-date")["held"])
        self.assertIn("LINE_V2_ADMIN_USER_IDS", out.getvalue())
        self.assertIn("日期已改為 10/2（五）", self.ask("日期 2026/10/2", "U9"))  # typed: nothing waits, no round
        self.assertEqual(len(self.told("U1")), 1)

    def test_with_nobody_subscribed_the_card_is_only_about_the_date(self):
        self.ask("通知 關", "U1")
        self.quiet()
        self.send("no-date", 0)
        card = self.transport.raw_pushes[0][1][-1]
        body = "\n".join(flex_texts(card["contents"]))
        self.assertIn("「我的班表」和日期都查不到這張", body)
        self.assertNotIn("通知", body + card["altText"])
        self.assertEqual([action["label"] for action in flex_buttons(card)][-1], "略過這張")
        self.ask("日期 2026/10/5", "U9")
        confirm = self.transport.raw[-1][0]
        self.assertIn("要設成這個日期嗎？", flex_texts(confirm["contents"]))
        self.assertEqual([action["label"] for action in flex_buttons(confirm)], ["10/5（一）", "略過這張"])
        self.quiet()
        reply = self.tap("run-no-date", "2026-10-05")
        self.assertIn("目前沒有人開通知，所以沒有發出通知", reply)
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.store.roster_on("U1", date(2026, 10, 5))["run_id"], "run-no-date")   # findable now

    def test_a_private_photo_is_never_held(self):
        self.send("mine", 0, user="U3", group=None)
        self.assertIn("讀不到班表上的日期", self.transport.replies[-1][1][0])
        self.assertFalse(self.roster("run-mine")["held"])
        self.assertEqual(self.transport.pushes, [])

    def test_settings_say_who_is_an_administrator_and_anyone_can_ask_for_their_user_id(self):
        self.assertIn("管理員：是", self.ask("我的設定", "U9"))
        self.assertNotIn("管理員", self.ask("我的設定", "U1"))
        self.send("no-date", 0)
        self.assertIn("管理員：是（1 張群組班表在等你確認日期）", self.ask("我的設定", "U9"))
        for wording in ("我的ID", "我的 id", "userId", "我的ＵserID"):
            self.assertIn("你的 LINE userId：\nU3\n", self.ask(wording, "U3"))
        self.assertIn("不是加好友用的 LINE ID", self.reply())

    def test_held_photos_survive_a_restart_and_old_databases_get_the_column(self):
        import sqlite3
        self.send("no-date", 0)
        self.assertEqual([r["run_id"] for r in Store(self.path).held_rosters()], ["run-no-date"])
        path = Path(self.folder.name) / "old.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("create table rosters(id integer primary key autoincrement, chat_id text, run_id text, "
                       "uploaded_by text, created_at real, codes integer, roster_date text)")
            db.execute("insert into rosters(chat_id, run_id, uploaded_by, created_at, codes) "
                       "values('Cgroup', 'old', 'U2', 500.0, 1)")       # undated, announced by the old bot
        store = Store(path)
        self.assertEqual(store.roster_by_run("old")["held"], 0)
        self.assertEqual(store.held_rosters(), [])
        store.add_roster("Cgroup", "new", "U2", 1, None, held=True)
        store.add_roster("U3", "private", "U3", 1, None)
        self.assertEqual([r["run_id"] for r in store.held_rosters()], ["new"])
        self.assertTrue(store.confirm_date("new", "2026-10-02"))
        self.assertFalse(store.confirm_date("new", "2026-10-03"))            # only the first caller notifies
        self.assertFalse(store.unhold("new"))
        self.assertEqual(store.roster_by_run("new")["roster_date"], "2026-10-02")


class HeldWithTwoAdministratorsTests(HeldCase):
    friends = {"U1", "U2", "U3", "U8", "U9"}
    settings = {"admins": ("U9", "U8"), "public_base": "https://bot.example"}

    def test_every_administrator_is_asked_and_shown_the_photo(self):
        self.send("no-date", 0)
        self.assertEqual([to for to, _ in self.transport.raw_pushes], ["U9", "U8"])
        for _, messages in self.transport.raw_pushes:                         # the photo first: its date is on it
            self.assertEqual([message["type"] for message in messages], ["image", "flex"])
            self.assertEqual(messages[0]["originalContentUrl"], "https://bot.example/runs/run-no-date/roster-v1.jpg")

    def test_whoever_answers_first_decides_and_the_other_is_told_it_is_done(self):
        self.send("no-date", 0)
        self.quiet()
        self.assertIn("日期已設為 10/2（五）", self.tap("run-no-date", "2026-10-02", user="U8"))
        self.assertEqual([m.split("]")[0] for m in self.told("U1")[-1]], ["[image", "[flex", "[image"])
        self.quiet()
        self.assertIn("已經確認過日期：10/2（五）", self.tap("run-no-date", "2026-10-03", user="U9"))
        self.assertEqual(self.transport.pushes, [])

    def test_one_unreachable_administrator_does_not_matter(self):
        self.transport.friends.discard("U9")
        self.send("no-date", 0)
        self.assertEqual([to for to, _ in self.transport.pushes], ["U8"])
        self.assertTrue(self.roster("run-no-date")["held"])


class OperatorLogTests(BotCase):
    """The server log names the duty codes that got no name, so gaps show up without reading every roster."""

    def logged(self, event):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.bot.handle(event)
        return out.getvalue()

    def test_a_group_roster_logs_the_duty_codes_that_have_no_name(self):
        self.register("U1", "林小明", notify=True)
        log = self.logged(image_event("U2", "Cgroup"))
        self.assertIn("2 個有勤務的代號沒有對到姓名：5、26", log)    # only 林小明 is registered here
        self.assertIn("1 個有勤務的代號當天沒有這個人（勤務格的代號可能讀錯）：甲（固定代號沒有設定）", log)

    def test_a_private_roster_logs_them_too(self):
        self.register("U1", "林小明")
        self.assertIn("沒有對到姓名：5、26", self.logged(image_event("U1")))

    def test_nothing_is_logged_when_every_duty_has_a_name(self):
        for user, name in (("U1", "林小明"), ("U2", "陳小華"), ("U3", "王大同"), ("U4", "某某某")):
            self.register(user, name)
        self.assertNotIn("沒有對到姓名", self.logged(image_event("U2", "Cgroup")))


class NotARosterTests(BotCase):
    """A group has all sorts of photos: only people who post rosters are told about one that fails."""

    def failing(self, error):
        def analyze(*args):
            raise error
        self.analyzer.analyze = analyze

    def test_a_stray_group_photo_is_ignored_silently(self):
        from all_staff.demo import NotARoster
        self.failing(NotARoster("這張圖片找不到班表的表格框線，看起來不是班表。"))
        self.bot.handle(image_event("U3", "Cgroup"))
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.transport.replies, [])

    def test_someone_who_posts_rosters_hears_about_a_bad_shot(self):
        from all_staff.demo import NotARoster
        self.store.add_roster("Cgroup", "earlier", "U2", 5, "2026-09-30")
        self.failing(NotARoster("這張圖片找不到班表的表格框線，看起來不是班表。"))
        self.bot.handle(image_event("U2", "Cgroup"))
        self.assertEqual([to for to, _ in self.transport.pushes], ["U2"])
        self.assertIn("看起來不是班表", self.transport.pushes[0][1][0])
        self.assertIn("這張沒有更新班表", self.transport.pushes[0][1][0])
        self.assertEqual(self.transport.replies, [])

    def test_a_private_photo_that_is_not_a_roster_is_answered(self):
        from all_staff.demo import NotARoster
        self.failing(NotARoster("這張圖片找不到班表的表格框線，看起來不是班表。"))
        self.bot.handle(image_event("U3"))
        self.assertIn("看起來不是班表", self.transport.replies[-1][1][0])

    def test_a_roster_that_cannot_be_cut_into_cells_still_tells_its_uploader(self):
        from all_staff.demo import DemoError
        self.failing(DemoError("無法從照片切出勤務格：x"))
        self.bot.handle(image_event("U3", "Cgroup"))
        self.assertEqual([to for to, _ in self.transport.pushes], ["U3"])
        self.assertIn("無法從照片切出勤務格", self.transport.pushes[0][1][0])

    def test_has_uploaded_counts_only_the_uploader(self):
        self.store.add_roster("Cgroup", "earlier", "U2", 5, "2026-09-30")
        self.assertTrue(self.store.has_uploaded("U2"))
        self.assertFalse(self.store.has_uploaded("U3"))


class UploadReportTests(BotCase):
    """Every photo posted in a group is reported to the administrators: recognised, failed, or not a roster."""
    friends = {"U1", "U2", "U3", "U9"}
    settings = {"admins": ("U9",), "notify": False, "public_base": "https://bot.example"}

    def setUp(self):
        super().setUp()
        self.register("U1", "林小明")
        self.quiet()

    def quiet(self):
        self.transport.pushes.clear(); self.transport.raw_pushes.clear(); self.transport.replies.clear()

    def failing(self, error):
        def analyze(*args):
            raise error
        self.analyzer.analyze = analyze

    def report(self, user="U9", messages=1):
        """The card of the one push an administrator got for a photo (a recognised roster's has the 勤務人員 crop of
        the name table under it, an undated one's the photo above it, in the same push)."""
        reports = [sent for to, sent in self.transport.raw_pushes if to == user]
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(reports[0]), messages)
        return next(message for message in reports[0] if message["type"] == "flex")

    def test_a_recognised_roster_tells_its_date_and_how_many_names_were_matched(self):
        from all_staff.bot import flex_size
        event = image_event("U2", "Cgroup")
        event["timestamp"] = int(datetime(2026, 10, 1, 7, 5, tzinfo=TAIPEI).timestamp() * 1000)
        self.bot.handle(event)
        self.assertEqual([to for to, _ in self.transport.pushes], ["U9"])   # notifications are off: nobody else
        self.assertEqual(self.transport.replies, [])                           # and nothing in the group
        card = self.report(messages=2)
        self.assertEqual(self.transport.raw_pushes[0][1][1],                   # the 勤務人員 crop, under the card
                         {"type": "image", "originalContentUrl": "https://bot.example/runs/run-1/staff-table-v1.jpg",
                          "previewImageUrl": "https://bot.example/runs/run-1/staff-table-v1-preview.jpg"})
        from test_all_staff_blank_names import report_rows
        texts = flex_texts(card["contents"])
        self.assertEqual(texts[:2], ["✅ 群組班表辨識成功", "華華｜10/1 07:05 傳到群組"])   # their LINE name
        self.assertEqual(report_rows(card), {"📅 班表日期": "10/1（四）", "👥 有勤務": "4 個代號",
                                             "👤 姓名配對": "1／3（2 個沒讀出來）",   # only 林小明 is a known name here
                                             "⚠️ 沒有這個人": "1 個（甲）",          # no fixed code is set here
                                             "❓ 信心較低": "1 格"})                  # read before blank cells: no ⬜
        self.assertIn("📷 下方是勤務人員（姓名代碼對照表）的原圖，可點開放大", texts)
        picker = next(node["action"] for node in flex_nodes(card) if node.get("action", {}).get("mode") == "date")
        self.assertEqual(picker, {"type": "datetimepicker", "label": "改這張的日期", "data": "redate|run-1",
                                  "mode": "date", "initial": "2026-10-01",   # bound to this photo, not the last one
                                  "min": "2026-08-02", "max": "2026-10-22"})   # near the day it was sent
        self.assertIn("📅 日期不對？點這裡改這張的日期", texts)
        self.assertEqual(card["contents"]["footer"]["contents"][0]["action"]["data"], "roster|run-1")
        self.assertTrue(card["altText"].startswith("✅ 群組班表辨識成功｜華華 10/1 07:05｜班表日期 10/1（四）"))
        self.assertLess(flex_size(card["contents"]), 10_000)

    def test_every_name_matched_and_the_versions_of_a_day(self):
        from all_staff.bot import upload_report
        roster = {"run_id": "r", "roster_date": "2026-10-01"}
        names = {"72": "林小明", "甲": "陳小華", "26": "王大同", "5": "某某某"}
        card = upload_report("ok", "華華", 0, roster=roster, result=RESULT, names=names, replaced=True)
        texts = flex_texts(card["contents"])
        self.assertIn("4／4，全部對到", texts)
        self.assertIn("同一天的更新版", texts)
        self.assertNotIn("🔍 姓名沒讀出來", texts)
        self.assertNotIn("footer", card["contents"])
        filed = flex_texts(upload_report("ok", "華華", 0, roster=roster, result=RESULT, names=names, replaced=True,
                                         superseded=True)["contents"])
        self.assertIn("同一天已有更晚的，只存檔", filed)
        self.assertNotIn("同一天的更新版", filed)

    def picked(self, data, day, user="U9"):
        event = postback_event(user, data)
        event["postback"]["params"] = {"date": day}
        self.bot.handle(event)

    def test_the_date_picker_changes_that_photo_even_after_a_later_one(self):
        self.bot.analyzer = SequenceAnalyzer({"a": "2026-10-02", "b": "2026-10-03"})   # a is really 10/1
        for message, minute in (("a", 5), ("b", 6)):
            event = image_event("U2", "Cgroup")
            event["message"]["id"] = message
            event["timestamp"] = int(datetime(2026, 10, 1, 7, minute, tzinfo=TAIPEI).timestamp() * 1000)
            self.bot.handle(event)
        self.picked("redate|run-a", "2026-10-01", user="U1")   # not an administrator: nothing happens
        self.assertEqual(self.store.roster_by_run("run-a")["roster_date"], "2026-10-02")
        self.picked("redate|run-a", "2026-10-01")
        self.assertIn("10/2（五）班表的日期已改為 10/1（四）", self.reply())
        self.assertIn("再點一次「📅 改這張的日期」改回來", self.reply())   # on the report, or on that day's duty card
        self.assertEqual(self.store.roster_by_run("run-a")["roster_date"], "2026-10-01")
        self.assertEqual(self.store.roster_by_run("run-b")["roster_date"], "2026-10-03")   # the later one untouched
        count = len(self.transport.replies)
        self.picked("redate|run-a", "not a date")
        self.picked("redate|missing", "2026-10-01")
        self.assertEqual(len(self.transport.replies), count)

    def test_the_date_picker_on_a_held_photo_dates_and_releases_it(self):
        self.store.add_roster("Cgroup", "held", "U2", 4, None, held=True)
        self.analyzer.data["run"]["id"] = "held"
        self.picked("redate|held", "2026-10-05")
        self.assertIn("日期已設為 10/5（一）", self.reply())
        self.assertEqual((self.store.roster_by_run("held")["roster_date"], self.store.roster_by_run("held")["held"]),
                         ("2026-10-05", 0))

    def test_unmatched_codes_are_listed_and_a_registered_uploader_is_named(self):
        self.register("U2", "王大同")
        self.analyzer.data["names"] = {"72": ["林小明"]}
        self.quiet()
        self.bot.handle(image_event("U2", "Cgroup"))
        texts = flex_texts(self.report(messages=2)["contents"])
        self.assertTrue(texts[1].startswith("王大同｜"))
        self.assertIn("1／3（2 個沒讀出來）", texts)
        self.assertEqual(texts[-1], "🖼️ 查看整張班表")
        unread = texts[texts.index("🔍 姓名沒讀出來") + 1:]
        self.assertEqual(unread[:2], ["5：照片上的字讀不出來", "26：照片上的字讀不出來"])   # 「72」 alone was read

    def test_a_failure_and_a_photo_that_is_not_a_roster_are_reported_with_the_reason(self):
        from all_staff.demo import DemoError, NotARoster
        self.failing(DemoError("無法從照片切出勤務格：x"))
        self.bot.handle(image_event("U3", "Cgroup"))
        self.assertEqual(sorted(to for to, _ in self.transport.pushes), ["U3", "U9"])   # the uploader too
        texts = flex_texts(self.report()["contents"])
        self.assertEqual(texts[0], "⚠️ 群組班表辨識失敗")
        self.assertIn("無法從照片切出勤務格：x", texts)
        self.assertIn("已私訊告知", texts)
        self.assertNotIn("footer", self.report()["contents"])
        self.quiet()
        self.failing(NotARoster("這張圖片找不到班表的表格框線，看起來不是班表。"))
        self.bot.handle(image_event("U3", "Cgroup"))       # someone who never posted a roster: not told
        self.assertEqual([to for to, _ in self.transport.pushes], ["U9"])
        texts = flex_texts(self.report()["contents"])
        self.assertEqual(texts[0], "🚫 不像班表，已略過")
        self.assertIn("這張圖片找不到班表的表格框線，看起來不是班表。", texts)
        self.assertIn("沒有私訊", texts)
        self.assertIn("一般照片可以忽略這則", texts[-1])

    def test_an_unexpected_error_shows_the_administrator_what_it_was(self):
        self.failing(RuntimeError("disk full"))
        self.bot.handle(image_event("U3", "Cgroup"))
        self.assertIn("處理時發生錯誤（RuntimeError: disk full）", flex_texts(self.report()["contents"]))
        self.assertIn("處理時發生錯誤，請稍後再試", self.transport.pushes[0][1][0] if self.transport.pushes[0][0] == "U3"
                      else self.transport.pushes[1][1][0])   # the uploader gets the plain wording

    def test_an_administrator_who_posts_gets_the_report_only(self):
        from all_staff.demo import DemoError
        self.failing(DemoError("無法從照片切出勤務格：x"))
        self.bot.handle(image_event("U9", "Cgroup"))
        self.assertEqual([to for to, _ in self.transport.pushes], ["U9"])
        self.assertIn("沒有私訊", flex_texts(self.report()["contents"]))

    def test_private_photos_and_bots_without_administrators_report_nothing(self):
        self.bot.handle(image_event("U2"))                  # a private upload: answered, never reported
        self.assertEqual(self.transport.pushes, [])
        self.bot.settings.admins = ()
        self.bot.handle(image_event("U2", "Cgroup"))
        self.assertEqual(self.transport.pushes, [])

    def test_an_undated_roster_only_brings_the_date_question(self):
        self.analyzer.data["run"]["roster_date"] = None
        self.bot.handle(image_event("U2", "Cgroup"))
        card = self.report(messages=2)                      # the photo, then its date question
        self.assertIn("⚠ 班表日期讀不到", flex_texts(card["contents"]))   # the question, not a second report


class RosterChoiceTests(BotCase):
    """The three problems found in production: wrong roster chosen, dates, claims."""

    def setUp(self):
        super().setUp()
        self.register("U1", "林小明")
        self.store.add_roster("Cgroup", "old-group", "U2", 3, "2026-09-29")

    def test_a_newer_private_photo_beats_an_older_group_roster_for_its_sender_only(self):
        self.store.add_roster("U1", "mine", "U1", 3, "2026-10-01")
        self.assertEqual(self.store.roster_for_user("U1")["run_id"], "mine")
        self.assertEqual(self.store.roster_for_user("U2")["run_id"], "old-group")
        self.assertEqual(self.store.team_roster()["run_id"], "old-group")

    def test_date_fix_targets_the_photo_just_sent_and_keeps_the_read_date(self):
        self.store.add_roster("U1", "mine", "U1", 3, None)          # header date unreadable
        reply = self.ask("日期 10/1")
        self.assertIn("你私訊", reply)
        self.assertEqual(self.store.roster_by_run("mine")["roster_date"], "2026-10-01")
        self.assertEqual(self.store.roster_by_run("old-group")["roster_date"], "2026-09-29")   # untouched
        self.assertIsNone(self.store.roster_by_run("mine")["read_date"])

    def test_new_group_upload_does_not_redirect_my_date_correction(self):
        self.store.add_roster("U1", "mine", "U1", 3, None)
        self.store.add_roster("Cgroup", "someone-elses", "U2", 3, "2026-10-02")
        self.ask("日期 10/1")
        self.assertEqual(self.store.roster_by_run("mine")["roster_date"], "2026-10-01")
        self.assertEqual(self.store.roster_by_run("someone-elses")["roster_date"], "2026-10-02")

    def test_changing_a_read_date_warns_and_can_be_undone(self):
        reply = self.ask("日期 10/1")
        self.assertIn("群組", reply)
        self.assertIn("照片上辨識到的日期是 9/29（二）", reply)
        self.assertEqual(self.store.roster_by_run("old-group")["read_date"], "2026-09-29")
        self.ask("日期 9/29")
        self.assertEqual(self.store.roster_by_run("old-group")["roster_date"], "2026-09-29")

    def test_claim_is_refused_when_the_table_names_someone_else(self):
        self.register("U3", "張小美")
        reply = self.ask("我是 72", "U3")
        self.assertIn("代號 72 是「林小明」", reply)
        self.assertEqual(self.store.claims("old-group"), {})

    def test_claim_on_a_code_without_duties_warns(self):
        self.register("U3", "張小美")
        reply = self.ask("我是 己", "U3")
        self.assertIn("沒有辨識到代號 己 的勤務", reply)
        self.assertIn("9/29（二）班表", reply)

    def test_names_are_re_read_when_stored_by_an_older_version(self):
        calls = []
        def ensure(run_id):
            calls.append(run_id)
            return {"names": {"甲": ["林小明"]}, "names_version": __import__("all_staff.name_table").name_table.NAMES_VERSION}
        self.analyzer.ensure_names = ensure
        with self.at(date(2026, 9, 29)):
            self.assertIn("代號 甲", self.ask("我的班表"))
        self.assertEqual(calls, ["old-group"])

    def test_failed_reread_does_not_use_the_old_name_to_code_mapping(self):
        def fail(run_id):
            raise OSError("photo unavailable")
        self.analyzer.ensure_names = fail
        with self.at(date(2026, 9, 29)):
            self.assertIn("今天的班表上找不到你", self.ask("我的班表"))
        self.assertNotIn("代號 72", self.reply())
        self.assertIn("沒有找到", self.ask("我的班表 9/29"))
        self.assertNotIn("林小明（72）", self.reply())

    def test_changing_name_drops_old_manual_code_claims(self):
        self.store.claim("old-group", "U1", "甲")
        self.store.set_notify("U1", True)
        self.ask("設定 王大明")
        self.assertEqual(self.store.claims("old-group"), {})
        self.assertTrue(self.store.user("U1")["notify"])

    def test_unregistering_drops_old_manual_code_claims(self):
        self.store.claim("old-group", "U1", "甲")
        self.ask("解除")
        self.assertEqual(self.store.claims_for_run("old-group"), [])

    def test_old_manual_claim_does_not_overwrite_someone_elses_name(self):
        self.register("U2", "陳小華")
        self.store.claim("old-group", "U2", "72")
        roster = self.store.roster_by_run("old-group")
        names = self.bot.names_for(roster, self.analyzer.data)
        self.assertEqual(names["72"], "林小明")
        self.assertEqual(names["甲"], "陳小華")
        self.assertEqual(self.store.claims_for_run("old-group"), [("U2", "72")])

    def test_missing_slot_names_are_reported_even_with_a_large_directory(self):
        reply = who_is_on(RESULT, 4, "intl", {"72": "林小明", "甲": "陳小華", "5": "張小美"},
                          self.store.team_roster())
        self.assertIn("只有代號的人", reply)


class StoredNameRereadTests(unittest.TestCase):
    def test_concurrent_queries_reread_once_and_preserve_the_result(self):
        import json
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from all_staff.demo import DemoApp
        from all_staff.name_table import NAMES_VERSION
        with tempfile.TemporaryDirectory() as folder:
            runs = Path(folder)
            run = runs / "test"
            (run / "upload").mkdir(parents=True)
            (run / "upload/photo.jpg").write_bytes(b"test")
            path = run / "result.json"
            path.write_text(json.dumps({"names": {"乙": ["蕭志明"]}, "people": []}))
            app = DemoApp.__new__(DemoApp)
            app.runs, app.lock = runs, threading.Lock()
            calls = []
            def read(photo):
                calls.append(photo)
                time.sleep(.02)
                return {"己": ["蕭志明"]}, ["26"]
            app._name_table = read
            ready = threading.Barrier(4)
            def query(_):
                ready.wait()
                return app.ensure_names("test")
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(query, range(4)))
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(r["names"] == {"己": ["蕭志明"]} for r in results))
            self.assertTrue(all(r["names_blank"] == ["26"] for r in results))   # also from the run read by another
            saved = json.loads(path.read_text())
            self.assertEqual(saved["names_version"], NAMES_VERSION)
            self.assertEqual(saved["names_blank"], ["26"])
            self.assertEqual(saved["people"], [])
            self.assertEqual(list(run.glob("*.tmp")), [])


class DateTests(BotCase):
    def test_printed_date_is_shown_not_the_upload_day(self):
        self.register("U1", "林小明")
        self.bot.handle(image_event("U1"))
        texts = self.transport.replies[-1][1]
        self.assertTrue(texts[0].startswith("✅ 10/1（四） 班表辨識完成"))
        self.assertIn("林小明（72）　10/1（四）", texts[1])

    def test_unknown_date_asks_for_it_and_can_be_set(self):
        self.analyzer.data["run"]["roster_date"] = None
        self.bot.handle(image_event("U1"))
        self.assertIn("讀不到班表上的日期", self.transport.replies[-1][1][0])
        self.assertIn("（日期未辨識）", self.ask("查 72"))
        self.assertIn("10/2（五）", self.ask("日期 10/2"))
        self.assertIn("代號 72　10/2（五）", self.ask("查 72"))

    def test_parse_day(self):
        from datetime import date
        from all_staff.bot import parse_day
        self.assertEqual(parse_day("10/1", date(2026, 9, 30)), date(2026, 10, 1))
        self.assertEqual(parse_day("1/2", date(2026, 12, 30)), date(2027, 1, 2))
        self.assertEqual(parse_day("2026年10月1日", date(2026, 9, 30)), date(2026, 10, 1))
        self.assertIsNone(parse_day("13/40", date(2026, 9, 30)))


class DateQueryTests(BotCase):
    def setUp(self):
        super().setUp()
        self.register("U1", "林小明")
        self.store.add_roster("Cgroup", "sep30", "U2", 3, "2026-09-30")
        self.store.add_roster("Cgroup", "oct1", "U2", 3, "2026-10-01")

    def test_split_day(self):
        from datetime import date
        from all_staff.bot import split_day
        near = date(2026, 10, 1)
        self.assertEqual(split_day("我的班表 9/30", near), ("我的班表", date(2026, 9, 30)))
        self.assertEqual(split_day("9/30的班表", near), ("班表", date(2026, 9, 30)))
        self.assertEqual(split_day("明天班表", near), ("班表", date(2026, 10, 2)))
        self.assertEqual(split_day("查 72 9月30日", near), ("查 72", date(2026, 9, 30)))
        self.assertEqual(split_day("10/1", near), ("", date(2026, 10, 1)))
        self.assertEqual(split_day("設定 72", near), ("設定 72", None))

    def test_without_today_or_tomorrow_the_card_offers_the_latest_and_its_other_dates(self):
        with self.at(date(2026, 10, 5)):
            reply = self.ask("我的班表")
        self.assertIn("今天的班表還沒上傳", reply)
        self.assertIn("明天的班表還沒上傳", reply)
        self.assertIn("👉 先看最新的一張：10/1（四）的班表", reply)
        self.assertNotIn("quickReply", self.transport.raw[-1][-1])
        latest = next(node["action"] for node in flex_nodes(self.transport.raw[-1][0])
                      if node.get("action", {}).get("data") == "mine|oct1")   # the 👉 line itself is tappable
        self.assertEqual(latest["displayText"], "我的班表 10/1")
        self.bot.handle(postback_event("U1", latest["data"]))   # that day's duty card, as 「我的班表」 used to be
        reply = self.reply()
        self.assertIn("林小明（72）　10/1（四）", reply)
        self.assertEqual(date_chips(self.transport.raw[-1][0]), ["9/30"])   # a tap on the chip sends the date
        self.assertIn("📆 其他日期", reply)
        self.assertNotIn("輸入「我的班表", reply)

    def test_today_button_uses_today_even_if_tomorrow_or_an_old_repost_is_newer(self):
        self.store.add_roster("Cgroup", "oct2", "U2", 4, "2026-10-02")
        self.store.add_roster("Cgroup", "oct3", "U2", 5, "2026-10-03")
        self.store.add_roster("Cgroup", "old-repost", "U2", 6, "2026-10-01")
        with self.at(date(2026, 10, 2)):
            for command in ("今天班表", "明天班表", "我的班表"):   # the old buttons' words: the same card
                reply = self.ask(command)
                self.assertIn("☀️ 今天 10/2（五）", reply, command)
                self.assertIn("🌙 明天 10/3（六）", reply, command)
                self.assertFalse(any("quickReply" in message for message in self.transport.raw[-1]), command)
            self.assertIn("林小明（72）　10/2（五）", self.ask("今天"))   # a date: that day's duty card
            self.assertIn("林小明（72）　10/3（六）", self.ask("明天"))
            self.assertIn("林小明（72）　10/1（四）", self.ask("10/1"))

    def test_today_button_reports_missing_today_instead_of_using_another_day(self):
        with self.at(date(2026, 10, 2)):
            self.assertIn("今天的班表還沒上傳", self.ask("今天班表"))
            self.assertNotIn("☀️ 今天 10/1", self.reply())
            self.assertIn("沒有 10/2（五）的班表", self.ask("今天"))

    def test_specific_date_for_me_and_for_a_code(self):
        for command in ("我的班表 9/30", "9/30", "9/30 班表", "班表 9月30日"):
            self.assertIn("林小明（72）　9/30（三）", self.ask(command), command)
        self.ask("9/30")
        self.assertEqual(date_chips(self.transport.raw[-1][0]), ["10/1"])
        self.assertIn("代號 甲　9/30（三）", self.ask("查 甲 9/30"))
        self.assertEqual(date_chips(self.transport.raw[-1][0]), [])     # codes change daily: no other day to follow
        self.assertIn("代號 甲　10/1（四）", self.ask("甲 10/1"))

    def test_reupload_of_the_same_day_replaces_it(self):
        from datetime import date
        self.store.add_roster("Cgroup", "sep30-fixed", "U2", 3, "2026-09-30")
        self.assertEqual(self.store.roster_on("U1", date(2026, 9, 30))["run_id"], "sep30-fixed")
        self.assertEqual(self.store.roster_for_user("U1")["run_id"], "oct1")   # an old day re-posted stays old

    def test_missing_date_lists_what_exists(self):
        reply = self.ask("我的班表 9/1")
        self.assertIn("沒有 9/1（二）的班表", reply)
        self.assertIn("可查的日期：10/1（四）、9/30（三）", reply)

    def test_ordinary_sentences_and_date_fix_are_not_queries(self):
        self.assertIn("看不懂", self.ask("今天好熱"))
        self.ask("日期 10/2")
        self.assertEqual(self.store.team_roster()["roster_date"], "2026-10-02")


class BrowseDateTests(BotCase):
    settings = {"public_base": "https://bot.example"}

    def setUp(self):
        super().setUp()
        self.store.add_roster("Cgroup", "today-run", "U2", 1, "2026-10-03")
        self.store.add_roster("Cgroup", "tomorrow-old", "U2", 2, "2026-10-04")
        self.store.add_roster("Cgroup", "tomorrow-run", "U2", 3, "2026-10-04")

    def test_time_picker_defaults_to_today_and_switches_to_latest_tomorrow(self):
        from unittest.mock import patch
        with patch("all_staff.bot.today", return_value=date(2026, 10, 3)):
            self.ask("查看時段")
            picker = self.transport.raw[-1][0]
            self.assertIn("10/3（六）班表", picker["altText"])
            switches = [item["action"] for item in picker["quickReply"]["items"]]
            self.assertEqual([action["data"] for action in switches],
                             ["slots|today-run", "slots|tomorrow-run"])
            self.bot.handle(postback_event("U1", switches[1]["data"]))
            self.assertIn("10/4（日）班表", self.transport.raw[-1][0]["altText"])
            self.bot.handle(postback_event("U1", "area|tomorrow-run|12|intl"))
            answer = self.transport.raw[-1][-1]
            actions = [item["action"] for item in answer["quickReply"]["items"]]
            self.assertIn("area|today-run|12|intl", [action["data"] for action in actions])
            self.bot.handle(postback_event("U1", "area|today-run|12|intl"))
            self.assertIn("10/3（六）", self.transport.raw[-1][0]["altText"])

    def test_staff_and_leave_lists_keep_their_view_when_switching_days(self):
        from unittest.mock import patch
        with patch("all_staff.bot.today", return_value=date(2026, 10, 3)):
            for command, switch, expected in (
                ("勤務人員", "staff|tomorrow-run", "10/4（日）班表"),
                ("休假人員", "leave|tomorrow-run", "10/4（日）班表"),
                ("輪休人員", "leave|tomorrow-run|rest", "10/4（日）班表"),
                ("請假人員", "leave|tomorrow-run|leave", "10/4（日）班表"),
            ):
                with self.subTest(command=command):
                    self.ask(command)
                    self.assertIn("10/3（六）班表", self.transport.raw[-1][0]["altText"])
                    actions = [item["action"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
                    self.assertIn(switch, [action["data"] for action in actions])
                    self.bot.handle(postback_event("U1", switch))
                    self.assertIn(expected, self.transport.raw[-1][0]["altText"])

    def test_private_roster_image_cannot_be_opened_by_another_user(self):
        self.store.add_roster("U1", "private-run", "U1", 4, "2026-10-03")
        before = len(self.transport.replies)
        self.bot.handle(postback_event("U2", "roster|private-run"))
        self.assertEqual(len(self.transport.replies), before)

    def test_plain_person_lookup_stays_on_the_roster_just_viewed(self):
        from unittest.mock import patch
        with patch("all_staff.bot.today", return_value=date(2026, 10, 3)):
            self.ask("休假人員 10/3")
            self.bot.handle(postback_event("U1", "roster|today-run"))
            self.ask("查 31")
            self.assertIn("10/3（六）", self.transport.raw[-1][0]["altText"])
            self.ask("查 31 10/4")
            self.assertIn("10/4（日）", self.transport.raw[-1][0]["altText"])
            self.ask("查 72")
            self.assertIn("10/4（日）", self.transport.raw[-1][0]["altText"])
            with self.store.lock, self.store.db:
                self.store.db.execute("update viewed_rosters set viewed_at=0 where user_id='U1'")
            self.ask("查 31")
            self.assertIn("10/3（六）", self.transport.raw[-1][0]["altText"])


class NextSlotAndWorkTests(BotCase):
    def setUp(self):
        super().setUp()
        self.register("U1", "林小明")
        self.store.add_roster("Cgroup", "today-run", "U2", 1, "2026-10-04")
        self.store.add_roster("Cgroup", "tomorrow-run", "U2", 2, "2026-10-05")

    def my_day(self, hour, minute=30, command="下個時段"):
        """「下個時段」 (now 「我的班表」's card) at ``hour``:``minute`` on 10/4: the reply's words and the card."""
        with self.at(datetime(2026, 10, 4, hour, minute, tzinfo=TAIPEI)):
            reply = self.ask(command)
        return reply, self.transport.raw[-1][0]

    def test_next_slot_shows_next_hour_even_when_the_work_is_the_same(self):
        reply, card = self.my_day(5)
        self.assertIn("⏭️ 下個小時 06–07：國際線", card["altText"])
        self.assertIn("▶️ 現在 05–06：國際線", reply)
        panel = card["contents"]["header"]["contents"][-1]["contents"]
        self.assertEqual([node["text"] for node in panel[:2]], ["⏭️ 下個小時 06–07", "國際線"])
        self.assertEqual((panel[1]["size"], panel[1]["weight"]), ("xxl", "bold"))
        now = next(node for node in flex_nodes(card["contents"]["body"]) if node.get("borderColor") == "#C2410C")
        self.assertEqual(flex_texts(now)[:3], ["05–07", "國際線", "▶ 現在"])   # the hour under way, ringed
        self.assertIn("⏭️ 下個小時 07–08：國內一線 ？", self.my_day(6)[1]["altText"])
        self.analyzer.data["people"][0]["slots"].append(slot(8, 9, "國際線備勤"))
        reply, card = self.my_day(8)
        self.assertIn("⏭️ 下個小時 09–10：國際備勤", card["altText"])
        self.assertNotIn("▶️ 現在", reply)

    def test_next_slot_moves_to_tomorrow_after_last_duty(self):
        reply, card = self.my_day(21, 0)
        self.assertIn("⏭️ 下個小時 22–23：沒有勤務｜👉 明天第一段 05–07：國際線", card["altText"])
        self.assertIn("🌙 明天 10/5（一）", reply)
        self.assertIn("✓ 05–07", reply)
        self.assertIn("✓ 07–08", reply)

    def test_next_slot_explains_missing_name_and_roster(self):
        self.analyzer.data["names"] = {}
        reply, card = self.my_day(21, 0)
        self.assertIn("今天的班表上找不到你", reply)
        self.assertIn("明天的班表上找不到你", reply)
        self.assertIn("✏️ 有上班就傳「我是 代號 10/4」", reply)
        self.assertNotIn("quickReply", card)                   # the day's tile is the way to claim
        fills = [node["action"]["fillInText"] for node in flex_nodes(card)
                 if node.get("action", {}).get("inputOption") == "openKeyboard"]
        self.assertEqual(fills, ["我是 10/4 ", "我是 10/5 "])
        self.store.db.execute("delete from rosters")
        self.store.db.commit()
        self.assertIn("目前還沒有班表", self.my_day(21, 0)[0])

    def test_last_current_hour_remains_formatted_when_no_next_hour_exists(self):
        self.store.db.execute("delete from rosters where run_id='tomorrow-run'")
        self.store.db.commit()
        reply, card = self.my_day(7)
        self.assertIn("⏭️ 下個小時 08–09：下班 🎉｜▶️ 現在 07–08：國內一線 ？｜🌙 明天的班表還沒上傳", card["altText"])
        now = next(node for node in flex_nodes(card["contents"]["body"]) if node.get("borderColor") == "#C2410C")
        self.assertEqual(flex_texts(now)[:3], ["07–08", "國內一線 ？", "▶ 現在"])
        self.assertIn("明天的班表還沒上傳", reply)

    def test_work_picker_and_whole_day_list_keep_roster_date(self):
        from all_staff.bot import work_key
        from unittest.mock import patch
        selected = work_key("國際線")
        with patch("all_staff.bot.today", return_value=date(2026, 10, 4)):
            self.ask("查看工作")
            picker = self.transport.raw[-1][0]
            self.assertIn("10/4（日）班表", picker["altText"])
            self.assertIn("國際線", flex_texts(picker["contents"]))
            self.assertEqual([item["action"]["data"] for item in picker["quickReply"]["items"]],
                             ["works|today-run", "works|tomorrow-run"])
            self.bot.handle(postback_event("U1", f"work|today-run|{selected}"))
            card = self.transport.raw[-1][0]
            contents = flex_texts(card["contents"])
            self.assertIn("05–06", contents)
            self.assertIn("06–07", contents)
            self.assertIn("05–06 26、72 林小明", card["altText"])   # an hour's people in code order
            self.assertIn("72 林小明", contents)
            actions = [item["action"]["data"] for item in card["quickReply"]["items"]]
            self.assertIn(f"work|tomorrow-run|{selected}", actions)
            self.bot.handle(postback_event("U1", actions[-1]))
            self.assertIn("10/5（一）班表", self.transport.raw[-1][0]["altText"])

    def test_unreadable_work_header_is_a_selectable_column_and_private_runs_stay_private(self):
        from all_staff.bot import work_key, work_name, work_options
        self.assertEqual(work_name({"column": 31, "duty": "c31"}), "第31欄（未辨識）")
        self.analyzer.data["people"][0]["slots"][0]["duty"] = "c31"
        self.assertIn("第2欄（未辨識）", [item[0] for item in work_options(self.analyzer.data)])
        self.store.add_roster("U1", "private-run", "U1", 3, "2026-10-04")
        before = len(self.transport.replies)
        self.bot.handle(postback_event("U2", "works|private-run"))
        self.bot.handle(postback_event("U2", f"work|private-run|{work_key('國際線')}"))
        self.assertEqual(len(self.transport.replies), before)

    def test_work_missing_on_the_other_day_offers_a_new_choice(self):
        from all_staff.bot import work_key
        self.analyzer.data["people"] = []
        self.bot.handle(postback_event("U1", f"work|tomorrow-run|{work_key('國際線')}"))
        answer = self.transport.raw[-1][0]
        self.assertIn("沒有這項工作", answer["text"])
        self.assertEqual(answer["quickReply"]["items"][0]["action"]["data"], "works|tomorrow-run")


class HelpAndMenuTests(BotCase):
    def test_help_command(self):
        from all_staff import rich_menu
        for command in ("用法說明", "說明", "使用說明"):
            reply = self.ask(command)
            message = self.transport.raw[-1][0]
            self.assertEqual(message["type"], "flex")
            self.assertTrue(message["altText"].startswith("📋 用法說明"))
            for phrase in ("設定 你的姓名", "我的班表", "下個小時", "查看時段", "查看工作", "查詢人員", "勤務人員",
                           "休假人員", "人流預測", "本月統計", "我的設定", "意見回饋", "通知 開"):
                self.assertIn(phrase, reply)
            self.assertNotIn("我是 代號", reply)
            self.assertLess(len(message["altText"]), 300)
            self.assertEqual(message["contents"]["type"], "carousel")
            bubbles = message["contents"]["contents"]
            self.assertEqual(len(bubbles), len(rich_menu.GROUPS))
            labels = []
            for bubble, (section, color) in zip(bubbles, rich_menu.GROUPS):
                self.assertIn(section, bubble["header"]["contents"][0]["text"])
                self.assertEqual(bubble["header"]["backgroundColor"], color)
                commands = [n for n in bubble["body"]["contents"] if n.get("type") == "box"]
                self.assertTrue(all(n["contents"][0]["weight"] == "bold" and
                                    n["contents"][0]["decoration"] == "underline" for n in commands))
                labels.extend(n["contents"][0]["text"] for n in commands)
            self.assertEqual(labels, [label for label, _, _ in rich_menu.BUTTONS])
            self.assertNotIn("用法說明", labels)                               # no button for it: typed instead
            self.assertTrue(flex_texts(bubbles[-1]["body"])[-1].endswith("想再看這四張說明卡，隨時傳「說明」。"))
            self.assertEqual(bubbles[-1]["footer"]["contents"][0]["action"]["text"], "進階說明")

    def test_advanced_instructions_remain_available(self):
        reply = self.ask("進階說明")
        for phrase in ("我是 代號", "日期 月/日", "查 姓名", "解除", "📅 改這張的日期"):
            self.assertIn(phrase, reply)
        self.assertNotIn("日期 10/1", reply)   # an example date was typed in as it was, onto the newest photo

    def test_rich_menu_buttons_match_bot_commands(self):
        from all_staff import rich_menu
        self.register("U1", "林小明")
        self.bot.handle(image_event("U2", "Cgroup"))
        menu = rich_menu.menu()
        covered = sum(a["bounds"]["width"] * a["bounds"]["height"] for a in menu["areas"])
        self.assertEqual(covered, (rich_menu.WIDTH - rich_menu.CATEGORY_WIDTH) * rich_menu.HEIGHT)
        self.assertEqual(rich_menu.ROW_COUNTS, (1, 3, 3, 3))
        self.assertEqual(len(rich_menu.ROW_FONTS), len(rich_menu.ROW_COUNTS))
        self.assertEqual([label for label, _ in rich_menu.GROUPS], ['班表', '查詢', '人員', '更多'])
        self.assertTrue(all(a['bounds']['x'] >= rich_menu.CATEGORY_WIDTH for a in menu['areas']))
        for index, area in enumerate(menu['areas']):
            a = area['bounds']
            self.assertLessEqual(a['x']+a['width'], rich_menu.WIDTH)
            self.assertLessEqual(a['y']+a['height'], rich_menu.HEIGHT)
            for other in menu['areas'][index+1:]:
                b = other['bounds']
                self.assertTrue(a['x']+a['width'] <= b['x'] or b['x']+b['width'] <= a['x'] or
                                a['y']+a['height'] <= b['y'] or b['y']+b['height'] <= a['y'])
        labels = [a["action"]["label"] for a in menu["areas"]]
        mine = menu["areas"][0]   # 今天班表／明天班表／下個時段 became one button across the 班表 row
        self.assertEqual((mine["action"]["label"], mine["action"]["text"]), ("我的班表", "我的班表"))
        self.assertEqual(mine["bounds"], {"x": rich_menu.CATEGORY_WIDTH, "y": 0,
                                          "width": rich_menu.WIDTH - rich_menu.CATEGORY_WIDTH,
                                          "height": rich_menu.HEIGHT // 4})
        self.assertNotIn("今天班表", labels); self.assertNotIn("下個時段", labels)
        self.assertIn("查看時段", labels); self.assertNotIn("設定姓名", labels)   # a one-off: 我的設定 offers it
        self.assertIn("查看工作", labels)
        self.assertIn("勤務人員", labels)
        self.assertEqual(len(labels), 10)
        self.assertLessEqual(len(labels), 20)                                   # LINE's limit for one menu
        self.assertIn("休假人員", labels)
        self.assertEqual(labels[1:4], ["查看時段", "查看工作", "查詢人員"])   # the 查詢 row
        self.assertEqual(labels[4:7], ["勤務人員", "休假人員", "人流預測"])   # the 人員 row
        self.assertEqual(labels[7:], ["本月統計", "我的設定", "意見回饋"])   # 更多: three, no 用法說明 any more
        self.assertEqual([a["bounds"]["width"] for a in menu["areas"][7:]],
                         [a["bounds"]["width"] for a in menu["areas"][1:4]])
        for label, prefix in (("查詢人員", "查 "), ("意見回饋", "意見 ")):
            action = next(a["action"] for a in menu["areas"] if a["action"]["label"] == label)
            self.assertEqual(action["type"], "postback")
            self.assertEqual(action["fillInText"], prefix)
        for area in menu["areas"]:
            action = area["action"]
            count = len(self.transport.replies)
            if action["type"] == "message":
                self.assertNotIn("看不懂", self.ask(action["text"]), action["text"])
            else:                                                              # keyboard pre-fill: bot stays quiet
                self.assertEqual(action["inputOption"], "openKeyboard")
                self.bot.handle(postback_event("U1", action["data"]))
                self.assertEqual(len(self.transport.replies), count)


class HeaderDateTests(unittest.TestCase):
    def test_full_and_partial_readings(self):
        from datetime import date
        from all_staff.header_date import parse
        self.assertEqual(parse("勤 務 分 配 表 2026 年9月11日星期五 主管"), [(date(2026, 9, 11), True)])
        self.assertEqual(parse("表 2026年9月19日星期 吧"), [(date(2026, 9, 19), False)])   # weekday unread
        self.assertEqual(parse("2026年9月13日星期六"), [])                                  # weekday disagrees
        low, high = date(2026, 9, 7), date(2026, 10, 5)
        self.assertEqual(parse("2026和年0月15日星基二 主管", low, high), [(date(2026, 9, 15), True)])
        self.assertEqual(parse("24日星期四 主", low, high), [(date(2026, 9, 24), True)])

    def test_a_reading_that_runs_out_of_time_does_not_lose_the_date(self):
        """On a busy server one Tesseract call can run past its limit; the other layout still reads the date."""
        import subprocess
        from datetime import date
        from pathlib import Path
        from unittest import mock
        from PIL import Image
        from all_staff import header_date
        calls = []

        def run(command, **kwargs):
            calls.append(kwargs.get("env") or {})
            if command[command.index("--psm") + 1] == "6":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(command, 0, "勤 務 分 配 表 2026年10月16日星期五 主管".encode(), b"")

        strip = Image.new("L", (600, 120), 255)
        with mock.patch.object(header_date.shutil, "which", return_value="/usr/bin/tesseract"), \
                mock.patch.object(header_date, "header_strip", return_value=strip), \
                mock.patch.object(header_date.subprocess, "run", side_effect=run), \
                mock.patch("builtins.print"):
            day = header_date.read_header_date(Path("10_16.jpg"), None, date(2026, 10, 16))
        self.assertEqual(day, date(2026, 10, 16))
        self.assertEqual(len(calls), 2)                                           # psm 6 ran out, psm 11 read it
        self.assertTrue(all(env.get("OMP_THREAD_LIMIT") == "1" for env in calls))  # one thread beside the cell OCR
        self.assertGreater(header_date.TIMEOUT, 10)


class ImageTests(BotCase):
    settings = {"public_base": "https://bot.example"}

    def test_duties_come_with_the_outlined_photo(self):
        self.bot.handle(image_event("U1"))
        with self.at(datetime(2026, 10, 1, 6, 30, tzinfo=TAIPEI)):
            self.register("U1", "林小明")
        texts = self.transport.replies[-1][1]
        self.assertEqual(len(texts), 3)                                       # registered, 我的班表, its photo
        self.assertEqual(texts[-1], "[image] https://bot.example/runs/run-1/people/72.jpg")
        self.assertEqual(self.transport.raw[-1][1]["type"], "flex")
        self.ask("查 甲")
        self.assertEqual(self.transport.replies[-1][1][-1],
                         "[image] https://bot.example/runs/run-1/people/%E7%94%B2.jpg")

    def test_code_missing_from_roster_gets_no_photo(self):
        self.bot.handle(image_event("U1"))
        self.ask("查 8")
        self.assertFalse(any(t.startswith("[image]") for t in self.transport.replies[-1][1]))

    def test_personal_card_keeps_other_dates_original_photo_and_lookup_button(self):
        self.bot.handle(image_event("U1"))
        self.register("U1", "林小明")
        self.store.add_roster("Cgroup", "older", "U2", 5, "2026-09-30")
        self.ask("10/1")
        messages = self.transport.raw[-1]
        self.assertEqual([m["type"] for m in messages], ["flex", "image"])
        self.assertEqual(date_chips(messages[0]), ["9/30"])
        self.assertIn("📷 下方是標示圖：藍框是勤務格", "\n".join(flex_texts(messages[0]["contents"])).splitlines())
        link = messages[0]["contents"]["footer"]["contents"][0]["action"]
        self.assertEqual(link, {"type": "postback", "label": "查看整張班表",
                                "data": "roster|run-1", "displayText": "查看整張班表"})
        self.bot.handle(postback_event("U1", link["data"]))
        self.assertEqual(self.transport.raw[-1][0]["originalContentUrl"],
                         "https://bot.example/runs/run-1/roster-v1.jpg")
        actions = [item["action"] for item in messages[1]["quickReply"]["items"]]
        self.assertEqual(actions[0]["label"], "查其他人")
        self.assertEqual(actions[0]["fillInText"], "查 2026/10/1 ")
        self.assertNotIn("勤務人員", [action["label"] for action in actions])
        self.assertNotIn("休假人員", [action["label"] for action in actions])

    def test_group_upload_privately_pairs_each_subscriber_with_photo(self):
        self.register("U1", "林小明", notify=True)
        self.register("U2", "陳小華", notify=True)
        self.transport.pushes.clear(); self.transport.replies.clear()
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertEqual(self.transport.replies, [])
        self.assertEqual(len(self.transport.pushes), 2)
        for to, _ in self.transport.pushes:
            messages = next(messages for recipient, messages in self.transport.raw_pushes if recipient == to)
            self.assertEqual([message["type"] for message in messages], ["image", "flex", "image"])
            self.assertEqual(messages[0]["originalContentUrl"],
                             "https://bot.example/runs/run-1/roster-v1.jpg")
            self.assertEqual(messages[0]["previewImageUrl"],
                             "https://bot.example/runs/run-1/roster-v1-preview.jpg")
        self.assertTrue(self.transport.pushes[0][1][2].endswith("/72.jpg"))
        self.assertTrue(self.transport.pushes[1][1][2].endswith("/%E7%94%B2.jpg"))

    def test_notification_starts_with_original_even_when_name_is_missing(self):
        self.register("U3", "張小美", notify=True)
        self.bot.handle(image_event("U1", "Cgroup"))
        messages = self.transport.raw_pushes[-1][1]
        self.assertEqual([message["type"] for message in messages], ["image", "text"])
        self.assertIn("/run-1/roster-v1.jpg", messages[0]["originalContentUrl"])
        self.assertIn("沒有找到「張小美」", messages[1]["text"])

    def test_original_photo_failure_does_not_block_duty_notification(self):
        self.register("U1", "林小明", notify=True)
        self.analyzer.roster_image = lambda run_id: None
        self.bot.handle(image_event("U1", "Cgroup"))
        messages = self.transport.raw_pushes[-1][1]
        self.assertEqual([message["type"] for message in messages], ["flex", "image"])
        self.assertIn("📌 新班表", messages[0]["altText"])

    def test_updated_roster_notification_starts_with_its_own_original_photo(self):
        self.register("U1", "林小明", notify=True)
        self.bot.handle(image_event("U1", "Cgroup"))
        updated = copy.deepcopy(self.analyzer.data)
        updated["run"]["id"] = "run-update"
        self.analyzer.data = updated
        self.bot.handle(image_event("U1", "Cgroup"))
        messages = self.transport.raw_pushes[-1][1]
        self.assertEqual([message["type"] for message in messages], ["image", "flex", "image"])
        self.assertIn("/run-update/roster-v1.jpg", messages[0]["originalContentUrl"])
        self.assertIn("🔄 班表更新", messages[1]["altText"])

    def test_full_roster_reply_uses_uploaded_png_when_line_accepts_it(self):
        import threading
        from types import SimpleNamespace
        from PIL import Image
        from all_staff.demo import DemoApp
        runs = Path(self.folder.name) / "runs"
        upload = runs / "original-run" / "upload"
        upload.mkdir(parents=True)
        source = upload / "sheet.png"
        Image.new("RGB", (500, 700), "white").save(source)
        app = SimpleNamespace(runs=runs, lock=threading.Lock(), result=lambda run_id: {})
        paths = DemoApp.roster_image(app, "original-run")
        self.assertEqual(paths[0], "original-run/upload/sheet.png")
        self.assertTrue((runs / paths[1]).is_file())
        self.assertEqual(DemoApp.roster_image(app, "original-run"), paths)

    def test_full_roster_photo_has_line_sized_original_and_preview(self):
        from PIL import Image
        from all_staff.evidence import write_roster_images
        folder = Path(self.folder.name)
        source, original, preview = (folder / name for name in ("upload.png", "roster.jpg", "preview.jpg"))
        with Image.new("RGB", (1200, 800), "white") as image:
            image.paste("red", (0, 0, 120, 120))
            image.save(source)
        write_roster_images(source, original, preview)
        self.assertLessEqual(original.stat().st_size, 10_000_000)
        self.assertLessEqual(preview.stat().st_size, 1_000_000)
        with Image.open(original) as full, Image.open(preview) as small:
            self.assertEqual(full.size, (1200, 800))
            self.assertEqual(small.size, (1000, 667))
            self.assertEqual(full.format, "JPEG")
            self.assertEqual(small.format, "JPEG")

    def test_slot_listing_pairs_with_only_that_hour_and_area(self):
        self.bot.handle(image_event("U1"))
        self.ask("5 國內")
        messages = self.transport.raw[-1]
        self.assertEqual([m["type"] for m in messages], ["flex", "image"])
        self.assertIn("10/1（四）班表｜05–06 國內", messages[0]["altText"])
        self.assertEqual(messages[1]["originalContentUrl"],
                         "https://bot.example/runs/run-1/slots/h05-06-dom-v1.jpg")
        self.assertIn("quickReply", messages[-1])

    def test_crop_failure_still_returns_the_slot_listing(self):
        self.bot.handle(image_event("U1"))
        def fail(*args):
            raise ValueError("missing geometry")
        self.analyzer.slot_image = fail
        self.ask("5 國內")
        self.assertEqual(len(self.transport.raw[-1]), 1)
        self.assertIn("國內二線\n1 人\n5", self.reply())
        self.assertIn("quickReply", self.transport.raw[-1][0])

    def test_staff_table_command_selects_requested_roster(self):
        self.bot.handle(image_event("U1"))
        self.register("U1", "林小明")
        self.store.add_roster("Cgroup", "next-day", "U2", 5, "2026-10-02")
        self.ask("勤務人員 10/1")
        messages = self.transport.raw[-1]
        self.assertIn("10/1（四）班表", messages[0]["altText"])
        self.assertEqual(messages[1]["originalContentUrl"],
                         "https://bot.example/runs/run-1/staff-table-v1.jpg")
        self.ask("勤務人員")
        self.assertIn("10/2（五）班表", self.transport.raw[-1][0]["altText"])

    def test_staff_table_needs_no_name_registration_and_missing_photo_has_a_link(self):
        self.bot.handle(image_event("U1"))
        self.ask("勤務人員")
        self.assertEqual(self.transport.raw[-1][1]["type"], "image")
        self.analyzer.staff_table_image = lambda run: None
        self.ask("勤務人員")
        card = self.transport.raw[-1][0]['contents']
        bubble = card['contents'][0] if card['type'] == 'carousel' else card
        self.assertEqual(bubble['footer']['contents'][0]['action']['data'], 'roster|run-1')


class StaffCardTests(BotCase):
    settings = {'public_base': 'https://bot.example'}

    def setUp(self):
        super().setUp()
        self.store.add_roster('Cgroup', 'run-1', 'U2', 5, '2026-10-01')
        path = Path(self.folder.name) / 'staff.txt'
        path.write_text('林小明\n王大同\n陳小華\n朱志明\n')
        self.bot.settings.staff_names = path
        self.store.set_staff_name('U1', '林小明')   # a colleague on the 名冊, whom the 名冊守門員 lets in

    def test_daily_names_are_one_card_of_bold_codes_with_quiet_gaps_and_options(self):
        self.ask('勤務人員')
        messages = self.transport.raw[-1]
        self.assertEqual([m['type'] for m in messages], ['flex', 'image'])
        bubble = messages[0]['contents']
        self.assertEqual((bubble['type'], bubble['size']), ('bubble', 'giga'))
        texts = flex_texts(bubble)
        self.assertIn('72 林小明', texts)
        self.assertIn('26 王大同', texts)
        self.assertIn('5 —', texts)                       # no name: the code and a faint dash, not a phrase
        self.assertNotIn('姓名未辨識', texts)
        self.assertIn('— 姓名未辨識 1 人，請看下方原圖', texts)   # said once, at the foot
        self.assertLess(texts.index('🔢 數字代號'), texts.index('5 —'))
        self.assertLess(texts.index('5 —'), texts.index('🎖️ 中文代號'))
        cells = {cell['text']: cell for cell in cells_of(bubble)}
        self.assertEqual(cells['72 林小明']['contents'][0], {'type': 'span', 'text': '72', 'weight': 'bold',
                                                          'color': STAFF_UI['navy']})
        self.assertEqual(cells['5 —']['contents'][1]['color'], STAFF_UI['faint'])
        actions = [i['action'] for i in messages[-1]['quickReply']['items']]
        self.assertEqual([a['label'] for a in actions], ['查詢人員', '查看時段', '休假人員'])
        self.assertEqual(actions[0]['fillInText'], '查 2026/10/1 ')

    def test_staff_actions_keep_date_after_a_new_roster_is_uploaded(self):
        self.ask('勤務人員')
        actions = [i['action'] for i in self.transport.raw[-1][-1]['quickReply']['items']]
        self.store.add_roster('Cgroup', 'newer', 'U2', 5, '2026-10-02')
        self.bot.handle(postback_event('U1', actions[1]['data']))
        self.assertIn('10/1', self.reply())
        self.analyzer.data['leave_table'] = {'available': True, 'rest': ['72'], 'leave': []}
        self.bot.handle(postback_event('U1', actions[2]['data']))
        self.assertIn('10/1', self.reply())
        self.assertIn('林小明', self.reply())

    def test_missing_crop_keeps_names_and_options(self):
        self.analyzer.staff_table_image = lambda run: None
        self.ask('勤務人員')
        messages = self.transport.raw[-1]
        self.assertEqual(len(messages), 1)
        texts = flex_texts(messages[0]['contents'])
        self.assertIn('72 林小明', texts)
        self.assertIn('— 姓名未辨識 1 人，請看整張班表', texts)   # no crop below: the whole photo instead
        self.assertNotIn('下方原圖', '\n'.join(texts))
        self.assertIn('quickReply', messages[0])

    def test_directory_order_never_assigns_daily_codes_and_off_duty_people_remain_listed(self):
        self.analyzer.data['names'] = {'35': ['朱志明'], '72': ['王大同']}
        self.ask('勤務人員')
        texts = flex_texts(self.transport.raw[-1][0]['contents'])
        self.assertIn('35 朱志明', texts)
        self.assertIn('72 王大同', texts)
        self.assertNotIn('林小明', '\n'.join(texts))
        self.assertIn('代號與姓名對照｜姓名已配對 2 人', texts)
        self.assertIn('👥 共 5 人', texts)   # 5, 26 and 甲 have duties but no name

    def test_people_on_leave_are_listed_with_a_tag_and_a_legend(self):
        self.analyzer.data['names']['35'] = ['朱志明']
        self.analyzer.data['leave_table'] = {'available': True, 'rest': ['35'],
                                             'leave': [{'code': '甲', 'name': [], 'kind': '補休'}]}
        self.ask('勤務人員')
        texts = flex_texts(self.transport.raw[-1][0]['contents'])
        self.assertIn('35 朱志明 💤', texts)
        self.assertIn('甲 陳小華 📝', texts)
        self.assertIn('72 林小明', texts)
        self.assertIn('💤 輪休　📝 請假（班表右下角的記載）', texts[-2].splitlines())

    def test_full_population_fits_one_bubble_and_keeps_all_valid_codes_once(self):
        from all_staff.bot import FLEX_BUBBLE_LIMIT, flex_size, staff_card
        from all_staff.schema import ALLOWED_CODES
        roster = {'run_id': 'one', 'roster_date': '2026-10-03'}
        for result, names in (({}, {code: '歐陽小明' for code in ALLOWED_CODES}),           # every name read
                              ({'people': [{'code': c} for c in ALLOWED_CODES]}, {})):    # the table not read
            with self.subTest(named=bool(names)):
                contents = staff_card(roster, result, names, show_original=True, has_image=True)['contents']
                # without names or fixed codes, 52 of the codes are ones nobody has, each with a word: two bubbles
                bubbles = [contents] if names else contents['contents']
                self.assertEqual(contents['type'], 'bubble' if names else 'carousel')
                for bubble in bubbles:
                    self.assertLessEqual(flex_size(bubble), FLEX_BUBBLE_LIMIT)
                    self.assertEqual(bubble['footer']['contents'][0]['action']['data'], 'roster|one')
                self.assertEqual([cell['contents'][0]['text'] for bubble in bubbles for cell in cells_of(bubble)],
                                 list(ALLOWED_CODES))

    def test_a_real_days_roster_with_leave_is_one_small_bubble(self):
        # 78 people, as the team has: 69 numeric and English codes and 9 役男, 15 of them on 輪休 or 請假
        from all_staff.bot import flex_size, staff_card
        codes = [str(n) for n in range(1, 61)] + list('ABCDEFGHI') + list('甲乙丙丁戊己庚辛壬')
        names = {code: '林小明' if n % 2 else '歐陽小明' for n, code in enumerate(codes) if n % 10}   # some not read
        leave = {'available': True, 'rest': codes[50:62],
                 'leave': [{'code': code, 'name': [], 'kind': '休假'} for code in codes[62:65]]}
        card = staff_card({'run_id': 'one', 'roster_date': '2026-10-03'},
                          {'people': [{'code': code} for code in codes], 'leave_table': leave}, names,
                          show_original=True, has_image=True)
        self.assertEqual(card['contents']['type'], 'bubble')
        self.assertLess(flex_size(card['contents']), 25_000)
        self.assertEqual(len(cells_of(card['contents'])), 78)

    def test_were_one_bubble_too_big_the_letters_would_move_to_a_second(self):
        from all_staff.bot import FLEX_BUBBLE_LIMIT, FLEX_CAROUSEL_LIMIT, flex_size, staff_card
        from all_staff.schema import ALLOWED_CODES
        leave = {'available': True, 'rest': list(ALLOWED_CODES), 'leave': []}   # every code tagged, long names
        card = staff_card({'run_id': 'one', 'roster_date': '2026-10-03'}, {'leave_table': leave},
                          {code: '歐陽小明明' for code in ALLOWED_CODES})
        bubbles = card['contents']['contents']
        self.assertEqual(len(bubbles), 2)
        self.assertTrue(all(flex_size(bubble) <= FLEX_BUBBLE_LIMIT for bubble in bubbles))
        self.assertLessEqual(flex_size(card['contents']), FLEX_CAROUSEL_LIMIT)
        self.assertTrue(all(cell['contents'][0]['text'].isdigit() for cell in cells_of(bubbles[0])))
        self.assertEqual([cell['contents'][0]['text'] for bubble in bubbles for cell in cells_of(bubble)],
                         list(ALLOWED_CODES))


class EvidenceTests(unittest.TestCase):
    def test_slot_crop_keeps_header_and_one_hour_without_neighbouring_area(self):
        from PIL import Image, ImageDraw
        from all_staff.evidence import render_slot
        canvas = Image.new("RGB", (300, 240), "white")
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, 20, 299, 79), fill="yellow")  # work headers
        draw.rectangle((0, 100, 299, 119), fill="red")   # previous hour
        draw.rectangle((0, 120, 99, 139), fill="green")
        draw.rectangle((100, 120, 299, 139), fill="purple")
        draw.rectangle((0, 140, 299, 159), fill="blue")  # next hour
        quad = lambda x1, y1, x2, y2: [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
        geometry = {"cells": {"9,2": quad(20,120,60,139), "9,3": quad(60,120,99,139)},
                    "headers": {"2": quad(20,20,60,79), "3": quad(60,20,99,79)},
                    "times": {"9": quad(0,120,20,139)}, "time_header": quad(0,20,20,79)}
        image = render_slot(canvas, geometry, 9, range(2,4))
        self.assertEqual(image.getpixel((image.width//2, 20)), (255,255,0))
        self.assertEqual(image.getpixel((image.width//2, image.height-10)), (0,128,0))
        colors = set(image.getdata())
        self.assertNotIn((255,0,0), colors)
        self.assertNotIn((128,0,128), colors)
        self.assertNotIn((0,0,255), colors)
        self.assertIsNone(render_slot(canvas, geometry, 10, range(2,4)))

    def test_render_outlines_cells_in_blue_and_crops_below_last_duty(self):
        from PIL import Image
        from all_staff.evidence import BLUE, render_person
        cells = {f"{r},{c}": [[c * 20, r * 10], [c * 20 + 20, r * 10], [c * 20 + 20, r * 10 + 10], [c * 20, r * 10 + 10]]
                 for r in range(4, 23) for c in range(2, 33)}
        canvas = Image.new("RGB", (700, 260), "white")
        image = render_person(canvas, {"cells": cells}, [(5, 3), (6, 3)])
        self.assertEqual(image.width, 700)
        self.assertLess(image.height, 260)                       # rows below the duty are cropped
        self.assertGreaterEqual(image.height, 80)                 # but one spare row is kept
        self.assertEqual(image.getpixel((70, 50)), BLUE)          # top edge of (5, 3)
        self.assertEqual(image.getpixel((200, 50)), (255, 255, 255))
        self.assertIsNone(render_person(canvas, {"cells": cells}, [(40, 3)]))

    @staticmethod
    def work_canvas(rows, columns):
        """Yellow headers above 20 px hour rows; the time column is x 0–39, column c is x 20c–20c+19."""
        from PIL import Image, ImageDraw
        quad = lambda x1, y1, x2, y2: [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        canvas = Image.new("RGB", (100 + 20 * max(columns), 140 + 20 * len(rows)), "white")
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, 20, canvas.width - 1, 79), fill="yellow")
        top = lambda row: 100 + 20 * (row - rows[0])
        geometry = {"cells": {f"{r},{c}": quad(20 * c, top(r), 20 * c + 19, top(r) + 19)
                              for r in rows for c in columns},
                    "headers": {str(c): quad(20 * c, 20, 20 * c + 19, 79) for c in columns},
                    "times": {str(r): quad(0, top(r), 39, top(r) + 19) for r in rows},
                    "time_header": quad(0, 20, 39, 79)}
        return canvas, draw, top, geometry

    def test_work_crop_keeps_its_columns_and_every_hour_in_order(self):
        from itertools import groupby
        from all_staff.evidence import BLUE, render_work
        canvas, draw, top, geometry = self.work_canvas(range(8, 13), range(2, 5))
        hours = {9: (0, 128, 0), 10: (255, 165, 0), 11: (0, 255, 255)}
        for row, colour in {8: (255, 0, 0), **hours, 12: (139, 69, 19)}.items():
            draw.rectangle((0, top(row), 79, top(row) + 19), fill=colour)
        draw.rectangle((80, top(8), 99, top(12) + 19), fill=(128, 0, 128))   # c04, the neighbouring work
        image = render_work(canvas, geometry, (2, 3), range(9, 12))
        names = {(255, 255, 0): "header", BLUE: "outline", **{colour: row for row, colour in hours.items()}}
        def down(x):
            return [name for name, _ in groupby(n for n in (names.get(image.getpixel((x, y)))
                                                            for y in range(image.height)) if n)]
        self.assertEqual(down(4 + 39 + 9), ["header", "outline", 9, 10, 11, "outline"])   # middle of c02
        self.assertEqual(down(4 + 19), ["header", 9, 10, 11])                              # the time column
        colours = {colour for _, colour in image.getcolors(image.width * image.height)}
        for left_out in ((255, 0, 0), (139, 69, 19), (128, 0, 128)):
            self.assertNotIn(left_out, colours)
        self.assertIsNone(render_work(canvas, geometry, (2, 3), range(9, 14)))   # 13 has no cells
        self.assertIsNone(render_work(canvas, geometry, (2, 5), range(9, 12)))   # nor has c05
        self.assertIsNone(render_work(canvas, {**geometry, "time_header": None}, (2, 3), range(9, 12)))
        self.assertIsNone(render_work(canvas, {"cells": geometry["cells"]}, (2, 3), range(9, 12)))

    def test_a_whole_day_of_one_column_wraps_into_panels_beside_each_other(self):
        from all_staff.evidence import render_work
        canvas, draw, top, geometry = self.work_canvas(range(4, 23), [2])
        draw.rectangle((0, top(4), 59, top(4) + 19), fill="red")
        draw.rectangle((0, top(22), 59, top(22) + 19), fill="blue")
        image = render_work(canvas, geometry, [2], range(4, 23))
        self.assertLess(image.height, 2 * image.width)          # one stack would be 66 × 436
        first = {image.getpixel((x, y)) for x in range(image.width // 2) for y in range(image.height)}
        last = {image.getpixel((x, y)) for x in range(image.width // 2, image.width) for y in range(image.height)}
        self.assertIn((255, 0, 0), first)
        self.assertNotIn((255, 0, 0), last)
        self.assertIn((0, 0, 255), last)
        self.assertEqual(image.getpixel((4 + 30, 30)), (255, 255, 0))                 # headers on every panel
        self.assertEqual(image.getpixel((image.width - 4 - 30, 30)), (255, 255, 0))
        short = render_work(canvas, geometry, [2], range(9, 13))
        self.assertGreater(short.height, short.width)                                 # four hours stay one stack

    def test_work_image_is_cached_per_columns_and_hours_beside_the_slot_crops(self):
        import json
        import threading
        from all_staff.demo import DemoApp, DemoError
        canvas, _, _, geometry = self.work_canvas(range(9, 12), range(2, 17))
        with tempfile.TemporaryDirectory() as folder:
            runs = Path(folder)
            cells = runs / "run-w" / "cells"
            (cells / "geometry").mkdir(parents=True)
            (cells / "corrected").mkdir()
            (runs / "run-w" / "result.json").write_text(json.dumps({"grid_version": 2}))
            (cells / "geometry" / "g.json").write_text(json.dumps(geometry))
            canvas.save(cells / "corrected" / "g.jpg")
            app = DemoApp.__new__(DemoApp)
            app.runs, app.lock = runs, threading.Lock()
            paths = app.work_image("run-w", [3, 2, 3], range(9, 12))
            self.assertEqual(paths, ("run-w/works/c02_03-h10-13-v1-g2.jpg",
                                     "run-w/works/c02_03-h10-13-v1-g2-preview.jpg"))
            self.assertTrue(all((runs / path).is_file() for path in paths))
            self.assertEqual(app.slot_image("run-w", 9, "intl"),
                             ("run-w/slots/h10-11-intl-v1-g2.jpg", "run-w/slots/h10-11-intl-v1-g2-preview.jpg"))
            self.assertIsNone(app.work_image("run-w", [2]))   # the whole day: no cells before 10-11
            self.assertFalse((runs / "run-w" / "works" / "c02-h05-24-v1-g2.jpg").exists())
            for columns, rows in (([], None), ([1], None), ([2], [3]), ([2], [23])):
                with self.assertRaises(DemoError):
                    app.work_image("run-w", columns, rows)
            (cells / "corrected" / "g.jpg").unlink()
            self.assertEqual(app.work_image("run-w", [2, 3], [11, 9]), paths)   # cached


class WorkImageTests(BotCase):
    settings = {"public_base": "https://bot.example"}

    def setUp(self):
        super().setUp()
        self.store.add_roster("Cgroup", "run-1", "U2", 5, "2026-10-01")
        self.asked = []
        def work_image(run_id, columns, rows):
            self.asked.append((run_id, columns, rows))
            stem = f"{run_id}/works/c{'_'.join(f'{c:02d}' for c in columns)}-h{rows[0] + 1:02d}-{rows[-1] + 2:02d}-v1"
            return stem + ".jpg", stem + "-preview.jpg"
        self.analyzer.work_image = work_image

    def show_work(self, work="國際線"):
        from all_staff.bot import work_key
        self.bot.handle(postback_event("U1", f"work|run-1|{work_key(work)}"))
        return self.transport.raw[-1]

    def test_whole_day_list_comes_with_the_work_cut_from_the_photo(self):
        messages = self.show_work()
        self.assertEqual([m["type"] for m in messages], ["flex", "image"])
        self.assertEqual(self.asked, [("run-1", [2, 3], range(4, 6))])   # 05–07, the staffed hours only
        self.assertEqual(messages[1]["originalContentUrl"], "https://bot.example/runs/run-1/works/c02_03-h05-07-v1.jpg")
        self.assertEqual(messages[1]["previewImageUrl"],
                         "https://bot.example/runs/run-1/works/c02_03-h05-07-v1-preview.jpg")
        self.assertIn("📷 下方為這項工作的原圖，可點開放大", flex_texts(messages[0]["contents"])[-1].splitlines())
        self.assertNotIn("quickReply", messages[0])
        self.assertEqual(messages[1]["quickReply"]["items"][0]["action"]["data"], "works|run-1")
        self.show_work("國內一線")
        self.assertEqual(self.asked[-1], ("run-1", [17], range(6, 7)))

    def test_list_stands_alone_without_a_crop(self):
        def alone():
            messages = self.show_work()
            self.assertEqual([m["type"] for m in messages], ["flex"])
            self.assertIn("quickReply", messages[0])
            self.assertNotIn("下方為這項工作的原圖", "\n".join(flex_texts(messages[0]["contents"])))
            self.assertIn("05–06", flex_texts(messages[0]["contents"]))
        self.analyzer.work_image = lambda run_id, columns, rows: None      # no geometry for this run
        alone()
        def fail(*args):
            raise ValueError("missing geometry")
        self.analyzer.work_image = fail
        alone()
        del self.analyzer.work_image                                         # an analyzer without crops
        alone()
        self.analyzer.work_image = lambda run_id, columns, rows: ("a.jpg", "b.jpg")
        self.bot.settings.public_base = None
        alone()


def printed_columns():
    """The 35 work columns c02–c36 as the roster prints them (``configs/all_staff_columns.json``)."""
    import json
    path = Path(__file__).resolve().parent / "configs/all_staff_columns.json"
    return {int(column): duty for column, duty in json.loads(path.read_text())["column_map"].items()}


class WorkCardTests(unittest.TestCase):
    """「查看工作」 as one card of work tiles, and one work's day as one card of hours."""
    roster = {"run_id": "20261008-091512-a1b2c3", "roster_date": "2026-10-08"}

    def setUp(self):
        from all_staff.bot import TAIPEI
        self.now = datetime(2026, 10, 8, 10, 20, tzinfo=TAIPEI)   # 10–11 is row 9

    def busy_day(self, per_column=1, columns=None, unsure=False):
        """Every printed column staffed from 05 to 24, ``per_column`` codes a cell (``unsure``: every cell unsure)."""
        from all_staff.schema import ALLOWED_CODES
        people = {}
        for column, duty in (columns or printed_columns()).items():
            for row in range(4, 23):
                for extra in range(per_column):
                    code = ALLOWED_CODES[(column * 19 + row * 3 + extra * 59) % len(ALLOWED_CODES)]
                    people.setdefault(code, []).append(slot(row, column, duty, .5 if unsure or row == 6 else .99))
        return {"people": [{"code": code, "slots": slots} for code, slots in people.items()]}

    def test_every_work_of_a_roster_is_a_tile_on_one_card_in_three_sections(self):
        from all_staff.bot import flex_size, work_key, work_options, work_picker
        result = self.busy_day()
        result["people"][0]["slots"].append(slot(9, 15, "c15"))   # an unreadable header becomes its column
        card = work_picker(self.roster, result, now=self.now)
        bubble = card["contents"]
        self.assertEqual((bubble["type"], bubble["size"]), ("bubble", "giga"))
        self.assertLess(flex_size(bubble), 25_000)
        options = work_options(result)
        self.assertGreaterEqual(len(options), 26)
        tiles = tappable(bubble, "work|")
        self.assertEqual([tile["action"]["data"] for tile in tiles],
                         [f"work|{self.roster['run_id']}|{work_key(name)}" for name, _, _ in options])
        self.assertEqual([tile["action"]["displayText"] for tile in tiles], [f"查看工作 {name}" for name, _, _ in options])
        self.assertTrue(all(len(tile["action"]["data"]) <= 300 and len(tile["action"]["label"]) <= 20 for tile in tiles))
        texts = flex_texts(bubble)
        self.assertIn("📅 10/8（四）・今天", texts)
        self.assertIn(f"📋 {len(options)} 項工作", texts)
        self.assertLess(texts.index("✈️ 國際"), texts.index("國際線"))
        self.assertLess(texts.index("國際線"), texts.index("第15欄\n（未辨識）"))
        self.assertLess(texts.index("第15欄\n（未辨識）"), texts.index("🏠 國內"))
        self.assertLess(texts.index("🏠 國內"), texts.index("國內一線"))
        self.assertLess(texts.index("國內帶勤"), texts.index("🧩 其他"))
        self.assertLess(texts.index("🧩 其他"), texts.index("A1站"))
        self.assertEqual([tile["contents"][1]["text"] for tile in tiles], [f"{count} 人" for _, _, count in options])
        self.assertIn("🔤 「第 N 欄」：表頭沒讀出來，用欄位代替", texts[-1].splitlines())
        self.assertNotIn("carousel", str(card))

    def test_one_work_lists_each_staffed_hour_and_frames_the_current_one(self):
        from all_staff.bot import SLOT_UI, work_card
        result = {"people": [{"code": "72", "slots": [slot(4, 2, "國際線"), slot(9, 2, "國際線")]},
                             {"code": "26", "slots": [slot(9, 3, "國際線", .5)]},
                             {"code": "5", "slots": [slot(4, 20, "國內線二線")]}]}
        names = {"72": "林小明"}
        card = work_card(self.roster, result, names, "國際線", now=self.now)
        bubble = card["contents"]
        texts = flex_texts(bubble)
        self.assertEqual(texts[:4], ["✈️ 國際線", "整天的上班名單", "📅 10/8（四）・今天", "👥 整天 2 人"])
        rows = [line for line in bubble["body"]["contents"][0]["contents"] if line["type"] == "box"]   # one panel
        self.assertEqual(len(rows), 2)
        self.assertEqual(flex_texts(rows[0]), ["05–06", "72 林小明"])
        self.assertEqual(flex_texts(rows[1]), ["現在", "10–11", "26 ？", "72 林小明"])
        self.assertNotIn("borderColor", rows[0])
        self.assertEqual(rows[1]["borderColor"], SLOT_UI["now"])
        notes = texts[-1].splitlines()
        self.assertIn("⏰ 框起來的是現在這個小時", notes)
        self.assertIn("❓ 標「？」的格子辨識信心較低，請對照原圖", notes)
        self.assertIn("🔤 只有代號：姓名未辨識", notes)
        tomorrow = work_card(self.roster, result, names, "國際線", now=self.now + timedelta(days=1))
        self.assertNotIn("現在", flex_texts(tomorrow["contents"]))
        self.assertIn("📅 10/8（四）・昨天", flex_texts(tomorrow["contents"]))

    def test_a_busy_work_is_one_bubble_even_the_busiest_possible(self):
        from all_staff.bot import FLEX_BUBBLE_LIMIT, flex_size, hour_label, work_card
        from all_staff.schema import ALLOWED_CODES
        names = {code: "歐陽小明" for code in ALLOWED_CODES}
        intl = {column: "國際線" for column in (2, 3, 4, 5)}   # the widest work: four columns
        card = work_card(self.roster, self.busy_day(1, intl), names, "國際線", has_image=True, now=self.now)
        self.assertEqual(card["contents"]["type"], "bubble")
        self.assertLess(flex_size(card["contents"]), 25_000)
        self.assertTrue(any(node.get("contents") for node in flex_nodes(card) if node["type"] == "text"))  # bold codes
        # Two codes read in every one of its cells, all day long, and every one of them unsure: two bubbles of
        # styled cells would pass LINE's 50 KB for a carousel, so the cells drop their bold and it stays one card
        card = work_card(self.roster, self.busy_day(2, intl, unsure=True), names, "國際線", has_image=True,
                         now=self.now)
        self.assertEqual(card["contents"]["type"], "bubble")
        self.assertLessEqual(flex_size(card["contents"]), FLEX_BUBBLE_LIMIT)
        texts = flex_texts(card)
        self.assertEqual([text for text in texts if text in map(hour_label, range(4, 23))],
                         [hour_label(row) for row in range(4, 23)])
        self.assertEqual(sum(text.endswith(" ？") for text in texts), 19 * 8)
        self.assertIn("❓ 標「？」的格子辨識信心較低，請對照原圖", texts[-1].splitlines())


class SimulatorWorkflowTests(unittest.TestCase):
    def test_private_onboarding_group_upload_and_reset(self):
        from all_staff.line_sim import Simulator
        with tempfile.TemporaryDirectory() as folder:
            sim = Simulator(FakeAnalyzer(), Path(folder), "http://localhost:8779", notify=True, push=True)

            def idle():
                deadline = time.monotonic() + 3
                while sim.world.pending and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(sim.world.pending, 0)
                self.assertFalse(any(item["kind"] == "error" for item in sim.world.state["log"]))

            try:
                idle()
                group = next(c for c in sim.world.state["chats"] if c["type"] == "group")
                for command in ("設定 陳志明", "通知 開"):
                    sim.send_text("dm:U_hua", "U_hua", command); idle()
                sim.send_image(group["key"], "U_ming", b"jpeg"); idle()
                self.assertFalse(any(m["sender"] == "bot" and m["chat"] == group["key"]
                                     for m in sim.world.state["messages"]))
                notices = [m for m in sim.world.state["messages"] if m["sender"] == "bot"
                           and m["chat"] == "dm:U_hua" and m["type"] == "flex"]
                self.assertIn("國際備勤", flex_texts(notices[-1]["contents"]))
                sim.send_text("dm:U_hua", "U_hua", "查看時段"); idle()
                picker = [m for m in sim.world.state["messages"] if m["sender"] == "bot" and m["chat"] == "dm:U_hua"][-1]
                self.assertEqual(picker["type"], "flex")
                sim.send_postback("dm:U_hua", "U_hua", "area|run-1|8|intl", "09–10 國際"); idle()
                listing = [m for m in sim.world.state["messages"] if m["sender"] == "bot"
                           and m["chat"] == "dm:U_hua" and m["type"] == "flex"][-1]
                self.assertIn("09–10 國際", listing["altText"])
                self.assertIn("甲 陳志明", flex_texts(listing["contents"]))
                self.assertEqual(sim.world.state["messages"][-1]["type"], "image")
                sim.reset(); idle()
                self.assertIsNone(sim.store.user("U_hua"))
                self.assertIsNone(sim.store.team_roster())
            finally:
                sim.pool.shutdown(wait=True)

    def test_an_undated_group_photo_waits_for_the_simulated_administrator(self):
        from all_staff.line_sim import Simulator
        analyzer = FakeAnalyzer(copy.deepcopy(RESULT))
        analyzer.data["run"]["roster_date"] = None
        with tempfile.TemporaryDirectory() as folder:
            sim = Simulator(analyzer, Path(folder), "http://localhost:8779", admins=("U_ming",), notify=True,
                            push=True)

            def idle():
                deadline = time.monotonic() + 3
                while sim.world.pending and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(sim.world.pending, 0)
                self.assertFalse(any(item["kind"] == "error" for item in sim.world.state["log"]))

            def from_bot(chat):
                return [m for m in sim.world.state["messages"] if m["sender"] == "bot" and m["chat"] == chat]

            try:
                idle()
                group = next(c for c in sim.world.state["chats"] if c["type"] == "group")
                for command in ("設定 陳志明", "通知 開"):
                    sim.send_text("dm:U_hua", "U_hua", command); idle()
                before = {chat: len(from_bot(chat)) for chat in ("dm:U_hua", "dm:U_jie", group["key"])}
                sim.send_image(group["key"], "U_jie", b"jpeg"); idle()
                self.assertEqual({chat: len(from_bot(chat)) for chat in before}, before)   # nobody has heard yet
                photo, card = from_bot("dm:U_ming")[-2:]
                self.assertEqual((photo["type"], card["type"]), ("image", "flex"))
                tomorrow = flex_buttons(card["contents"])[1]
                sim.send_postback("dm:U_ming", "U_ming", tomorrow["data"], tomorrow["displayText"]); idle()
                self.assertIn("日期已設為", from_bot("dm:U_ming")[-1]["text"])
                notice = [m for m in from_bot("dm:U_hua") if m["type"] == "flex"][-1]
                self.assertIn("國際備勤", flex_texts(notice["contents"]))
                self.assertNotIn("日期未辨識", flex_texts(notice["contents"]))
                self.assertEqual(len(from_bot("dm:U_jie")), before["dm:U_jie"])            # the uploader never does
            finally:
                sim.pool.shutdown(wait=True)


class RosterGateTests(unittest.TestCase):
    """A picture that is not a roster is stopped before it is stored (and queued) as one."""

    @staticmethod
    def png(size=(400, 300), color="white") -> bytes:
        from io import BytesIO
        from PIL import Image
        buffer = BytesIO()
        Image.new("RGB", size, color).save(buffer, "PNG")
        return buffer.getvalue()

    @staticmethod
    def app(runs: Path):
        from all_staff.demo import DemoApp
        app = DemoApp.__new__(DemoApp)
        app.template = Path(__file__).resolve().parent / "configs/all_staff_roster_template.local.json"
        app.runs = runs
        return app

    def test_evidence_needs_a_date_names_or_many_confident_codes(self):
        from all_staff.demo import roster_evidence

        def data(date=None, names=0, confident=0, weak=0, single=0):
            readings = {str(n): ["林小明", ""] for n in range(names)}
            readings.update({f"s{n}": ["林"] for n in range(single)})
            cells = [{"pred": [{"code": "1", "conf": .9}] * confident + [{"code": "2", "conf": .4}] * weak}]
            return {"run": {"roster_date": date}, "names": readings, "sheets": [{"cells": cells}]}

        self.assertTrue(roster_evidence(data(date="2026-10-01")))
        self.assertTrue(roster_evidence(data(names=98)))
        self.assertTrue(roster_evidence(data(confident=450)))
        self.assertFalse(roster_evidence(data(confident=29)))                # the rich-menu picture
        self.assertFalse(roster_evidence(data(names=9, confident=119)))      # just under each threshold
        self.assertFalse(roster_evidence(data(single=60)))                   # one character is not a name
        self.assertFalse(roster_evidence(data(weak=500)))                    # many codes, none of them confident
        self.assertFalse(roster_evidence({"run": {}, "sheets": []}))

    def test_the_table_check_only_says_no_when_it_is_sure(self):
        from unittest import mock
        app, payload = self.app(Path("unused")), self.png()
        corners = ((0, 0), (10, 0), (10, 10), (0, 10))
        with mock.patch("backend.phase1_cell_pipeline._registration_corners", return_value=(corners, "perspective")):
            self.assertTrue(app._has_table(payload))
        with mock.patch("backend.phase1_cell_pipeline._registration_corners", return_value=(None, "perspective")):
            self.assertFalse(app._has_table(payload))
        with mock.patch("backend.phase1_cell_pipeline._registration_corners", side_effect=RuntimeError("boom")):
            self.assertTrue(app._has_table(payload))                         # a failing check lets it through

    def test_a_picture_without_a_table_that_cannot_be_cut_is_not_a_roster_and_leaves_nothing(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest import mock
        from all_staff.demo import DemoError, NotARoster
        with tempfile.TemporaryDirectory() as folder:
            app = self.app(Path(folder))
            app._pool, app.lock, app.context = ThreadPoolExecutor(1), threading.Lock(), {"fx": .22, "fy": .12}
            try:
                for table, expected in ((False, NotARoster), (True, DemoError)):
                    with mock.patch.object(app, "_has_table", return_value=table), \
                            mock.patch("all_staff.context.prepare_context", return_value={}), \
                            mock.patch("all_staff.schema.load_annotations", return_value=[]), \
                            mock.patch.object(app, "_roster_date", return_value=None), \
                            mock.patch.object(app, "_name_table", return_value=({}, [])):
                        with self.assertRaises(expected) as caught:
                            app.analyze(self.png(), "avatar.png")
                    self.assertIs(type(caught.exception), expected)
                    if not table:                                            # no table found, nothing cut
                        self.assertIn("表格框線", str(caught.exception))
                        self.assertEqual(list(Path(folder).iterdir()), [])
                    else:                                                    # a table that could not be cut
                        self.assertIn("無法從照片切出勤務格", str(caught.exception))
            finally:
                app._pool.shutdown()

    def test_a_picture_that_reads_like_nothing_is_removed_again_but_a_dated_one_is_kept(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest import mock
        from all_staff.demo import NotARoster
        sheet = lambda: {"cells": [{"pred": [{"code": "1", "conf": .9}] * 20}]}
        with tempfile.TemporaryDirectory() as folder:
            app = self.app(Path(folder))
            app._pool, app.lock = ThreadPoolExecutor(1), threading.Lock()
            app.context, app.engine = {"fx": .22, "fy": .12}, object()
            app.recognizer = app.roster = app.root = None

            def analyze(roster_date):
                # the table check missed it (a photo LINE has shrunk): no longer a reason to stop
                with mock.patch.object(app, "_has_table", return_value=False), \
                        mock.patch("all_staff.context.prepare_context", return_value={}), \
                        mock.patch("all_staff.schema.load_annotations", return_value=[object()]), \
                        mock.patch("all_staff.demo.sheet_payload", side_effect=lambda *a, **k: sheet()), \
                        mock.patch("all_staff.demo.common_payload", return_value={}), \
                        mock.patch("all_staff.demo.people", return_value=[]), \
                        mock.patch.object(app, "_roster_date", return_value=roster_date), \
                        mock.patch.object(app, "_name_table", return_value=({}, [])):
                    return app.analyze(self.png(), "x.png")

            try:
                with self.assertRaises(NotARoster):
                    analyze(None)
                self.assertEqual(list(Path(folder).iterdir()), [])           # nothing left on disk
                data = analyze("2026-10-01")
                self.assertTrue((Path(folder) / data["run"]["id"] / "result.json").is_file())
            finally:
                app._pool.shutdown()


class WebhookTests(unittest.TestCase):
    def test_signature(self):
        import base64, hashlib, hmac
        from all_staff.line_webhook import valid_signature
        body = b'{"events":[]}'
        signature = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
        self.assertTrue(valid_signature("secret", body, signature))
        self.assertFalse(valid_signature("secret", body + b" ", signature))
        self.assertFalse(valid_signature("secret", body, None))

    def test_callback_route_checks_signature_and_dispatches(self):
        import base64, hashlib, hmac, json, os
        from unittest import mock
        try:
            from fastapi.testclient import TestClient
        except Exception:  # fastapi / httpx not installed
            self.skipTest("fastapi test client unavailable")
        from all_staff import demo, line_webhook
        replies = []
        class Transport(line_webhook.LineTransport):
            def _call(self, method, url, payload=None):
                if url.endswith("/message/reply"):   # not the rich menu calls of the start-up sync
                    replies.append(payload)
                return b"{}"
        env = {"LINE_V2_CHANNEL_SECRET": "s", "LINE_V2_CHANNEL_ACCESS_TOKEN": "t", "LINE_V2_PUBLIC_BASE_URL": ""}
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, env), \
                mock.patch.object(demo, "DemoApp", lambda *a, **k: FakeAnalyzer()), \
                mock.patch.object(line_webhook, "LineTransport", Transport):
            client = TestClient(line_webhook.create_app(Path(folder), Path("m.pt"), roster=None,
                                                         template=Path("t.json"), folder=Path(folder)))
            body = json.dumps({"events": [text_event("U1", "說明")]}).encode()
            signature = base64.b64encode(hmac.new(b"s", body, hashlib.sha256).digest()).decode()
            self.assertEqual(client.post("/line/callback", content=body,
                                         headers={"x-line-signature": "bad"}).status_code, 400)
            self.assertEqual(client.post("/line/callback", content=body,
                                         headers={"x-line-signature": signature}).status_code, 200)
            for _ in range(50):
                if replies: break
                time.sleep(.05)
            self.assertIn("📋 用法說明", replies[0]["messages"][0]["altText"])

    def test_administrators_are_line_user_ids_not_line_ids(self):
        import contextlib
        import io
        from all_staff.line_webhook import admin_ids
        one, two = "U" + "0123456789abcdef" * 2, "U" + "f" * 32
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(admin_ids(f"qteddie1322, {one}，{two}\n'{one}' U123"), (one, two))
            self.assertEqual(admin_ids("U" + "F" * 32), ())                   # LINE's ids are lower-case hex
        self.assertIn("「qteddie1322」不是 LINE userId", out.getvalue())
        self.assertIn("「U123」", out.getvalue())
        self.assertNotIn(one, out.getvalue())
        self.assertEqual((admin_ids(None), admin_ids(""), admin_ids(" , ")), ((), (), ()))

    def test_administrators_come_from_the_environment(self):
        import base64, hashlib, hmac, json, os
        from unittest import mock
        try:
            from fastapi.testclient import TestClient
        except Exception:  # fastapi / httpx not installed
            self.skipTest("fastapi test client unavailable")
        from all_staff import demo, line_webhook
        replies = []
        class Transport(line_webhook.LineTransport):
            def _call(self, method, url, payload=None):
                if url.endswith("/message/reply"):   # not the rich menu calls of the start-up sync
                    replies.append(payload)
                return b"{}"
        admin = "U" + "0123456789abcdef" * 2
        env = {"LINE_V2_CHANNEL_SECRET": "s", "LINE_V2_CHANNEL_ACCESS_TOKEN": "t", "LINE_V2_PUBLIC_BASE_URL": "",
               "LINE_V2_ADMIN_USER_IDS": f"qteddie1322,{admin}"}
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, env), \
                mock.patch.object(demo, "DemoApp", lambda *a, **k: FakeAnalyzer()), \
                mock.patch.object(line_webhook, "LineTransport", Transport):
            client = TestClient(line_webhook.create_app(Path(folder), Path("m.pt"), roster=None,
                                                         template=Path("t.json"), folder=Path(folder)))
            for user in (admin, "U" + "9" * 32):
                body = json.dumps({"events": [text_event(user, "我的設定")]}).encode()
                signature = base64.b64encode(hmac.new(b"s", body, hashlib.sha256).digest()).decode()
                seen = len(replies)
                self.assertEqual(client.post("/line/callback", content=body,
                                             headers={"x-line-signature": signature}).status_code, 200)
                for _ in range(50):
                    if len(replies) > seen: break
                    time.sleep(.05)
            self.assertIn("管理員：是", replies[0]["messages"][0]["text"])
            self.assertNotIn("管理員", replies[1]["messages"][0]["text"])


if __name__ == "__main__":
    unittest.main()


class RegisterShowsTodayTests(BotCase):
    """A first registration answers with the 我的班表 card (and its photo): today's roster first, even once
    tomorrow's is already up."""
    settings = {"notify": False, "public_base": "https://bot.example"}

    def register_at(self, hour):
        with self.at(datetime(2026, 10, 8, hour, 30, tzinfo=TAIPEI)):
            self.ask("設定 林小明")
        return self.transport.raw[-1]

    def test_today_comes_before_a_newer_tomorrow(self):
        self.store.add_roster("Cteam", "run-today", "U9", 5, "2026-10-08", 100)
        self.store.add_roster("Cteam", "run-tomorrow", "U9", 5, "2026-10-09", 200)
        said, card, photo = self.register_at(6)
        self.assertTrue(said["text"].startswith("已登記：林小明"))
        self.assertEqual(flex_texts(card["contents"]["header"])[0], "📅 林小明的班表")   # the new card, not duty_card
        texts = flex_texts(card["contents"]["body"])
        self.assertLess(texts.index("☀️ 今天 10/8（四）"), texts.index("🌙 明天 10/9（五）"))
        self.assertEqual(texts[texts.index("☀️ 今天 10/8（四）") + 1], "代號 72・共 3 小時")
        self.assertEqual(photo["originalContentUrl"], "https://bot.example/runs/run-today/people/72.jpg")
        self.assertNotIn("quickReply", photo)

    def test_without_today_tomorrow_is_on_the_card(self):
        self.store.add_roster("Cteam", "run-tomorrow", "U9", 5, "2026-10-09", 200)
        said, card, photo = self.register_at(9)
        texts = flex_texts(card["contents"])
        self.assertIn("今天的班表還沒上傳", texts)
        self.assertIn("🌙 明天 10/9（五）", texts)
        self.assertEqual(photo["originalContentUrl"], "https://bot.example/runs/run-tomorrow/people/72.jpg")

    def test_without_any_roster_it_says_how_rosters_arrive(self):
        said, = self.register_at(9)
        self.assertIn("已登記：林小明", said["text"])
        self.assertIn("目前還沒有班表", said["text"])

    def test_without_photos_only_the_words_and_the_card(self):
        self.bot.settings.images = False
        self.store.add_roster("Cteam", "run-today", "U9", 5, "2026-10-08", 100)
        said, card = self.register_at(6)
        self.assertEqual(card["type"], "flex")
