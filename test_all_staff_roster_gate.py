"""名冊守門員: in a private chat only someone whose registered name is on the 名冊 sees anything of the team.

The 名冊 is the staff list the 幹部 keep (``staff_names.txt``) and the names of the fixed codes. Administrators and
幹部 are always let in. Anyone else can register a name on the 名冊, read the help, find their user id, send 「意見」
and open 我的設定; everything else answers with one card. Without a 名冊 the gate is open.
"""
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from all_staff.bot import (FLEX_BUBBLE_LIMIT, FLEX_CAROUSEL_LIMIT, GATE_POSTBACKS, TEXT_LIMIT, flex_size, gate_card,
                           help_card, welcome_card)
from test_all_staff_bot import BotCase, flex_nodes, flex_texts, image_event, postback_event, text_event
from test_all_staff_flow import MESSAGE as FLOW_MESSAGE

STAFF = "# 名冊\n林小明\n陳小華\n\n# 役男\n蕭志明\n"
FIXED = {"己": "蕭志明", "甲": "陳志明"}              # 陳志明 is on the 名冊 through a fixed code only
SECRETS = ("林小明", "陳小華", "蕭志明", "陳志明", "王大同", "run-1")   # the 名冊, names read off the roster, its id
CAPTAIN, ADMIN = "U8", "U9"                            # a 幹部, an administrator
GATE_TILE = "✏️ 登記名冊上的姓名"
FLOW_DAY = date(2026, 10, 6)                           # the day of FLOW_MESSAGE


def follow_event(user):
    event = text_event(user, "")
    event["type"] = "follow"
    del event["message"]
    return event


def check_line_limits(test, message):
    """What LINE refuses or cuts: empty or long texts, long labels and data, too many quick replies, a bubble over
    30 KB (``FLEX_BUBBLE_LIMIT`` leaves room), an altText past the bot's 400 characters."""
    if message["type"] == "text":
        test.assertTrue(message["text"].strip())
        test.assertLessEqual(len(message["text"]), TEXT_LIMIT)
    if message["type"] == "flex":
        test.assertTrue(message["altText"].strip())
        test.assertLessEqual(len(message["altText"]), 400)
        limit = FLEX_CAROUSEL_LIMIT if message["contents"]["type"] == "carousel" else FLEX_BUBBLE_LIMIT
        test.assertLessEqual(flex_size(message["contents"]), limit)
        test.assertTrue(all(node["text"] for node in flex_nodes(message["contents"])
                            if node.get("type") in ("text", "span")))
    items = message.get("quickReply", {}).get("items", [])
    test.assertLessEqual(len(items), 13)
    actions = [item["action"] for item in items] + [node["action"] for node in flex_nodes(message.get("contents", {}))
                                                    if node.get("action")]
    for action in actions:
        test.assertTrue(action.get("label"))
        test.assertLessEqual(len(action["label"]), 20)
        for key in ("data", "fillInText", "displayText", "text"):
            test.assertLessEqual(len(action.get(key) or ""), 300, key)


class GateCase(BotCase):
    friends = {"U1", "U2", "U3", "U4", CAPTAIN, ADMIN}
    settings = {"notify": False, "admins": (ADMIN,)}

    def setUp(self):
        super().setUp()
        root = Path(self.folder.name)
        self.names, self.fixed = root / "staff_names.txt", root / "fixed_staff_codes.json"
        self.names.write_text(STAFF, encoding="utf-8")
        self.fixed.write_text(json.dumps(FIXED, ensure_ascii=False), encoding="utf-8")
        self.bot.settings.staff_names, self.bot.settings.fixed_staff_codes = self.names, self.fixed
        self.store.add_officer(CAPTAIN, "隊長", "seed")
        self.store.add_roster("Cgroup", "run-1", "U2", 5, "2026-10-01")   # the team's roster, not to be shown

    def tearDown(self):
        for messages in self.transport.raw + [messages for _, messages in self.transport.raw_pushes]:
            for message in messages:
                check_line_limits(self, message)
        super().tearDown()

    def name(self, user):
        return (self.store.user(user) or {}).get("staff_name")

    def tap(self, user, data):
        self.bot.handle(postback_event(user, data))

    def answered(self):
        return len(self.transport.replies)

    def assertGated(self, words="", *, own=()):
        """The last answer is the gate card and nothing else, naming nobody on the 名冊 but ``own``."""
        messages = self.transport.raw[-1]
        self.assertEqual(len(messages), 1, words)
        card = messages[0]
        self.assertEqual((card["type"], card["contents"]["type"]), ("flex", "bubble"), words)
        self.assertIn(GATE_TILE, flex_texts(card["contents"]), words)
        self.assertNotLeaked(messages, own=own, words=words)
        tile = next(node for node in flex_nodes(card) if node.get("action", {}).get("data") == "guide|設定姓名")
        self.assertEqual((tile["action"]["inputOption"], tile["action"]["fillInText"]), ("openKeyboard", "設定 "))
        return card

    def assertNotLeaked(self, messages, *, own=(), words=""):
        dump = json.dumps(messages, ensure_ascii=False)
        for secret in SECRETS:
            if secret not in own:
                self.assertNotIn(secret, dump, words)


class RegistrationTests(GateCase):
    def test_a_name_on_the_roster_registers_and_lets_them_in(self):
        self.assertIn("已登記：陳小華", self.ask("設定 陳小華", "U3"))
        self.assertEqual(self.name("U3"), "陳小華")
        self.ask("查看時段", "U3")
        self.assertNotIn(GATE_TILE, self.reply())
        self.assertIn("已登記：陳志明", self.ask("設定 陳志明", "U4"))      # on the 名冊 by a fixed code only

    def test_a_name_off_the_roster_is_refused_kindly_and_changes_nothing(self):
        reply = self.ask("設定 王大明", "U3")
        self.assertIn("🙏 名冊上沒有「王大明」，所以沒有登記。", reply)
        self.assertIn("請確認姓名有沒有打錯字", reply)
        self.assertIn("新進同仁請先請幹部把你加入名冊（人員管理 → 新增人員），加好之後再傳一次「設定 你的姓名」", reply)
        self.assertNotLeaked(self.transport.raw[-1])
        action = self.transport.raw[-1][0]["quickReply"]["items"][0]["action"]
        self.assertEqual((action["label"], action["fillInText"]), ("✏️ 重新輸入姓名", "設定 "))
        for words in ("綁定 王大明", "登記 王大明", "姓名 王大明", "我是 王大明", "設定王大明"):   # every way of registering
            self.assertIn("名冊上沒有「王大明」", self.ask(words, "U3"), words)
        self.assertIsNone(self.store.user("U3"))
        self.assertEqual(self.store.pending_name_requests(), [])
        self.assertEqual(self.transport.raw_pushes, [])
        self.ask("查看時段", "U3")
        self.assertGated()

    def test_a_taken_name_on_the_roster_still_goes_to_the_administrators(self):
        self.register("U1", "林小明")
        self.assertIn("「林小明」已經有其他 LINE 帳號登記了", self.ask("設定 林小明", "U3"))
        self.assertIsNone(self.name("U3"))
        self.assertEqual([who for who, _ in self.transport.raw_pushes], [ADMIN])   # not the 幹部
        self.ask("我的班表", "U3")                                          # not in while it waits
        card = self.assertGated(own=("林小明",))                            # their own request, named
        self.assertIn("⏳ 你申請的「林小明」還在等管理員確認，通過後就能使用。", flex_texts(card["contents"]))

    def test_a_change_request_to_a_name_off_the_roster_is_refused_before_it_is_filed(self):
        self.register("U1", "林小明")
        reply = self.ask("設定 王大明")
        self.assertIn("名冊上沒有「王大明」，所以沒有送出改名申請，你的姓名還是「林小明」。", reply)
        self.assertEqual(self.name("U1"), "林小明")
        self.assertIsNone(self.store.pending_name_request("U1"))
        self.assertEqual(self.transport.raw_pushes, [])

    def test_an_approval_after_the_name_left_the_roster_is_turned_down(self):
        self.register("U1", "林小明")
        self.ask("設定 陳小華")
        number = self.store.pending_name_request("U1")["id"]
        self.ask("刪除人員 陳小華", CAPTAIN)                                  # 陳小華 leaves meanwhile
        pushes = len(self.transport.raw_pushes)
        self.tap(ADMIN, f"rename|{number}|yes")
        reply = self.reply()
        self.assertIn("⚠️「陳小華」目前不在名冊上，所以這次沒有同意，林小明（LINE：明明）的姓名沒有變更，申請還在等。", reply)
        self.assertIn("請先把「陳小華」加進名冊（傳「新增人員 陳小華」），再按一次同意；不然請按不同意。", reply)
        self.assertEqual(self.name("U1"), "林小明")
        self.assertEqual(self.store.name_request(number)["status"], "pending")
        self.assertEqual(len(self.transport.raw_pushes), pushes)              # the requester is not told anything yet
        action = self.transport.raw[-1][0]["quickReply"]["items"][0]["action"]
        self.assertEqual((action["label"], action["text"]), ("➕ 加入名冊", "新增人員 陳小華"))
        self.ask(action["text"], ADMIN)                                      # back on the 名冊: now it counts
        self.tap(ADMIN, f"rename|{number}|yes")
        self.assertEqual(self.name("U1"), "陳小華")
        self.assertIn("✅ 已同意", self.reply())

    def test_a_refusal_needs_no_roster(self):
        self.register("U1", "林小明")
        self.ask("設定 陳小華")
        number = self.store.pending_name_request("U1")["id"]
        self.ask("刪除人員 陳小華", CAPTAIN)
        self.tap(ADMIN, f"rename|{number}|no")
        self.assertIn("❌ 已婉拒", self.reply())
        self.assertEqual(self.store.name_request(number)["status"], "refused")

    def test_officers_and_administrators_register_names_on_the_roster_too(self):
        reply = self.ask("設定 林大明", CAPTAIN)                              # a typo would stay on their account
        self.assertIn("名冊上沒有「林大明」", reply)
        self.assertIn("名冊上沒有「王大華」", self.ask("設定 王大華", ADMIN))
        self.assertEqual((self.name(CAPTAIN), self.name(ADMIN)), (None, None))
        self.ask("人員管理", CAPTAIN)                                          # still let in, unregistered
        self.assertNotIn("請先登記", "\n".join(flex_texts(self.transport.raw[-1][0]["contents"])))
        self.assertIn("已登記：蕭志明", self.ask("設定 蕭志明", CAPTAIN))

    def test_a_code_is_no_way_in(self):
        reply = self.ask("設定 72", "U3")
        self.assertIn("請改用姓名登記，例如「設定 王小明」。", reply)
        self.assertNotIn("我是 72", reply)                                   # only offered to those let in
        self.assertIn("我是 72", self.ask("設定 72", CAPTAIN))


class WhitelistTests(GateCase):
    """U3 has registered nothing: only what registering needs works."""

    def test_the_help_and_the_user_id(self):
        self.ask("用法說明", "U3")
        carousel, = self.transport.raw[-1]
        self.assertEqual(carousel["contents"]["type"], "carousel")
        self.assertTrue(any("名冊" in line for line in flex_texts(carousel["contents"])))
        for words in ("說明", "help", "？"):
            self.ask(words, "U3")
            self.assertEqual(self.transport.raw[-1][0]["contents"]["type"], "carousel", words)
        self.ask("進階說明", "U3")
        self.assertIn("姓名要和隊上名冊上的一模一樣；新進同仁請先請幹部加入名冊（人員管理 → 新增人員），再登記。",
                      flex_texts(self.transport.raw[-1][0]["contents"]))
        self.assertIn("U3", self.ask("我的ID", "U3"))
        self.assertIn("U3", self.ask("我的 userId", "U3"))

    def test_feedback(self):
        self.assertIn("收到了，謝謝", self.ask("意見 名冊上少了我", "U3"))
        self.assertEqual(self.store.recent_feedback()[0]["body"], "名冊上少了我")
        self.assertIn("想說的話請接在「意見」後面", self.ask("意見", "U3"))
        count = self.answered()
        self.tap("U3", "fill:意見")                                          # the menu's 意見回饋: opens the keyboard
        self.assertEqual(self.answered(), count)

    def test_my_settings_says_how_to_get_in(self):
        reply = self.ask("我的設定", "U3")
        self.assertIn("姓名：尚未登記", reply)
        self.assertIn("輸入「設定 你的姓名」（例如 設定 王小明）登記姓名。", reply)
        self.assertIn("🗂️ 姓名要和隊上名冊上的一模一樣", reply)
        self.assertNotIn(GATE_TILE, reply)
        labels = [item["action"]["label"] for item in self.transport.raw[-1][0]["quickReply"]["items"]]
        self.assertEqual(labels, ["設定姓名"])
        self.assertNotLeaked(self.transport.raw[-1])

    def test_the_greeting_and_its_tile(self):
        self.bot.handle(follow_event("U3"))
        welcome, = self.transport.raw[-1]
        texts = flex_texts(welcome["contents"])
        self.assertIn("第一步：登記你的姓名", texts)
        self.assertIn("🗂️ 姓名要和隊上名冊上的一模一樣；新進同仁請先請幹部加入名冊（人員管理 → 新增人員），再登記。", texts)
        self.assertNotLeaked([welcome])
        self.tap("U3", "guide|設定姓名")
        self.assertIn("在「設定 」後面打你的姓名送出就好，例如：設定 王小明\n姓名要和隊上名冊上的一模一樣。", self.reply())
        count = self.answered()
        self.tap("U3", "fill:設定")                                          # 「✏️ 設定姓名」 only opens the keyboard
        self.assertEqual(self.answered(), count)

    def test_everything_else_answers_with_the_gate_card_alone(self):
        self.register("U1", "林小明")
        self.bot.settings.notify = True
        before = (self.store.hourly_wage("U3"), self.store.roster_by_run("run-1")["roster_date"])
        for words in ("我的班表", "班表", "今天", "明天", "10/1", "我的班表 10/1", "查 72", "72", "查 林小明",
                      "查詢人員", "查看時段", "10-11 國際", "查看工作", "勤務人員", "勤務人員 10/1", "休假人員",
                      "人流預測", "人流預測 10/6", FLOW_MESSAGE, "本月統計", "9月統計", "加班", "時薪 200",
                      "設定時薪 200", "通知 開", "我是 72", "我是 10/1 72", "日期 10/1", "姓名申請", "人員管理",
                      "人員名冊", "新增人員 王大明", "役男 丁 王大明", "解除", "意見列表", "你好"):
            count = self.answered()
            self.ask(words, "U3")
            self.assertEqual(self.answered(), count + 1, words)
            self.assertGated(words)
        self.assertEqual((self.store.hourly_wage("U3"), self.store.roster_by_run("run-1")["roster_date"]), before)
        self.assertEqual(self.store.claims("run-1"), {})
        self.assertIsNone(self.store.flow_on("U3", FLOW_DAY))
        self.assertEqual(self.store.recent_feedback(), [])
        self.assertEqual(self.names.read_text(encoding="utf-8"), STAFF)
        self.assertEqual(self.transport.raw_pushes, [])

    def test_every_postback_but_the_keyboard_ones_too(self):
        for data in ("roster|run-1", "person|run-1|72", "mine|run-1", "staff|run-1", "slots|run-1", "slot|run-1|5",
                     "area|run-1|5|intl", "works|run-1", "leave|run-1", "stats|2026-10", "cal|2026-10",
                     "cal|2026-10-01", "wage", "flow|2026-10-06", "fill:查", "rename|1|yes", "guide|新增人員",
                     "fixed|己", "setcode|丁|王大明|empty", "date|run-1|2026-10-01", "noop"):
            count = self.answered()
            self.tap("U3", data)
            self.assertEqual(self.answered(), count + 1, data)
            self.assertGated(data)
        self.assertEqual(set(GATE_POSTBACKS), {"guide|設定姓名", "fill:設定", "fill:意見"})

    def test_a_private_photo_is_not_read(self):
        self.bot.handle(image_event("U3"))
        self.assertGated()
        self.assertEqual(self.analyzer.calls, 0)
        self.assertIsNone(self.store.own_upload("U3"))

    def test_a_sticker_stays_unanswered_as_before(self):
        event = text_event("U3", "")
        event["message"] = {"type": "sticker", "id": "s", "packageId": "1", "stickerId": "1"}
        self.bot.handle(event)
        self.assertEqual(self.transport.replies, [])

    def test_in_a_group_nothing_changes(self):
        self.bot.handle(image_event("U3", "Cgroup"))                         # read silently, as ever
        self.assertEqual(self.analyzer.calls, 1)
        self.bot.handle(text_event("U3", FLOW_MESSAGE, "Cgroup"))
        self.assertIsNotNone(self.store.flow_on("U1", FLOW_DAY))             # the team's 航班人數 for that day
        self.assertEqual(self.transport.replies, [])
        self.assertEqual([who for who, _ in self.transport.raw_pushes], [ADMIN, ADMIN])   # the two reports, as ever
        report = self.transport.raw_pushes[-1][1][0]
        link, = [node["action"] for node in flex_nodes(report["contents"].get("footer", {})) if node.get("action")]
        self.tap(ADMIN, link["data"])                                        # its button: the gate lets them through
        self.assertTrue(self.transport.raw[-1][-1]["altText"].startswith("10/6（二）人流預測"))


class RemovedNameTests(GateCase):
    def setUp(self):
        super().setUp()
        self.register("U1", "林小明")

    def test_a_name_removed_from_the_roster_loses_access(self):
        self.ask("查看時段")
        self.assertNotIn(GATE_TILE, self.reply())
        self.ask("刪除人員 林小明", CAPTAIN)                                 # left the team
        self.ask("查看時段")
        card = self.assertGated(own=("林小明",))
        texts = flex_texts(card["contents"])
        self.assertEqual(texts[0], "🔒 姓名不在名冊上")
        self.assertIn("⚠️ 你登記的姓名「林小明」目前不在名冊上（可能已經離隊，或名冊上不是這樣寫）。如果你還在隊上，請聯絡幹部。",
                      texts)
        self.assertEqual(card["altText"], "🔒 你登記的「林小明」目前不在名冊上，暫時看不到班表；如果你還在隊上，請聯絡幹部。")
        self.assertIn("⚠️「林小明」目前不在名冊上，暫時看不到班表；如果你還在隊上，請聯絡幹部。", self.ask("我的設定"))
        self.bot.handle(follow_event("U1"))                                  # back after blocking: not welcomed in
        self.assertGated(own=("林小明",))
        self.ask("新增人員 林小明", CAPTAIN)                                 # back on the 名冊: back in
        self.ask("查看時段")
        self.assertNotIn(GATE_TILE, self.reply())
        self.bot.handle(follow_event("U1"))
        self.assertIn("歡迎回來，林小明", self.reply())

    def test_typing_the_removed_name_again_explains(self):
        self.ask("刪除人員 林小明", CAPTAIN)
        reply = self.ask("設定 林小明")
        self.assertIn("🙏「林小明」目前不在名冊上，所以暫時看不到班表。\n如果你還在隊上，請聯絡幹部。", reply)
        self.assertNotIn("本來就是你登記的姓名", reply)

    def test_a_fixed_code_holder_removed_loses_access_too(self):
        self.register("U4", "陳志明")
        self.ask("刪除人員 陳志明", CAPTAIN)                                 # clears 甲 as well
        self.ask("勤務人員", "U4")
        self.assertGated(own=("陳志明",))


class BypassTests(GateCase):
    def test_officers_and_administrators_are_let_in_unregistered(self):
        for user in (CAPTAIN, ADMIN):
            self.ask("勤務人員", user)
            self.assertNotIn(GATE_TILE, self.reply(), user)
            self.assertIn("勤務人員", self.reply(), user)
        self.tap(CAPTAIN, "roster|run-1")
        self.assertEqual(self.transport.raw[-1][0]["type"], "text")         # no public_base: the photo cannot be had
        self.assertNotIn(GATE_TILE, self.reply())

    def test_an_officer_registered_off_the_roster_stays_in_until_no_longer_one(self):
        self.register(CAPTAIN, "林大明")
        self.ask("我的班表", CAPTAIN)
        self.assertNotIn(GATE_TILE, self.reply())
        self.store.remove_officer(CAPTAIN)
        self.ask("我的班表", CAPTAIN)
        self.assertGated(own=("林大明",))


class EmptyRosterTests(BotCase):
    settings = {"notify": False}

    def test_an_empty_roster_opens_the_gate_with_one_warning(self):
        names = Path(self.folder.name) / "staff_names.txt"
        names.write_text("# 名冊\n\n", encoding="utf-8")
        self.bot.settings.staff_names = names
        self.store.add_roster("Cgroup", "run-1", "U2", 5, "2026-10-01")
        log = io.StringIO()
        with redirect_stdout(log):
            self.assertIn("已登記：王大明", self.ask("設定 王大明", "U3"))
            self.ask("勤務人員", "U3")
            self.ask("用法說明", "U3")
        self.assertNotIn(GATE_TILE, self.reply())
        self.assertEqual(log.getvalue().count("名冊是空的"), 1)
        self.assertFalse(any("名冊" in line for line in flex_texts(self.transport.raw[-1][0]["contents"])))

    def test_no_roster_files_at_all_is_open_and_quiet(self):
        log = io.StringIO()
        with redirect_stdout(log):
            self.assertIn("已登記：王大明", self.ask("設定 王大明", "U3"))
            self.ask("勤務人員", "U3")
        self.assertNotIn(GATE_TILE, self.reply())
        self.assertNotIn("名冊是空的", log.getvalue())


class NotificationTests(GateCase):
    settings = {"notify": True, "admins": (ADMIN,)}

    def test_only_those_let_in_are_told(self):
        self.register("U1", "林小明", notify=True)
        self.register("U3", "蕭志明", notify=True)
        self.store.set_staff_name("U4", "王大明")                            # registered before there was a gate
        self.store.set_notify("U4", True)
        self.store.bind("U2", "72", "華華")                                  # an old code subscriber
        self.store.set_notify("U2", True)
        self.ask("刪除人員 蕭志明", CAPTAIN)                                 # U3 has left
        self.transport.raw_pushes.clear()
        self.bot.handle(image_event("U1", "Cgroup"))
        told = [who for who, _ in self.transport.raw_pushes if who != ADMIN]   # ADMIN: the upload report
        self.assertEqual(told, ["U1"])
        self.assertEqual(len(self.bot._audience()), 1)


class AdminOnlyApprovalTests(GateCase):
    def test_a_officers_tap_is_ignored_and_the_list_is_not_theirs(self):
        self.register("U1", "林小明")
        self.register(CAPTAIN, "蕭志明")
        self.ask("設定 陳小華")
        number = self.store.pending_name_request("U1")["id"]
        self.assertEqual([who for who, _ in self.transport.raw_pushes], [ADMIN])   # no card for the 幹部
        count = self.answered()
        self.tap(CAPTAIN, f"rename|{number}|yes")
        self.assertEqual(self.answered(), count)                             # ignored, as a stale button is
        self.assertEqual(self.store.name_request(number)["status"], "pending")
        reply = self.ask("姓名申請", CAPTAIN)
        self.assertEqual(self.transport.raw[-1][0]["type"], "text")         # the non-approver's answer
        self.assertIn("你的姓名是「蕭志明」。要改姓名，請輸入「設定 新的姓名」，會交給管理員確認。", reply)
        self.assertNotIn("陳小華", reply)
        self.tap(ADMIN, f"rename|{number}|yes")
        self.assertEqual(self.name("U1"), "陳小華")


class ReviewFixTests(GateCase):
    def test_clearing_a_fixed_code_keeps_its_holder_on_the_roster(self):
        self.ask("設定 陳志明", "U4")                                        # on the 名冊 through 甲 only
        self.ask("役男 甲 取消", CAPTAIN)
        self.assertIn("陳志明", self.names.read_text(encoding="utf-8"))
        self.ask("我的班表", "U4")
        self.assertNotIn(GATE_TILE, "\n".join(flex_texts(self.transport.raw[-1][0].get("contents", {}))))

    def test_giving_an_english_code_to_someone_else_keeps_the_former_holder_listed(self):
        self.fixed.write_text(json.dumps({**FIXED, "A": "王大同"}, ensure_ascii=False), encoding="utf-8")
        self.assertNotIn("王大同", self.names.read_text(encoding="utf-8"))     # on the 名冊 through the code only
        self.register("U4", "王大同")
        self.ask("代號 A 陳小華", CAPTAIN)
        self.assertIn("王大同", self.names.read_text(encoding="utf-8"))        # written there: he is not locked out
        self.ask("我的班表", "U4")
        self.assertNotIn(GATE_TILE, "\n".join(flex_texts(self.transport.raw[-1][0].get("contents", {}))))

    def test_a_conscript_replaced_on_his_code_is_off_the_roster_and_kept_out(self):
        self.register("U4", "陳志明")                                         # on the 名冊 through 甲 only
        self.ask("役男 甲 陳小華", CAPTAIN)
        self.assertNotIn("陳志明", self.names.read_text(encoding="utf-8"))
        self.ask("我的班表", "U4")
        self.assertIn(GATE_TILE, flex_texts(self.transport.raw[-1][0]["contents"]))

    def test_a_request_that_can_no_longer_be_approved_is_not_promised_on_the_gate(self):
        self.register("U4", "林小明")                                         # someone else has 林小明
        self.ask("設定 林小明", "U3")                                         # so U3's goes to the administrator
        self.ask("查 72", "U3")
        self.assertIn("⏳", "\n".join(flex_texts(self.transport.raw[-1][0]["contents"])))
        self.names.write_text(STAFF.replace("林小明\n", ""), encoding="utf-8")   # then it leaves the 名冊
        self.ask("查 72", "U3")
        self.assertNotIn("⏳", "\n".join(flex_texts(self.transport.raw[-1][0]["contents"])))


class WithoutAdministratorTests(GateCase):
    settings = {"notify": False, "admins": ()}

    def test_a_name_off_the_roster_is_not_said_to_be_filed_when_nobody_could_approve(self):
        self.register("U1", "林小明")
        reply = self.ask("設定 王大華")
        self.assertIn("名冊上沒有「王大華」，所以沒有改名，你的姓名還是「林小明」。", reply)
        self.assertNotIn("申請", reply.splitlines()[0])

    def test_an_uploader_kept_out_is_not_told_to_correct_the_date(self):
        self.analyzer.data["run"]["roster_date"] = None
        self.bot.handle(image_event("U3", "Cgroup"))                         # U3 registered nothing
        self.assertFalse([to for to, _ in self.transport.raw_pushes if to == "U3"])
        self.register("U1", "林小明")
        self.analyzer.data["run"]["id"] = "run-2"
        self.bot.handle(image_event("U1", "Cgroup"))                         # someone let in still is
        notice, = [messages[-1] for to, messages in self.transport.raw_pushes if to == "U1"]
        picker = notice["quickReply"]["items"][0]["action"]
        self.assertEqual((picker["type"], picker["data"]), ("datetimepicker", "redate|run-2"))   # for that very photo
        self.assertNotIn("日期 10/1", notice["text"])


class CardTests(unittest.TestCase):
    def cards(self):
        return [gate_card(), gate_card(removed="歐陽娜娜娜"), gate_card(pending="歐陽娜娜娜"),
                gate_card(removed="歐陽娜娜娜", pending="歐陽小明明"), welcome_card(gated=True),
                welcome_card(locked=False, gated=True), help_card(gated=True), help_card(True, gated=True, notify=True)]

    def test_within_lines_limits_and_in_the_house_colours(self):
        for card in self.cards():
            check_line_limits(self, card)
            for node in flex_nodes(card):
                for key in ("color", "backgroundColor", "borderColor", "startColor", "endColor"):
                    if key in node:
                        self.assertRegex(node[key], r"^#[0-9A-F]{6}([0-9A-F]{2})?$", key)

    def test_the_gate_card_says_why_and_offers_the_way_in(self):
        card = gate_card()
        bubble = card["contents"]
        self.assertEqual((bubble["size"], bubble["header"]["background"]["type"]), ("giga", "linearGradient"))
        texts = flex_texts(bubble)
        self.assertEqual(texts[0], "🔒 請先登記姓名")
        self.assertIn("為了保護同仁的班表和個人資料，登記的姓名要在隊上的名冊上，才能查看班表、時段、人員和統計。", texts)
        self.assertIn("🆕 新進同仁：請先請幹部把你加入名冊（人員管理 → 新增人員），再登記一次。", texts)
        self.assertFalse(any(re.search("[他她]", line) for line in texts + [card["altText"]]))
        self.assertLess(flex_size(bubble), 6_000)


if __name__ == "__main__":
    unittest.main()
