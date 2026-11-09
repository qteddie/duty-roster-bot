"""姓名申請: a registered name is locked; changing it, or taking a name another account has, needs an approver.

The approvers are the administrators (``Settings.admins``), each pushed one card with ✅ 同意／❌ 不同意; the first
of them to tap decides. The 幹部 (``Store.officers``) keep 人員管理 but do not answer these.
"""
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from all_staff.bot import (FLEX_BUBBLE_LIMIT, MORE_HELP, NAME_PUSH_GAP, NAME_SHOWN, TAIPEI, Store, flex_size,
                           help_card, name_request_card, welcome_card)
from test_all_staff_bot import BotCase, flex_nodes, flex_texts, postback_event, tappable

# a 幹部, an administrator who is a 幹部 too, another administrator, a 幹部 who blocked the bot
CAPTAIN, ADMIN, DEPUTY, SENIOR = "U8", "U9", "U6", "U7"


def without_time(box):
    """A ✅／❌ box's postback without the moment its card was built."""
    return box["action"]["data"].rsplit("|", 1)[0]


class Case(BotCase):
    def name(self, user):
        """The name ``user`` has registered (None: none yet, or no row at all)."""
        return (self.store.user(user) or {}).get("staff_name")


class NameLockCase(Case):
    friends = {"U1", "U2", "U3", CAPTAIN, ADMIN, DEPUTY}
    settings = {"notify": False, "admins": (ADMIN, DEPUTY)}

    def setUp(self):
        super().setUp()
        self.store.add_officer(CAPTAIN, "隊長", "seed")
        self.store.add_officer(ADMIN, None, "seed")
        self.store.set_staff_name(CAPTAIN, "林大明", "大明")
        self.store.set_staff_name(DEPUTY, "陳大文", "大文")
        self.register("U1", "林小明")

    def pushed(self, to):
        return [messages for who, messages in self.transport.raw_pushes if who == to]

    def card_for(self, to):
        """The last name request card pushed to ``to``."""
        return next(message for messages in reversed(self.pushed(to)) for message in messages
                    if message["type"] == "flex" and "姓名" in message["altText"])

    def tap(self, user, number, answer):
        self.bot.handle(postback_event(user, f"rename|{number}|{answer}"))

    def request(self, user="U1"):
        return self.store.pending_name_request(user)


class ChangeRequestTests(NameLockCase):
    def test_a_registered_name_is_not_changed_by_typing_another(self):
        reply = self.ask("設定 王小明")
        self.assertEqual(self.name("U1"), "林小明")
        self.assertIn("改名申請送出了：林小明 → 王小明", reply)
        self.assertIn("交給管理員確認，有結果會私訊通知你", reply)
        self.assertIn("你的姓名還是「林小明」", reply)
        request = self.request()
        self.assertEqual((request["current_name"], request["requested_name"], request["status"]),
                         ("林小明", "王小明", "pending"))
        for words in ("綁定 王小明", "登記 王小明", "姓名 王小明", "我是 王小明"):   # every way of registering
            self.ask(words)
            self.assertEqual(self.name("U1"), "林小明", words)

    def test_every_administrator_gets_one_card_and_no_officer_does(self):
        self.bot.settings.admins = (ADMIN, DEPUTY, ADMIN)                    # listed twice: still one card
        self.ask("設定 王小明")
        self.assertEqual(sorted(who for who, _ in self.transport.raw_pushes), [DEPUTY, ADMIN])   # ADMIN once
        self.assertEqual(self.pushed(CAPTAIN), [])                           # a 幹部 does not answer these
        card = self.card_for(ADMIN)
        self.assertEqual(self.pushed(ADMIN), [[card]])
        bubble = card["contents"]
        self.assertEqual((bubble["type"], bubble["size"]), ("bubble", "giga"))
        self.assertEqual(bubble["header"]["background"]["type"], "linearGradient")
        texts = flex_texts(bubble)
        for words in ("🙋 姓名變更申請", "林小明 想改名", "林小明", "王小明", "✅ 同意", "❌ 不同意", "改成「王小明」", "維持「林小明」"):
            self.assertIn(words, texts)
        self.assertTrue(any("申請人：林小明（LINE：明明）" in line for line in texts))
        number = self.request()["id"]
        self.assertEqual([without_time(box) for box in tappable(card, "rename|")],
                         [f"rename|{number}|yes", f"rename|{number}|no"])
        self.assertTrue(all(box["action"]["data"].rsplit("|", 1)[1].isdigit() for box in tappable(card, "rename|")))
        self.assertIn("林小明", card["altText"])
        self.assertIn("王小明", card["altText"])

    def test_approving_changes_the_name_and_tells_the_requester(self):
        self.store.add_roster("Cgroup", "run-1", "U2", 5, "2026-10-01")
        self.analyzer.data["names"]["72"] = ["王小明"]
        self.ask("設定 王小明")
        number = self.request()["id"]
        with self.at(datetime(2026, 10, 1, 6, 30, tzinfo=TAIPEI)):
            self.tap(DEPUTY, number, "yes")
        self.assertEqual(self.name("U1"), "王小明")
        self.assertIn("✅ 已同意：林小明（LINE：明明） 改名為「王小明」", self.reply())
        said, card = self.pushed("U1")[-1]
        self.assertIn("改名申請通過了！你的姓名現在是「王小明」", said["text"])
        self.assertEqual(flex_texts(card["contents"]["header"])[0], "📅 王小明的班表")   # 我的班表 under the new name
        self.assertIn("代號 72・共 3 小時", flex_texts(card["contents"]["body"]))
        request = self.store.name_request(number)
        self.assertEqual((request["status"], request["decided_by"]), ("approved", DEPUTY))

    def test_refusing_keeps_the_name_and_tells_the_requester(self):
        self.ask("設定 王小明")
        number = self.request()["id"]
        self.tap(ADMIN, number, "no")
        self.assertEqual(self.name("U1"), "林小明")
        self.assertIn("❌ 已婉拒", self.reply())
        said, = self.pushed("U1")[-1]
        self.assertIn("改名申請沒有通過，姓名維持「林小明」", said["text"])
        request = self.store.name_request(number)
        self.assertEqual((request["status"], request["decided_by"]), ("refused", ADMIN))
        self.assertIsNotNone(request["decided_at"])

    def test_the_first_answer_decides_and_later_taps_are_told_who_answered(self):
        self.ask("設定 王小明")
        number = self.request()["id"]
        self.tap(DEPUTY, number, "yes")
        pushes = len(self.transport.raw_pushes)
        self.tap(ADMIN, number, "no")                                        # another approver, too late
        self.assertIn("這筆姓名申請已經處理過了：陳大文在", self.reply())
        self.assertIn("同意了（林小明（LINE：明明） 改名為「王小明」）。這次沒有變更。", self.reply())
        self.tap(DEPUTY, number, "no")                                       # the same one, again
        self.assertIn("已經處理過了：你在", self.reply())
        self.assertEqual(self.name("U1"), "王小明")
        self.assertEqual(len(self.transport.raw_pushes), pushes)             # nobody is told twice

    def test_only_approvers_can_decide(self):
        self.ask("設定 王小明")
        number = self.request()["id"]
        replies = len(self.transport.replies)
        for user in ("U1", "U2", CAPTAIN):                                    # the requester, anyone else, a 幹部
            self.tap(user, number, "yes")
        self.assertEqual(len(self.transport.replies), replies)               # ignored, as a stale button is
        self.assertEqual(self.request()["status"], "pending")
        self.assertEqual(self.name("U1"), "林小明")

    def test_a_new_request_replaces_the_pending_one(self):
        self.ask("設定 王小明")
        first = self.request()["id"]
        reply = self.ask("設定 陳小明")
        self.assertIn("之前申請的「王小明」已經取消，以這次為準", reply)
        self.assertEqual(self.request()["requested_name"], "陳小明")
        self.assertEqual(self.store.name_request(first)["status"], "replaced")
        self.assertEqual(len(self.store.pending_name_requests()), 1)          # one pending request a person
        self.tap(DEPUTY, first, "yes")                                       # the old card
        self.assertIn("已經處理過了：申請人後來送出了新的申請", self.reply())
        self.assertEqual(self.name("U1"), "林小明")

    def test_asking_again_for_the_same_name_pushes_nobody_twice(self):
        self.ask("設定 王小明")
        pushes = len(self.transport.raw_pushes)
        self.assertIn("「王小明」的申請已經送出了，還在等", self.ask("設定 王小明"))
        self.assertEqual(len(self.transport.raw_pushes), pushes)

    def test_the_name_they_already_have_files_nothing_and_withdraws_a_request(self):
        pushes = len(self.transport.raw_pushes)
        self.assertIn("「林小明」本來就是你登記的姓名", self.ask("設定 林小明"))
        self.assertEqual(len(self.transport.raw_pushes), pushes)
        self.assertIsNone(self.request())
        self.ask("設定 王小明")
        number = self.request()["id"]
        self.assertIn("申請中的「王小明」已經取消", self.ask("設定 林小明"))
        self.assertEqual(self.store.name_request(number)["status"], "cancelled")
        self.tap(ADMIN, number, "yes")
        self.assertIn("已經處理過了：申請人自己取消了", self.reply())
        self.assertEqual(self.name("U1"), "林小明")

    def test_unregistering_withdraws_the_request_but_keeps_the_lock(self):
        self.ask("設定 王小明")
        reply = self.ask("解除")
        self.assertIn("已取消姓名登記", reply)
        self.assertIn("申請中的「王小明」也一併取消了", reply)
        self.assertIn("之後再登記「林小明」以外的姓名，要先經管理員同意", reply)
        self.assertIsNone(self.name("U1"))
        reply = self.ask("設定 陳小明")                                        # not a first registration
        self.assertIn("你之前登記過「林小明」，換成「陳小明」要先交給管理員確認", reply)
        request = self.request()
        self.assertEqual((request["current_name"], request["released_name"]), (None, "林小明"))   # what it has now
        texts = flex_texts(name_request_card([(request, [])])["contents"])
        self.assertIn("（之前登記過「林小明」，後來解除了）", texts)
        self.assertIn("不登記", texts)                                       # not 「維持「林小明」」
        self.assertIsNone(self.name("U1"))
        self.assertNotIn("你的姓名還是", self.reply())
        self.ask("設定 林小明")                                              # their own name comes straight back
        self.assertEqual(self.name("U1"), "林小明")
        self.assertIn("已登記：林小明", self.reply())
        self.assertIsNone(self.request())                                    # and settles the request

    def test_an_approver_asking_goes_to_the_others(self):
        self.ask("設定 陳大武", DEPUTY)
        self.assertEqual(self.name(DEPUTY), "陳大文")
        self.assertEqual([who for who, _ in self.transport.raw_pushes], [ADMIN])
        number = self.request(DEPUTY)["id"]
        self.tap(DEPUTY, number, "yes")
        self.assertIn("自己的申請要由其他管理員確認", self.reply())
        self.assertEqual(self.name(DEPUTY), "陳大文")
        self.assertIn("目前沒有等待確認的姓名申請", self.ask("姓名申請", DEPUTY))   # not their own

    def test_an_officer_asks_the_administrators_like_anyone(self):
        self.ask("設定 林大華", CAPTAIN)
        self.assertEqual(self.name(CAPTAIN), "林大明")
        self.assertEqual(sorted(who for who, _ in self.transport.raw_pushes), [DEPUTY, ADMIN])
        self.assertIn("你申請的「林大華」在", self.ask("姓名申請", CAPTAIN))   # where their own stands, no list
        self.ask("設定 王小明")
        self.assertIn("你申請的「林大華」在", self.ask("姓名申請", CAPTAIN))   # U1's is not theirs to answer
        self.assertEqual(self.transport.raw[-1][0]["type"], "text")


class FirstRegistrationTests(NameLockCase):
    def test_a_name_nobody_has_registers_at_once(self):
        reply = self.ask("設定 陳小華", "U3")
        self.assertEqual(self.name("U3"), "陳小華")
        self.assertIn("已登記：陳小華", reply)
        self.assertIn("登記後就不能自己改了；打錯字的話再傳「設定 正確的姓名」", reply)
        self.assertEqual(self.transport.raw_pushes, [])
        self.assertIsNone(self.request("U3"))

    def test_a_name_another_account_has_is_a_request(self):
        reply = self.ask("設定 林小明", "U2")
        self.assertIsNone(self.name("U2"))
        self.assertIn("「林小明」已經有其他 LINE 帳號登記了", reply)
        self.assertIn("有結果會私訊通知你", reply)
        request = self.request("U2")
        self.assertEqual((request["current_name"], request["requested_name"], request["display_name"]),
                         (None, "林小明", "華華"))
        texts = flex_texts(self.card_for(DEPUTY)["contents"])
        for words in ("🙋 姓名登記申請", "華華 想登記姓名", "還沒登記", "想登記", "登記為「林小明」", "不登記"):
            self.assertIn(words, texts)
        self.assertIn("⚠️「林小明」已經有 1 個 LINE 帳號登記（LINE：明明）。一個姓名只能登記在一個 LINE 帳號："
                      "同意後姓名轉給申請人，原本的帳號會被解除。", texts)
        self.tap(DEPUTY, request["id"], "yes")
        self.assertEqual(self.name("U2"), "林小明")
        self.assertIsNone(self.name("U1"))             # a name belongs to one LINE account: it moved
        self.assertIn("✅ 已同意：LINE：華華 登記為「林小明」", self.reply())
        self.assertIn("姓名登記通過了：林小明", self.pushed("U2")[-1][0]["text"])

    def test_a_refused_first_registration_leaves_them_unregistered(self):
        self.ask("設定 林小明", "U2")
        self.tap(ADMIN, self.request("U2")["id"], "no")
        self.assertIsNone(self.name("U2"))
        said, = self.pushed("U2")[-1]
        self.assertIn("「林小明」的登記申請沒有通過，目前還沒有登記姓名", said["text"])
        self.assertIn("請直接聯絡管理員。", said["text"])


class WithoutApproversTests(Case):
    """Without administrators (a bare install, or only 幹部) the old behaviour stays: names change at once."""
    settings = {"notify": False}

    def test_a_change_happens_at_once_with_a_warning_in_the_log(self):
        self.register("U1", "林小明")
        log = io.StringIO()
        with redirect_stdout(log):
            reply = self.ask("設定 王小明")
        self.assertEqual(self.name("U1"), "王小明")
        self.assertIn("已登記：王小明", reply)
        self.assertNotIn("不能自己改", reply)
        self.assertIn("沒有管理員可以確認姓名", log.getvalue())
        self.assertEqual(self.store.pending_name_requests(), [])

    def test_officers_alone_lock_no_name(self):
        self.store.add_officer("U2", "隊長")
        self.register("U1", "林小明")
        with redirect_stdout(io.StringIO()):
            self.ask("設定 王小明")
        self.assertEqual(self.name("U1"), "王小明")
        self.assertEqual(self.transport.raw_pushes, [])                      # no card for the 幹部
        self.ask("用法說明")
        self.assertFalse(any("申請改姓名" in line for line in flex_texts(self.transport.raw[-1][0]["contents"])))

    def test_a_taken_name_is_the_exception(self):
        """A name belongs to one LINE account: with nobody to say yes, it stays with the account that has it."""
        self.register("U1", "林小明")
        with redirect_stdout(io.StringIO()):
            reply = self.ask("設定 林小明", "U2")
        self.assertIn("已經有其他 LINE 帳號登記了", reply)
        self.assertEqual((self.name("U1"), self.name("U2")), ("林小明", None))

    def test_the_only_approver_changes_their_own_name_at_once(self):
        self.store.add_officer("U2", "隊長")
        self.register("U2", "陳小華")
        with redirect_stdout(io.StringIO()):
            self.ask("設定 陳大華", "U2")
        self.assertEqual(self.name("U2"), "陳大華")
        self.ask("我的設定", "U2")
        labels = [item["action"]["label"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertEqual(labels[0], "改姓名")


class ListingTests(NameLockCase):
    def test_an_approver_lists_the_pending_ones_each_with_its_answers(self):
        self.ask("設定 王小明")
        self.ask("設定 林小明", "U2")
        self.ask("姓名申請", ADMIN)
        card, = self.transport.raw[-1]
        texts = flex_texts(card["contents"])
        self.assertIn("📮 姓名申請", texts)
        self.assertIn("2 筆等你確認", texts)
        self.assertIn("🙋 林小明（LINE：明明）", texts)
        self.assertIn("🙋 LINE：華華", texts)
        numbers = [request["id"] for request in self.store.pending_name_requests()]
        self.assertEqual([without_time(box) for box in tappable(card, "rename|")],
                         [f"rename|{numbers[0]}|yes", f"rename|{numbers[0]}|no",
                          f"rename|{numbers[1]}|yes", f"rename|{numbers[1]}|no"])
        self.tap(ADMIN, numbers[0], "yes")
        self.assertIn("還有 1 筆姓名申請等你確認", self.reply())
        self.assertEqual(self.transport.raw[-1][-1]["quickReply"]["items"][0]["action"]["text"], "姓名申請")
        self.tap(ADMIN, numbers[1], "no")
        self.assertIn("目前沒有等待確認的姓名申請", self.ask("姓名申請", ADMIN))

    def test_a_long_list_shows_the_oldest_and_counts_the_rest(self):
        for number in range(NAME_SHOWN + 2):
            self.store.add_name_request(f"U{100 + number}", None, "林小明", f"同仁{number}")
        self.ask("姓名申請", DEPUTY)
        card, = self.transport.raw[-1]
        self.assertEqual(len(tappable(card, "rename|")), 2 * NAME_SHOWN)
        self.assertIn(f"{NAME_SHOWN + 2} 筆等你確認", flex_texts(card["contents"]))
        self.assertIn("還有 2 筆，處理完這些再傳一次「姓名申請」", "\n".join(flex_texts(card["contents"])))

    def test_anyone_else_is_told_how_to_ask_and_where_theirs_stands(self):
        self.assertIn("要改姓名，請輸入「設定 新的姓名」", self.ask("姓名申請"))
        self.ask("設定 王小明")
        self.assertIn("你申請的「王小明」在", self.ask("姓名申請"))
        self.assertIn("請先輸入「設定 你的姓名」", self.ask("姓名申請", "U3"))
        self.assertIsNone(self.store.user("U3"))                           # 「申請」 was not taken for a name
        self.assertEqual(self.name("U1"), "林小明")


class SettingsAndHelpTests(NameLockCase):
    def labels(self):
        return [item["action"]["label"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]

    def test_my_settings_offers_to_ask_and_shows_the_request(self):
        reply = self.ask("我的設定")
        self.assertIn("🔒 姓名登記後不能自己改", reply)
        self.assertEqual(self.labels()[0], "申請改姓名")
        self.ask("設定 王小明")
        reply = self.ask("我的設定")
        self.assertRegex(reply, r"申請中：改成「王小明」（\d+/\d+（.）\d\d:\d\d 送出，等待確認）")
        self.assertNotIn("🔒", reply)
        self.ask("設定 林小明", "U2")
        self.assertIn("申請中：登記「林小明」", self.ask("我的設定", "U2"))

    def test_an_approver_sees_how_many_wait(self):
        self.ask("設定 王小明")
        self.assertIn("姓名申請：1 筆等你確認（傳「姓名申請」）", self.ask("我的設定", ADMIN))
        self.assertIn("📮 姓名申請", self.labels())
        self.assertIn("姓名申請\n1 筆等你確認", self.ask("人員管理", ADMIN))   # an administrator who is a 幹部: the tile
        self.assertIn("📮 姓名申請", self.labels())

    def test_an_officer_is_not_asked_to_answer(self):
        self.ask("設定 王小明")
        self.assertNotIn("姓名申請", self.ask("我的設定", CAPTAIN))
        self.assertNotIn("📮 姓名申請", self.labels())
        self.assertNotIn("姓名申請", self.ask("人員管理", CAPTAIN))
        self.assertNotIn("📮 姓名申請", self.labels())

    def test_the_help_and_the_welcome_say_the_name_is_locked(self):
        self.assertTrue(any("申請改姓名" in line for line in flex_texts(help_card()["contents"])))
        self.assertIn("再傳「設定 新姓名」會送出申請，管理員同意後才會改。",
                      "\n".join(flex_texts(help_card(advanced=True)["contents"])))
        self.assertIn("姓名申請：管理員", MORE_HELP)
        self.assertIn("🔒 請登記自己的姓名：登記後要改，需要管理員同意。", flex_texts(welcome_card()["contents"]))
        self.assertIn("要管理員同意", self.ask("人員管理指令", CAPTAIN))
        for texts in (flex_texts(help_card()["contents"]), flex_texts(help_card(advanced=True)["contents"]),
                      flex_texts(welcome_card()["contents"])):
            self.assertFalse(any("管理員或幹部" in line or "管理員和幹部" in line for line in texts))


class ReviewFixTests(NameLockCase):
    def test_switching_names_back_and_forth_pushes_the_approvers_once(self):
        for target in ("陳小明", "王小明", "陳小明", "王小明"):
            reply = self.ask(f"設定 {target}")
        self.assertEqual(len(self.pushed(ADMIN)), 1)                         # the first request only
        self.assertEqual(len(self.pushed(DEPUTY)), 1)
        self.assertIn("這次沒有再私訊管理員（剛剛才通知過），管理員傳「姓名申請」就會看到這次的申請", reply)
        self.assertEqual(self.request()["requested_name"], "王小明")        # the newest one is the one pending
        self.ask("姓名申請", DEPUTY)
        self.assertIn("王小明", "\n".join(flex_texts(self.transport.raw[-1][0]["contents"])))
        self.store.db.execute("update name_requests set created_at=created_at-?, pushed_at=pushed_at-?",
                              (NAME_PUSH_GAP + 60, NAME_PUSH_GAP + 60))
        self.store.db.commit()
        self.ask("設定 陳小明")                                               # half a day later: pushed again
        self.assertEqual(len(self.pushed(ADMIN)), 2)

    def test_a_tap_on_a_replaced_card_points_to_the_list(self):
        self.ask("設定 陳小明")
        first = self.request()["id"]
        self.ask("設定 王小明")
        self.tap(DEPUTY, first, "yes")
        self.assertIn("申請人後來送出了新的申請，這筆作廢", self.reply())
        self.assertEqual(self.transport.raw[-1][0]["quickReply"]["items"][0]["action"]["text"], "姓名申請")
        self.assertEqual(self.name("U1"), "林小明")

    def test_a_name_taken_after_the_card_was_sent_is_shown_before_approving(self):
        self.ask("設定 王小明")
        number = self.request()["id"]
        self.register("U2", "王小明")                                         # the real 王小明 registers meanwhile
        self.tap(DEPUTY, number, "yes")
        self.assertEqual(self.name("U1"), "林小明")                          # nothing decided yet
        replies = self.transport.raw[-1]
        self.assertIn("已經有其他 LINE 帳號登記了", replies[0]["text"])
        self.assertTrue(any("已經有 1 個 LINE 帳號登記" in line for line in flex_texts(replies[1]["contents"])))
        fresh = next(box for box in tappable(replies[1], f"rename|{number}|yes"))
        self.bot.handle(postback_event(DEPUTY, fresh["action"]["data"]))      # seen the warning: now it counts
        self.assertEqual(self.name("U1"), "王小明")

    def test_another_approvers_old_card_is_checked_as_well(self):
        self.ask("設定 王小明")
        number = self.request()["id"]
        old = next(box for box in tappable(self.card_for(ADMIN), f"rename|{number}|yes"))
        self.register("U2", "王小明")
        self.tap(DEPUTY, number, "yes")                                      # DEPUTY is warned
        self.bot.handle(postback_event(ADMIN, old["action"]["data"]))       # ADMIN's card never showed it
        self.assertIn("已經有其他 LINE 帳號登記了", self.transport.raw[-1][0]["text"])
        self.assertEqual(self.name("U1"), "林小明")

    def test_approving_from_the_list_that_shows_the_holder_needs_no_second_look(self):
        self.ask("設定 王小明")
        number = self.request()["id"]
        self.register("U2", "王小明")
        self.ask("姓名申請", DEPUTY)
        listed = next(box for box in tappable(self.transport.raw[-1][0], f"rename|{number}|yes"))
        self.bot.handle(postback_event(DEPUTY, listed["action"]["data"]))
        self.assertEqual(self.name("U1"), "王小明")

    def test_a_correction_after_an_answer_is_pushed_again(self):
        self.ask("設定 王曉明")
        self.tap(DEPUTY, self.request()["id"], "no")
        reply = self.ask("設定 王小明")                                       # the corrected name
        self.assertEqual(len(self.pushed(ADMIN)), 2)
        self.assertNotIn("剛剛才通知過", reply)

    def test_an_unregistered_administrator_is_never_shown_as_a_raw_user_id(self):
        admin = "Uefb15c7dcd80589fa1892ed94f52a97b"
        self.bot.settings.admins = (admin, DEPUTY)
        self.transport.friends.add(admin)
        self.ask("設定 王小明")
        number = self.request()["id"]
        self.tap(admin, number, "no")
        self.tap(DEPUTY, number, "yes")
        self.assertIn("管理員 Ua1b2…c3d4", self.reply())
        self.assertNotIn(admin, self.reply())


class OnlyApproverTests(Case):
    settings = {"notify": False, "admins": ("U9",)}
    friends = {"U1", "U2", "U3", "U9"}

    def test_the_only_approver_is_not_told_their_own_name_is_locked(self):
        self.store.db.execute("delete from officers")
        self.store.db.commit()
        self.register("U9", "林大華")
        self.ask("用法說明", "U9")
        self.assertFalse(any("申請改姓名" in line for line in flex_texts(self.transport.raw[-1][0]["contents"])))
        self.register("U9", "林大明")
        self.assertEqual(self.name("U9"), "林大明")                           # nobody else to ask: it changes
        self.ask("用法說明", "U1")                                           # everyone else: locked
        self.assertTrue(any("申請改姓名" in line for line in flex_texts(self.transport.raw[-1][0]["contents"])))


class UnreachableApproversTests(NameLockCase):
    friends = {"U1", "U2", "U3"}                                            # no approver can be pushed

    def test_a_push_that_reached_nobody_does_not_hold_back_the_next(self):
        self.ask("設定 王曉明")
        reply = self.ask("設定 王小明")
        self.assertNotIn("剛剛才通知過", reply)
        self.assertIn("目前私訊不到管理員，請直接跟管理員說一聲你送出了申請", reply)


class UnlockedHelpTests(Case):
    settings = {"notify": False}

    def test_the_help_and_the_welcome_do_not_promise_a_lock_nobody_can_open(self):
        self.store.db.execute("delete from officers")
        self.store.db.commit()
        self.bot.handle(postback_event("U3", "noop"))
        self.ask("用法說明", "U3")
        texts = flex_texts(self.transport.raw[-1][0]["contents"])
        self.assertFalse(any("申請改姓名" in line for line in texts))
        self.assertTrue(any("回覆下方的按鈕可設定或更改姓名" in line for line in texts))
        self.assertIn("要改姓名，再傳一次「設定 新姓名」即可。", "\n".join(flex_texts(help_card(advanced=True, locked=False)["contents"])))
        self.assertFalse(any(line.startswith("🔒") for line in flex_texts(welcome_card(locked=False)["contents"])))
        self.register("U3", "林小明"); self.register("U3", "王小明")
        self.assertEqual(self.name("U3"), "王小明")                          # no approver: it changes at once


class DeliveryTests(NameLockCase):
    friends = {"U1", "U2", "U3", CAPTAIN, DEPUTY}   # the administrator ADMIN cannot be pushed

    def test_push_failures_do_not_break_the_flow(self):
        self.store.add_officer(SENIOR, "學姊", "seed")                     # a 幹部: not asked at all
        log = io.StringIO()
        with redirect_stdout(log):
            self.ask("設定 王小明")
        self.assertEqual([who for who, _ in self.transport.raw_pushes], [DEPUTY])
        self.assertIn("已私訊 1/2 位管理員", log.getvalue())
        self.transport.friends.discard("U1")                                # the requester blocks it too
        with redirect_stdout(io.StringIO()):
            self.tap(DEPUTY, self.request()["id"], "yes")
        self.assertEqual(self.name("U1"), "王小明")      # decided all the same
        self.assertIn("已同意", self.reply())
        self.assertIn("私訊沒有送到 林小明（LINE：明明）", self.pushed(DEPUTY)[-1][0]["text"])

    def test_with_nobody_reached_the_requester_is_told_to_say_so(self):
        self.transport.friends.discard(DEPUTY)
        with redirect_stdout(io.StringIO()):
            reply = self.ask("設定 王小明")
        self.assertIn("目前私訊不到管理員", reply)
        self.assertEqual(self.transport.raw_pushes, [])                      # the 幹部 CAPTAIN is not asked instead
        self.assertEqual(self.request()["requested_name"], "王小明")         # still filed, and listed
        self.ask("姓名申請", ADMIN)
        self.assertEqual(len(tappable(self.transport.raw[-1][0], "rename|")), 2)


class CardTests(unittest.TestCase):
    def requests(self, count, holders=True):
        return [({"id": 9_999_990 + index, "user_id": f"U{index}", "current_name": "歐陽小明明" if index % 2 else None,
                  "requested_name": "歐陽娜娜娜", "display_name": "超級長的LINE名稱🌸🌸🌸🌸🌸🌸",
                  "status": "pending", "created_at": 1_791_000_000 + index * 60},
                 [{"name": "另一個很長的名字🌸🌸"}, {"name": "第二位"}, {"name": "第三位"}] if holders else [])
                for index in range(count)]

    def test_a_full_list_fits_one_bubble_within_lines_limits(self):
        for card in (name_request_card(self.requests(NAME_SHOWN), listing=True, more=99),
                     name_request_card(self.requests(1)), name_request_card(self.requests(1, holders=False))):
            self.assertEqual(card["contents"]["type"], "bubble")
            self.assertLess(flex_size(card["contents"]), FLEX_BUBBLE_LIMIT)
            self.assertLessEqual(len(card["altText"]), 400)
            nodes = flex_nodes(card)
            self.assertTrue(all(node["text"] for node in nodes if node.get("type") in ("text", "span")))
            for node in nodes:
                for key in ("color", "backgroundColor", "borderColor", "startColor", "endColor"):
                    if key in node:
                        self.assertRegex(node[key], r"^#[0-9A-F]{6}([0-9A-F]{2})?$", key)
                action = node.get("action")
                if action:
                    self.assertLessEqual(len(action["label"]), 20)
                    self.assertLessEqual(len(action["data"]), 300)


class StoreTests(unittest.TestCase):
    def test_an_old_database_gains_the_table_and_the_column(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot.sqlite3"
            old = sqlite3.connect(path)
            old.execute("create table users(user_id text primary key, code text, name text, notify integer default 0, "
                        "updated_at real, staff_name text, hourly_wage real)")
            old.execute("insert into users(user_id, staff_name) values('U1', '林小明')")
            old.commit(); old.close()
            store = Store(path)
            self.assertEqual(store.locked_name("U1"), "林小明")
            store.set_staff_name("U1", None)
            self.assertEqual(store.locked_name("U1"), "林小明")             # 「解除」 keeps the lock
            self.assertIsNone(store.user("U1")["staff_name"])
            store.set_staff_name("U1", "王小明")
            self.assertEqual(store.locked_name("U1"), "王小明")
            number, replaced = store.add_name_request("U1", "王小明", "陳小明", None)
            self.assertIsNone(replaced)
            self.assertTrue(store.decide_name_request(number, "approved", "U9"))
            self.assertFalse(store.decide_name_request(number, "refused", "U8"))   # only the first answer counts
            self.assertEqual(store.name_request(number)["decided_by"], "U9")


if __name__ == "__main__":
    unittest.main()
