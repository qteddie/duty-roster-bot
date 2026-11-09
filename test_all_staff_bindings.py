"""Who is behind each name.

* A staff name belongs to one LINE account. Asking for a name another account has still goes to the
  administrators (姓名申請), but a yes now *moves* the name: the account that had it is released.
* 「綁定清單」 shows the 幹部 and the administrators which LINE account (by its LINE name) registered which
  staff name; 「解除登記 姓名」 releases one.
* A 役男 who is replaced on his Chinese code has left: he comes off the 名冊 instead of staying on it without
  a code, among the people whose numeric code changes daily.
"""
import copy
import io
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from all_staff.bot import FLEX_BUBBLE_LIMIT, FLEX_CAROUSEL_LIMIT, NameTaken, Store, binding_card, flex_size
from all_staff.name_table import load_directory, load_fixed_codes
from all_staff.staff_book import StaffBook
from test_all_staff_bot import RESULT, BotCase, flex_texts, postback_event, tappable
from test_all_staff_name_lock import ADMIN, DEPUTY, NameLockCase
from test_all_staff_name_lock import CAPTAIN as CHIEF          # a 幹部 (隊長) registered as 林大明 in NameLockCase
from test_all_staff_officers import CAPTAIN, STAFF, TODAY, OfficerCase
from test_all_staff_roster_gate import check_line_limits


class LineLimits:
    """Every reply and push of a test is something LINE accepts (as the 名冊守門員 tests check theirs)."""

    def tearDown(self):
        for messages in self.transport.raw + [messages for _, messages in self.transport.raw_pushes]:
            for message in messages:
                check_line_limits(self, message)
        super().tearDown()


def share_a_name(store, user, name, display=None, at=5.0):
    """A second account under ``name``, as databases from before the one-account rule can hold."""
    with store.lock, store.db:
        store.db.execute("drop index if exists users_staff_name")
        store.db.execute("insert into users(user_id, staff_name, name, updated_at, notify) values(?,?,?,?,0)",
                         (user, name, display, at))


# ---------------------------------------------------------------- 役男 replaced on his code
class ReplacedConscriptBookTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.names, self.fixed = root / "staff_names.txt", root / "fixed_staff_codes.json"
        self.names.write_text(STAFF, encoding="utf-8")
        self.fixed.write_text(json.dumps({"己": "蕭志明"}), encoding="utf-8")
        self.book = StaffBook(self.names, self.fixed)

    def tearDown(self):
        self.folder.cleanup()

    def test_whoever_is_replaced_on_a_chinese_code_comes_off_the_list(self):
        change = self.book.set_code("己", "陳小華")
        self.assertEqual((change["before"], change["removed"]), ("蕭志明", "蕭志明"))
        self.assertEqual(load_directory(self.names), ["林小明", "陳小華"])
        self.assertEqual(load_fixed_codes(self.fixed), {"己": "陳小華"})
        self.assertIn("# 中文固定代號", self.names.read_text(encoding="utf-8"))   # the comments stay

    def test_whoever_is_replaced_on_an_english_code_stays_and_is_written_onto_the_list(self):
        self.fixed.write_text(json.dumps({"己": "蕭志明", "A": "王大同"}, ensure_ascii=False), encoding="utf-8")
        self.assertNotIn("王大同", load_directory(self.names))        # on the 名冊 through the code only
        change = self.book.set_code("A", "陳小華")
        self.assertEqual((change["before"], change["removed"]), ("王大同", None))
        self.assertIn("王大同", load_directory(self.names))

    def test_the_day_a_conscript_was_replaced_is_kept_for_older_rosters(self):
        self.book.set_code("己", "陳小華", day="2026-10-08")
        self.assertEqual(self.book.former(date(2026, 10, 7)), ["蕭志明"])     # a roster from before: still him
        self.assertEqual(self.book.former(date(2026, 10, 8)), [])
        self.assertEqual(self.book.former(None), [])                         # an undated roster: today's 名冊
        self.book.set_code("A", "林小明", day="2026-10-08")
        self.book.set_code("A", "陳小華", day="2026-10-09")                   # English: nobody left
        self.assertEqual(self.book.former(date(2026, 10, 1)), ["蕭志明"])
        self.book.former_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.book.former(date(2026, 10, 1)), [])

    def test_a_conscript_who_leaves_by_clearing_or_removing_keeps_his_older_rosters_too(self):
        self.assertEqual(self.book.clear_code("己", day="2026-10-08"), "蕭志明")
        self.assertEqual(self.book.former(date(2026, 10, 7)), ["蕭志明"])
        self.book.set_code("丁", "陳小華", day="2026-10-08")
        self.book.remove(["陳小華", "林小明"], day="2026-10-09")           # 陳小華 had 丁; 林小明 never had a code
        self.assertEqual(self.book.former(date(2026, 10, 8)), ["陳小華"])
        self.book.set_code("A", "林小明", day="2026-10-09")
        self.book.clear_code("A", day="2026-10-10")                      # an English code: he has not left
        self.assertEqual(self.book.former(date(2026, 10, 1)), ["蕭志明", "陳小華"])

    def test_who_has_left_by_a_given_day(self):
        self.book.set_code("己", "陳小華", day="2026-10-08")
        self.assertEqual(self.book.departed(date(2026, 10, 7)), set())
        self.assertEqual(self.book.departed(date(2026, 10, 8)), {"蕭志明"})
        self.assertEqual(self.book.departed(None), {"蕭志明"})                # an undated roster: today's 名冊
        self.book.add(["蕭志明"])                                           # back on the 名冊: not gone after all
        self.assertEqual(self.book.departed(date(2026, 10, 9)), set())

    def test_nobody_leaves_when_nobody_is_replaced(self):
        for code, name in (("丁", "林小明"), ("丁", "林小明"), ("甲", "林小明")):   # a blank code, the same person, a move
            with self.subTest(code=code):
                self.assertIsNone(self.book.set_code(code, name)["removed"])
        self.assertEqual(load_directory(self.names), ["林小明", "陳小華", "蕭志明"])


class ReplacedConscriptTests(LineLimits, OfficerCase):
    def test_the_replaced_conscript_leaves_the_roster_and_the_answer_says_so(self):
        reply = self.say("役男 己 陳小華")
        self.assertIn("役男代號「己」：陳小華", reply)
        self.assertIn("原本是 蕭志明：役男換人，已經把他移出名冊", reply)
        self.assertNotIn("他現在沒有代號", reply)
        self.assertNotIn("蕭志明", self.directory())
        self.assertEqual(load_fixed_codes(self.fixed), {"己": "陳小華"})

    def test_he_is_no_longer_among_the_people_without_a_code(self):
        self.say("役男 己 陳小華")
        card = self.say("人員名冊")
        self.assertNotIn("蕭志明", card)
        self.assertIn("沒有固定代號\n1 位", card)        # 林小明; 陳小華 has 己 now

    def test_one_tap_puts_him_back_when_it_was_a_mistake(self):
        self.say("役男 己 陳小華")
        items = self.transport.raw[-1][-1]["quickReply"]["items"]
        back = next(item["action"] for item in items if item["action"]["label"] == "➕ 加回蕭志明")
        self.assertEqual(back["text"], "新增人員 蕭志明")
        self.assertIn("已加入名冊：蕭志明", self.say(back["text"]))

    def test_his_line_account_is_told_nothing_but_sees_no_roster_any_more(self):
        self.register("U2", "蕭志明")
        reply = self.say("役男 己 陳小華")
        self.assertIn("蕭志明 登記的 LINE 帳號從現在起看不到班表", reply)
        self.assertEqual(self.transport.pushes, [])
        self.assertIn("目前不在名冊上", self.ask("我的班表", "U2"))

    def test_an_english_code_changing_hands_removes_nobody(self):
        self.fixed.write_text(json.dumps({"己": "蕭志明", "A": "王大同"}, ensure_ascii=False), encoding="utf-8")
        reply = self.say("代號 A 陳小華")
        self.assertIn("原本是 王大同，他現在沒有代號", reply)
        self.assertIn("王大同", self.directory())                 # written there: the 名冊 decides who may use the bot
        self.register("U3", "王大同")
        self.assertNotIn("不在名冊", self.ask("我的班表", "U3"))

    def test_the_prompts_before_replacing_a_conscript_say_he_will_leave_the_roster(self):
        self.assertIn("原本那位會移出名冊", self.say("役男 己"))
        self.assertIn("蕭志明 會移出名冊", self.say("役男 己 王大同"))            # a name the bot does not know yet
        self.assertIn("名冊裡還沒有「王大同」", self.reply())
        with patch("all_staff.bot.today", return_value=TODAY):
            self.bot.handle(postback_event(CAPTAIN, "setcode|己|陳小華|empty"))  # a cell that was blank when shown
        self.assertIn("換了以後 蕭志明 會移出名冊", self.reply())
        self.assertEqual(load_fixed_codes(self.fixed), {"己": "蕭志明"})          # asked, nothing done
        self.say("代號 A 林小明")
        self.assertNotIn("移出名冊", self.say("代號 A"))
        self.assertNotIn("移出名冊", self.say("代號 A 王大同"))
        with patch("all_staff.bot.today", return_value=TODAY):
            self.bot.handle(postback_event(CAPTAIN, "setcode|A|陳小華|empty"))    # an English cell taken meanwhile
        self.assertIn("要換成 陳小華 嗎", self.reply())
        self.assertNotIn("移出名冊", self.reply())

    def test_a_replaced_conscript_who_is_a_cadre_is_not_said_to_be_locked_out(self):
        self.store.release(CAPTAIN)
        self.store.set_staff_name(CAPTAIN, "蕭志明", None)
        reply = self.say("役男 己 陳小華")
        self.assertIn("已經把他移出名冊", reply)
        self.assertNotIn("看不到班表", reply)
        self.assertIn("📋 人員名冊", self.say("人員名冊"))        # he manages as before: the card, not the gate

    def test_clearing_a_conscripts_code_keeps_him_but_offers_one_tap_to_take_him_off(self):
        reply = self.say("役男 己 取消")
        self.assertIn("原本是 蕭志明，他仍在名冊上", reply)
        self.assertIn("蕭志明", self.directory())
        items = self.transport.raw[-1][-1]["quickReply"]["items"]
        off = next(item["action"] for item in items if item["action"]["label"] == "➖ 移出蕭志明")
        self.assertIn("已從名冊移除：蕭志明", self.say(off["text"]))
        self.say("代號 A 林小明")
        self.say("代號 A 取消")                                 # an English code: no such offer
        labels = [item["action"]["label"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertFalse(any(label.startswith("➖ 移出") for label in labels))

    def test_the_commands_say_what_happens_to_the_one_replaced(self):
        self.assertIn("原本那位役男會移出名冊", self.say("人員管理指令"))


class FormerConscriptRosterTests(LineLimits, OfficerCase):
    """蕭志明 had 己 until 陳小華 took it on 10/8: he is off the 名冊, but rosters from before still name him."""

    def names_on(self, day, readings):
        result = copy.deepcopy(RESULT)
        result["names"] = readings
        return self.bot.names_for({"run_id": "run-1", "roster_date": day}, result)

    def test_older_rosters_still_show_him_on_his_code(self):
        self.say("役男 己 陳小華")
        self.assertNotIn("蕭志明", self.directory())
        self.assertEqual(self.names_on("2026-10-07", {"己": ["蕭志明"]}).get("己"), "蕭志明")
        self.assertEqual(self.names_on("2026-10-08", {"己": ["看不清"]}).get("己"), "陳小華")

    def test_a_successor_with_a_similar_name_does_not_take_over_older_rosters(self):
        self.say("新增人員 林子軍")
        self.say("役男 己 林子軍")
        self.assertEqual(self.names_on("2026-10-07", {"己": ["蕭志明"]}).get("己"), "蕭志明")
        self.assertEqual(self.names_on("2026-10-08", {}).get("己"), "林子軍")

    def test_from_the_day_he_left_his_name_is_matched_to_nothing(self):
        self.say("役男 己 陳小華")
        names = self.names_on("2026-10-09", {"72": ["蕭志明"], "26": ["王大同"]})
        self.assertNotIn("蕭志明", names.values())

    def test_not_even_while_his_line_account_still_carries_the_name(self):
        self.register("U2", "蕭志明")                       # registered names are matched too, as long as they are staff
        self.assertEqual(self.names_on("2026-10-07", {"72": ["蕭志明"]}).get("己"), "蕭志明")
        self.say("役男 己 陳小華")
        for day in ("2026-10-08", "2026-10-09"):
            self.assertNotIn("蕭志明", self.names_on(day, {"72": ["蕭志明"]}).values(), day)
        self.assertNotIn("蕭志明", self.bot.names_for({"run_id": "run-1"}, {**copy.deepcopy(RESULT), "names": {"72": ["蕭志明"]}}).values())
        self.assertEqual(self.names_on("2026-10-07", {"己": ["蕭志明"]}).get("己"), "蕭志明")   # before: still him
        self.say("新增人員 蕭志明")                          # he was not gone after all: matched like anyone again
        self.assertEqual(self.names_on("2026-10-09", {"72": ["蕭志明"]}).get("72"), "蕭志明")

    def test_leaving_by_the_buttons_the_bot_offers_protects_older_rosters_as_well(self):
        self.say("役男 己 取消")
        items = self.transport.raw[-1][-1]["quickReply"]["items"]
        self.say(next(item["action"]["text"] for item in items if item["action"]["label"] == "➖ 移出蕭志明"))
        self.say("新增人員 林子軍")
        self.say("役男 己 林子軍")
        self.assertEqual(self.names_on("2026-10-07", {"己": ["蕭志明"]}).get("己"), "蕭志明")

    def test_the_leave_card_of_an_older_roster_names_him_not_his_successor(self):
        from all_staff.leave_table import LEAVE_VERSION
        self.say("新增人員 林子軍")
        self.say("役男 己 林子軍")
        self.analyzer.data.update(leave_table={"available": True, "rest": [], "leave": [
            {"code": None, "name": ["蕭志明"], "kind": "事假"}]}, leave_table_version=LEAVE_VERSION)
        self.analyzer.data["run"]["roster_date"] = "2026-10-07"
        self.store.add_roster("Cgroup", "run-1", "U1", 5, "2026-10-07")
        card = self.say("休假人員 10/7")
        self.assertIn("蕭志明", card)
        self.assertNotIn("林子軍", card)


# ---------------------------------------------------------------- one LINE account per name
class OneAccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "bot.sqlite3"
        self.store = Store(self.path)
        self.store.set_staff_name("U1", "林小明", "明明")

    def tearDown(self):
        self.folder.cleanup()

    def name(self, user):
        return (self.store.user(user) or {}).get("staff_name")

    def test_a_second_account_cannot_be_given_a_name_that_is_taken(self):
        with self.assertRaises(NameTaken):
            self.store.set_staff_name("U2", "林小明", "華華")
        self.assertIsNone(self.name("U2"))
        self.store.set_staff_name("U1", "林小明")              # the same account again: fine
        self.assertEqual(self.store.registered(), {"林小明": 1})

    def test_taking_a_name_releases_whoever_had_it(self):
        self.store.set_notify("U1", True)
        self.store.claim("run-1", "U1", "72")
        released = self.store.take_name("U2", "林小明", "華華")
        self.assertEqual([(r["user_id"], r["name"]) for r in released], [("U1", "明明")])
        self.assertEqual((self.name("U1"), self.name("U2")), (None, "林小明"))
        self.assertEqual(self.store.locked_name("U1"), "林小明")   # still theirs to ask back: a change of name needs a yes
        self.assertEqual(self.store.user("U1")["notify"], 0)
        self.assertEqual(self.store.claims("run-1"), {})          # 「我是 72」 was that account's
        self.assertEqual(self.store.take_name("U2", "林小明", "華華"), [])   # nobody left to release

    def test_the_database_itself_keeps_names_apart(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.lock, self.store.db:
                self.store.db.execute("insert into users(user_id, staff_name) values('U2', '林小明')")

    def test_a_database_that_already_holds_shared_names_still_opens(self):
        share_a_name(self.store, "U2", "林小明", "華華")
        log = io.StringIO()
        with redirect_stdout(log):
            again = Store(self.path)
        self.assertIn("綁定清單", log.getvalue())                # told where to look
        self.assertEqual(again.registered(), {"林小明": 2})
        with self.assertRaises(NameTaken):
            again.set_staff_name("U3", "林小明")
        again.release("U2")
        with redirect_stdout(io.StringIO()) as quiet:
            Store(self.path)                                      # sorted out: the rule is in the database now
        self.assertEqual(quiet.getvalue(), "")

    def test_releasing_an_account_turns_its_notifications_off(self):
        self.store.set_notify("U1", True)
        self.assertTrue(self.store.release("U1"))
        self.assertEqual(self.store.user("U1")["notify"], 0)
        self.assertFalse(self.store.release("U1"))               # nothing left to release

    def test_a_yes_checks_decides_and_moves_the_name_in_one_step(self):
        self.store.set_staff_name("U2", "陳小華", "華華")
        number, _ = self.store.add_name_request("U2", "陳小華", "林小明", "華華")
        shown = time.time() + 1                                  # the card showed U1 as the holder
        released = self.store.approve_name_request(number, "U9", shown)
        self.assertEqual([account["user_id"] for account in released], ["U1"])
        self.assertEqual((self.name("U1"), self.name("U2")), (None, "林小明"))
        self.assertEqual(self.store.name_request(number)["status"], "approved")
        self.assertIsNone(self.store.approve_name_request(number, "U9", shown))     # answered already

    def test_a_yes_does_not_release_someone_the_card_could_not_show(self):
        from all_staff.bot import LOOK_AGAIN
        number, _ = self.store.add_name_request("U2", None, "王大同", "華華")
        shown = time.time() - 1
        self.store.set_staff_name("U3", "王大同", "小三")          # registers after the card was built
        self.assertIs(self.store.approve_name_request(number, "U9", shown), LOOK_AGAIN)
        self.assertEqual((self.name("U3"), self.name("U2")), ("王大同", None))
        self.assertEqual(self.store.name_request(number)["status"], "pending")

    def test_a_request_of_an_account_that_is_released_no_longer_says_it_has_that_name(self):
        refused, _ = self.store.add_name_request("U1", "林小明", "陳大華", "明明")
        self.store.decide_name_request(refused, "refused", "U9")
        number, _ = self.store.add_name_request("U1", "林小明", "王大同", "明明")
        self.store.set_staff_name("U2", "陳小華", "華華")
        other, _ = self.store.add_name_request("U2", "陳小華", "王小明", "華華")
        before = (self.store.name_request(refused), self.store.name_request(other))
        self.store.release("U1")
        request = self.store.name_request(number)
        self.assertEqual((request["current_name"], request["released_name"], request["status"]),
                         (None, "林小明", "pending"))
        self.assertEqual((self.store.name_request(refused), self.store.name_request(other)), before)   # only this one

    def test_a_yes_keeps_the_line_name_read_from_line_since_the_request(self):
        self.store.set_staff_name("U2", "陳小華", "舊的名字")
        number, _ = self.store.add_name_request("U2", "陳小華", "王大同", "舊的名字")
        self.store.set_display("U2", "新的名字")
        self.store.approve_name_request(number, "U9", time.time() + 1)
        self.assertEqual((self.store.user("U2")["staff_name"], self.store.user("U2")["name"]), ("王大同", "新的名字"))

    def test_two_yeses_for_one_name_at_the_same_moment_leave_one_owner(self):
        import threading
        from all_staff.bot import LOOK_AGAIN
        name = "王大同"
        for round_ in range(25):
            for user in ("U1", "U2", "U3"):
                self.store.release(user)
            self.store.set_staff_name("U1", name, "明明")
            first, _ = self.store.add_name_request("U2", None, name, "華華")
            second, _ = self.store.add_name_request("U3", None, name, "小三")
            shown, barrier, results = time.time(), threading.Barrier(2), {}   # both cards showed U1, and only U1
            time.sleep(0.002)
            def approve(number):
                barrier.wait()
                results[number] = self.store.approve_name_request(number, "U9", shown)
            threads = [threading.Thread(target=approve, args=(number,)) for number in (first, second)]
            [t.start() for t in threads]; [t.join() for t in threads]
            self.assertEqual(self.store.registered().get(name), 1, round_)
            moved = [number for number, result in results.items() if isinstance(result, list)]
            self.assertEqual(len(moved), 1, results)                       # the other one saw who had registered
            self.assertIs(results[first if moved == [second] else second], LOOK_AGAIN)
            for number in (first, second):
                self.store.decide_name_request(number, "cancelled", "U9")

    def test_when_the_rule_started_is_recorded_at_every_start(self):
        since = self.store.one_name_since()
        self.assertIsNotNone(since)
        with self.store.lock, self.store.db:
            self.store.db.execute("delete from meta where key='one_name_since'")
        again = Store(self.path)                                           # an earlier build added name_at without it
        self.assertGreaterEqual(again.one_name_since(), since)
        with again.lock, again.db:
            again.db.execute("update meta set value='abc' where key='one_name_since'")
        self.assertIsNotNone(Store(self.path).one_name_since())            # unreadable: from this start on

    def test_bindings_list_every_account_with_its_line_name(self):
        self.store.set_staff_name("U2", "陳小華", None)
        self.assertEqual([(b["user_id"], b["staff_name"], b["name"]) for b in self.store.bindings()],
                         [("U1", "林小明", "明明"), ("U2", "陳小華", None)])


class TransferTests(LineLimits, NameLockCase):
    """U1 is 林小明. U2 asks for the same name; an administrator's yes moves it."""

    def test_a_yes_moves_the_name_and_releases_the_account_that_had_it(self):
        self.ask("設定 林小明", "U2")
        texts = flex_texts(self.card_for(DEPUTY)["contents"])
        self.assertIn("⚠️「林小明」已經有 1 個 LINE 帳號登記（LINE：明明）。一個姓名只能登記在一個 LINE 帳號："
                      "同意後姓名轉給申請人，原本的帳號會被解除。", texts)
        self.tap(DEPUTY, self.request("U2")["id"], "yes")
        self.assertEqual((self.name("U1"), self.name("U2")), (None, "林小明"))
        reply = self.reply()
        self.assertIn("✅ 已同意：LINE：華華 登記為「林小明」", reply)
        self.assertIn("原本登記「林小明」的 LINE 帳號（明明）已經解除", reply)
        self.assertEqual(self.store.registered().get("林小明"), 1)

    def test_the_released_account_has_to_ask_to_get_it_back(self):
        self.ask("設定 林小明", "U2")
        self.tap(DEPUTY, self.request("U2")["id"], "yes")
        self.assertIn("「林小明」已經有其他 LINE 帳號登記了", self.ask("設定 林小明", "U1"))
        self.assertIsNone(self.name("U1"))
        self.assertEqual(self.request("U1")["requested_name"], "林小明")

    def test_a_no_changes_nothing(self):
        self.ask("設定 林小明", "U2")
        self.tap(ADMIN, self.request("U2")["id"], "no")
        self.assertEqual((self.name("U1"), self.name("U2")), ("林小明", None))

    def test_a_change_of_name_onto_a_taken_one_moves_it_too(self):
        self.register("U2", "陳小華")
        self.ask("設定 林小明", "U2")
        self.tap(DEPUTY, self.request("U2")["id"], "yes")
        self.assertEqual((self.name("U1"), self.name("U2")), (None, "林小明"))
        self.assertNotIn("陳小華", self.store.registered())

    def test_accounts_that_shared_a_name_before_the_rule_are_all_released(self):
        share_a_name(self.store, "U3", "林小明", "小三")
        self.ask("設定 林小明", "U2")
        self.tap(DEPUTY, self.request("U2")["id"], "yes")
        self.assertEqual((self.name("U1"), self.name("U3"), self.name("U2")), (None, None, "林小明"))
        released = self.reply().split("LINE 帳號（")[1].split("）已經解除")[0]
        self.assertEqual(sorted(released.split("、")), ["小三", "明明"])


class OldCardTests(LineLimits, NameLockCase):
    """A card pushed before a yes moved the name says 「原本的帳號不受影響」: its ✅ must not release anyone."""

    def test_a_card_from_before_the_rule_asks_again_with_the_warning(self):
        with self.store.lock, self.store.db:                     # U1 had the name long before that card was made
            self.store.db.execute("update users set updated_at=updated_at-7200 where user_id='U1'")
        self.ask("設定 林小明", "U2")
        number = self.request("U2")["id"]
        old_card = int((time.time() - 3600) * 1000)              # built an hour before this version first ran
        self.bot.handle(postback_event(DEPUTY, f"rename|{number}|yes|{old_card}"))
        self.assertEqual((self.name("U1"), self.name("U2")), ("林小明", None))   # nothing decided
        replies = self.transport.raw[-1]
        self.assertIn("請看過下面這張再決定一次", replies[0]["text"])
        self.assertTrue(any("原本的帳號會被解除" in line for line in flex_texts(replies[1]["contents"])))
        fresh = next(box for box in tappable(replies[1], f"rename|{number}|yes"))
        self.bot.handle(postback_event(DEPUTY, fresh["action"]["data"]))
        self.assertEqual((self.name("U1"), self.name("U2")), (None, "林小明"))

    def test_an_unreadable_start_of_the_rule_still_gives_old_cards_their_second_look(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("update meta set value='abc' where key='one_name_since'")
            self.store.db.execute("update users set updated_at=updated_at-7200 where user_id='U1'")
        self.ask("設定 林小明", "U2")
        number = self.request("U2")["id"]
        self.bot.handle(postback_event(DEPUTY, f"rename|{number}|yes|{int((time.time() - 3600) * 1000)}"))
        self.assertEqual(self.name("U1"), "林小明")
        fresh = next(box for box in tappable(self.transport.raw[-1][1], f"rename|{number}|yes"))
        self.bot.handle(postback_event(DEPUTY, fresh["action"]["data"]))     # the new card is never old: no loop
        self.assertEqual((self.name("U1"), self.name("U2")), (None, "林小明"))

    def test_an_old_card_for_a_name_nobody_has_is_answered_as_always(self):
        self.ask("設定 王小明")                                    # U1 wants another name: nobody is released
        number = self.request()["id"]
        self.bot.handle(postback_event(DEPUTY, f"rename|{number}|yes|{int((time.time() - 3600) * 1000)}"))
        self.assertEqual(self.name("U1"), "王小明")

    def test_a_request_card_of_a_released_account_shows_it_without_a_name(self):
        self.ask("設定 王大同")                                    # U1 (林小明) asks to become 王大同
        self.ask("解除登記 林小明", ADMIN)                         # then a 幹部 releases U1
        self.ask("姓名申請", DEPUTY)
        texts = flex_texts(self.transport.raw[-1][0]["contents"])
        self.assertIn("還沒登記", texts)
        self.assertFalse(any("維持「林小明」" in line for line in texts))
        self.assertTrue(any("之前登記過「林小明」" in line for line in texts))

    def test_moving_a_cadres_name_says_the_role_stays_with_the_old_account(self):
        self.ask("設定 林大明", "U3")                              # 林大明 is CHIEF, a 幹部 (隊長)
        self.tap(DEPUTY, self.request("U3")["id"], "yes")
        reply = self.reply()
        self.assertIn("已經解除", reply)
        self.assertIn("仍是幹部", reply)
        self.assertNotIn("看不到班表了", reply)
        self.assertIn("新增幹部 林大明", reply)
        self.assertIn(f"移除幹部 {CHIEF}", reply)                   # the whole user id: the only handle left on it
        self.assertTrue(self.store.is_officer(CHIEF))
        card = flex_texts(self.card_for(DEPUTY)["contents"])      # and the card said so before the yes
        self.assertTrue(any("是幹部" in line and "照樣看得到班表" in line for line in card))

    def test_without_a_roster_nobody_is_said_to_be_kept_out(self):
        self.ask("設定 林小明", "U2")                              # no 名冊 here: the 名冊守門員 is off
        self.assertFalse(any("看不到班表" in line for line in flex_texts(self.card_for(DEPUTY)["contents"])))
        self.tap(DEPUTY, self.request("U2")["id"], "yes")
        self.assertNotIn("看不到班表", self.reply())
        self.assertNotIn("看不到班表", self.ask("解除登記 林小明", ADMIN))


class NobodyToAskTests(LineLimits, BotCase):
    """Without administrators nobody can say yes: a name that is taken stays with the account that has it."""
    settings = {"notify": False}

    def name(self, user):
        return (self.store.user(user) or {}).get("staff_name")

    def test_a_taken_name_is_refused(self):
        self.register("U1", "林小明")
        reply = self.ask("設定 林小明", "U2")
        self.assertIn("「林小明」已經有其他 LINE 帳號登記了", reply)
        self.assertIn("一個姓名只能登記在一個 LINE 帳號", reply)
        self.assertIn("請聯絡幹部", reply)
        self.assertNotIn("已登記：", reply)
        self.assertEqual((self.name("U1"), self.name("U2")), ("林小明", None))
        self.assertEqual(self.store.pending_name_requests(), [])

    def test_the_refusal_says_which_name_the_account_keeps(self):
        self.register("U1", "林小明")
        self.register("U2", "陳小華")
        self.assertIn("你的姓名還是「陳小華」", self.ask("設定 林小明", "U2"))
        self.assertEqual(self.name("U2"), "陳小華")


class OnlyAdministratorTests(LineLimits, BotCase):
    """The one administrator has nobody to ask and is the one who would say yes: the name moves at once."""
    friends = {"U1", "U2", "U9"}
    settings = {"notify": False, "admins": ("U9",)}

    def test_the_only_administrator_takes_a_name_over_at_once(self):
        self.register("U1", "林小明")
        log = io.StringIO()
        with redirect_stdout(log):
            reply = self.ask("設定 林小明", "U9")
        self.assertIn("已登記：林小明", reply)
        self.assertIn("原本登記「林小明」的 LINE 帳號（明明）已經解除", reply)
        self.assertEqual(self.store.registered(), {"林小明": 1})
        self.assertIsNone(self.store.user("U1")["staff_name"])
        self.assertIn("U9", log.getvalue())


# ---------------------------------------------------------------- 綁定清單 and 解除登記
class BindingCase(LineLimits, OfficerCase):
    """CAPTAIN (林大明, 隊長) is a 幹部, U9 an administrator; U1 is 林小明, U2 陳小華. 名冊: 林小明 陳小華 蕭志明."""
    friends = {"U1", "U2", "U3", "U9", CAPTAIN}
    settings = {"notify": False, "admins": ("U9",)}

    def setUp(self):
        super().setUp()
        self.register("U2", "陳小華")
        self.transport.replies.clear(); self.transport.raw.clear()

    def name(self, user):
        return (self.store.user(user) or {}).get("staff_name")

    def card(self):
        return next(message for message in self.transport.raw[-1] if message["type"] == "flex")


class BindingListTests(BindingCase):
    def test_a_cadre_sees_which_line_account_has_which_name(self):
        reply = self.say("綁定清單")
        texts = flex_texts(self.card()["contents"])
        self.assertIn("🔗 綁定清單", texts)
        self.assertIn("3 個 LINE 帳號已登記｜名冊 3 位", texts)
        for name, display in (("林小明", "明明"), ("陳小華", "華華")):
            self.assertEqual(texts[texts.index(name) + 1], display, name)
        self.assertIn("林小明＝明明", self.card()["altText"])
        self.assertIn("綁定清單", reply)

    def test_an_administrator_sees_it_too_and_nobody_else(self):
        self.assertIn("林小明", self.say("綁定清單", "U9"))
        reply = self.say("綁定清單", "U1")
        self.assertIn("只有幹部", reply)
        self.assertNotIn("華華", reply)

    def test_someone_the_gate_keeps_out_is_shown_the_gate_not_the_list(self):
        gate = self.say("我的班表", "U3")                      # not registered: what the 名冊守門員 says
        self.assertIn("請先登記姓名", gate)
        self.assertEqual(self.say("綁定清單", "U3"), gate)     # 「綁定 清單」 must not read as registering 清單 either
        self.assertEqual(self.say("登記名單", "U3"), gate)
        self.assertIsNone(self.name("U3"))

    def test_every_way_of_asking(self):
        for words in ("綁定清單", "綁定名單", "綁定列表", "登記清單", "登記名單", "登記列表", "綁定 清單"):
            self.assertIn("🔗 綁定清單", self.say(words), words)

    def test_the_list_follows_the_roster_and_says_who_manages(self):
        self.store.set_staff_name("U9", "蕭志明", "隊部公務機")
        texts = flex_texts(self.card_after("綁定清單")["contents"])
        order = [texts.index(name) for name in ("林小明", "陳小華", "蕭志明")]
        self.assertEqual(order, sorted(order))
        self.assertIn("管理員", texts[texts.index("隊部公務機") + 1])
        self.assertIn("隊長", texts[texts.index("林大明") + 2])

    def card_after(self, words, user=CAPTAIN):
        self.say(words, user)
        return self.card()

    def test_an_account_under_a_name_that_is_not_on_the_roster_is_set_apart(self):
        texts = flex_texts(self.card_after("綁定清單")["contents"])
        apart = texts[texts.index("🚫 不在名冊"):]
        self.assertIn("林大明", apart)                        # the 幹部 registered before the 名冊 was the gate
        self.assertLess(texts.index("林小明"), texts.index("🚫 不在名冊"))

    def test_names_shared_by_several_accounts_come_first_with_the_way_out(self):
        share_a_name(self.store, "U7", "林小明", "冒名的人")
        card = self.card_after("綁定清單")
        texts = flex_texts(card["contents"])
        shared = texts[texts.index("⚠️ 同一個姓名有好幾個帳號"):texts.index("✅ 已登記")]
        self.assertIn("林小明", shared)
        self.assertTrue(any("明明" in line and "冒名的人" in line for line in shared))
        self.assertEqual([box["action"]["text"] for box in walk_actions(card) if box["action"].get("text", "").startswith("解除登記")],
                         ["解除登記 林小明"])

    def test_an_account_without_a_line_name_shows_the_end_of_its_user_id(self):
        self.register("U3", "蕭志明")                         # the fake LINE knows no display name for U3
        texts = flex_texts(self.card_after("綁定清單")["contents"])
        self.assertIn("…U3", texts[texts.index("蕭志明") + 1])

    def test_the_panel_and_the_commands_lead_to_it(self):
        panel = self.card_after("人員管理")
        self.assertIn("綁定清單", flex_texts(panel["contents"]))
        self.assertIn("綁定清單", [box["action"].get("text") for box in walk_actions(panel)])
        help_text = self.say("人員管理指令")
        self.assertIn("綁定清單", help_text)
        self.assertIn("解除登記 王小明", help_text)

    def test_a_long_list_stays_within_lines_limits(self):
        for count in (120, 300):
            with self.subTest(count=count):
                names = [f"{a}{b}測試" for a in "趙錢孫李周吳鄭王馮陳褚衛蔣沈韓楊朱秦尤許何呂施張孔曹嚴華金魏"
                         for b in "甲乙丙丁戊己庚辛壬癸"][:count]
                accounts = [{"user_id": f"U{index:032x}", "staff_name": name, "name": f"LINE 上很長的顯示名稱 {index}"}
                            for index, name in enumerate(names)]
                started = time.monotonic()
                card = binding_card(accounts, names, {}, officers={}, admins=())
                self.assertLess(time.monotonic() - started, 2.0)      # cutting a long list costs no more than building it
                check_line_limits(self, card)
                bubbles = card["contents"]["contents"] if card["contents"]["type"] == "carousel" else [card["contents"]]
                self.assertLessEqual(len(bubbles), 12)
                self.assertTrue(all(flex_size(bubble) <= FLEX_BUBBLE_LIMIT for bubble in bubbles))
                texts = flex_texts(card["contents"])
                listed = [name for name in names if name in texts]
                if count == 120:
                    self.assertEqual(len(listed), 120)
                else:                                              # what does not fit is owned up to, exactly
                    self.assertGreater(len(listed), 100)
                    self.assertLess(len(listed), count)
                    self.assertTrue(any(f"最後 {count - len(listed)} 個帳號沒有列出" in line for line in texts))


def walk_actions(node):
    """Every node of a Flex message that carries an action, depth first."""
    if isinstance(node, list):
        return [found for child in node for found in walk_actions(child)]
    if isinstance(node, dict):
        return ([node] if isinstance(node.get("action"), dict) else []) + \
            [found for key, child in node.items() if key != "action" for found in walk_actions(child)]
    return []


class ReleaseTests(BindingCase):
    def test_a_cadre_releases_an_account_and_it_is_out(self):
        reply = self.say("解除登記 陳小華")
        self.assertIn("已解除「陳小華」的登記（LINE：華華）", reply)
        self.assertIsNone(self.name("U2"))
        self.assertEqual(self.transport.pushes, [])              # nobody is pushed: it costs quota
        self.assertIn("設定", self.ask("我的班表", "U2"))         # the gate, asking for a name again
        self.assertNotIn("班表日期", self.reply())

    def test_an_administrator_releases_too_and_nobody_else(self):
        self.assertIn("已解除「陳小華」的登記", self.say("解除登記 陳小華", "U9"))
        self.say("解除登記 林小明", "U2")                        # released just now: the gate keeps them out
        self.assertEqual(self.name("U1"), "林小明")
        self.register("U3", "蕭志明")
        reply = self.say("解除登記 林小明", "U3")
        self.assertIn("只有幹部和管理員可以解除別人的登記", reply)
        self.assertIn("「解除」", reply)
        self.assertEqual(self.name("U1"), "林小明")

    def test_letting_go_of_ones_own_name_is_as_it_was(self):
        for words, user in (("解除", "U1"), ("取消登記", "U2"), ("解除綁定", CAPTAIN)):
            with self.subTest(words=words):
                self.assertIn("已取消姓名登記", self.say(words, user))
                self.assertIsNone(self.name(user))

    def test_a_name_no_account_has(self):
        self.assertIn("沒有 LINE 帳號登記「蕭志明」", self.say("解除登記 蕭志明"))

    def test_the_command_alone_explains_itself_and_releases_nobody(self):
        reply = self.say("解除登記")
        self.assertIn("例如：解除登記 王小明", reply)
        self.assertEqual(self.name(CAPTAIN), "林大明")         # not their own registration either
        self.assertIn("「解除」", self.say("解除登記", "U1"))   # someone else: how to let go of their own
        self.assertEqual(self.name("U1"), "林小明")

    def test_the_other_words_for_it_take_a_name_as_well(self):
        self.assertIn("已解除「陳小華」的登記", self.say("解除綁定 陳小華"))
        self.assertIn("已解除「林小明」的登記", self.say("取消登記 林小明", "U9"))
        self.assertIn("已解除「林大明」的登記", self.say("取消綁定 林大明", "U9"))

    def test_a_number_for_a_name_with_one_account_is_told_what_works(self):
        reply = self.say("解除登記 陳小華 2")
        self.assertIn("只登記在 1 個 LINE 帳號上", reply)
        self.assertIn("傳「解除登記 陳小華」", reply)
        self.assertNotIn("陳小華 2", reply)
        self.assertEqual(self.name("U2"), "陳小華")

    def test_a_name_on_fourteen_accounts_still_makes_a_reply_line_takes(self):
        for index in range(13):
            share_a_name(self.store, f"V{index:02d}", "陳小華", f"帳號{index}", at=float(index + 1))
        reply = self.say("解除登記 陳小華")
        self.assertIn("14. ", reply)
        self.assertLessEqual(len(self.transport.raw[-1][-1]["quickReply"]["items"]), 13)
        self.assertIn("14 個帳號", self.say("解除登記 陳小華 ALL"))

    def test_a_cadre_released_together_with_an_ordinary_account(self):
        self.store.release(CAPTAIN)
        with self.store.lock, self.store.db:      # the 幹部 under a name an ordinary account has too (older data)
            self.store.db.execute("drop index if exists users_staff_name")
            self.store.db.execute("update users set staff_name='陳小華', name='隊長的LINE' where user_id=?", (CAPTAIN,))
        reply = self.say("解除登記 陳小華 全部", "U9")
        self.assertIn("隊長的LINE 仍是幹部", reply)
        self.assertIn("其餘被解除的帳號現在沒有登記姓名，看不到班表", reply)
        self.assertIn(f"移除幹部 {CAPTAIN}", reply)

    def test_three_accounts_released_one_by_one_never_hit_the_wrong_one(self):
        share_a_name(self.store, "U7", "陳小華", "舊手機", at=1.0)
        share_a_name(self.store, "U8", "陳小華", "冒名", at=2.0)       # U2 (華華) registered last: the real one
        first = self.say("解除登記 陳小華")
        self.assertEqual([line[:2] for line in first.splitlines() if line[:2] in ("1.", "2.", "3.")], ["1.", "2.", "3."])
        self.assertRegex(first, r"3\. 華華｜\d+/\d+ 登記｜…U2")       # LINE name, the day it registered, the id's end
        for tail in ("…U7", "…U8", "…U2"):
            self.assertIn(tail, first)                               # the rows can be told apart
        second = self.say("解除登記 陳小華 1")                         # 舊手機 goes
        self.assertIn("舊手機", second.splitlines()[0])
        rows = [line for line in second.splitlines() if line[:2] in ("1.", "2.")]
        self.assertEqual([("冒名" in rows[0]), ("華華" in rows[1])], [True, True])    # the list again, renumbered
        labels = [item["action"]["label"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertEqual(labels[:2], ["解除第 1 個", "解除第 2 個"])
        self.say("解除登記 陳小華 1")                                   # read off the new list: 冒名
        self.assertEqual([p["user_id"] for p in self.store.users_named("陳小華")], ["U2"])

    def test_the_shared_tile_of_the_list_tells_the_accounts_apart(self):
        share_a_name(self.store, "U7", "陳小華", "華華")                # the same LINE name on both
        self.say("綁定清單")
        texts = flex_texts(self.card()["contents"])
        self.assertTrue(any("…U2" in line and "…U7" in line for line in texts))

    def test_a_colon_or_no_space_before_the_name_is_understood(self):
        self.assertIn("只有幹部和管理員", self.say("取消登記:林大明", "U1"))   # an ordinary member, however it is typed
        self.assertEqual(self.name(CAPTAIN), "林大明")
        self.assertIn("已解除「陳小華」的登記", self.say("解除登記：陳小華"))
        self.assertIn("已解除「林小明」的登記", self.say("解除登記林小明"))

    def test_a_number_that_is_no_number_shows_the_list(self):
        share_a_name(self.store, "U7", "陳小華", "冒名")
        for which in ("➀", "二", "0", "-1"):
            with self.subTest(which=which):
                self.assertIn("1. ", self.say(f"解除登記 陳小華 {which}"))
        self.assertEqual(self.store.registered()["陳小華"], 2)

    def test_the_reply_says_the_name_is_free_for_whoever_registers_it_first(self):
        reply = self.say("解除登記 陳小華")
        self.assertIn("「陳小華」現在沒有人登記", reply)
        self.assertIn("已登記：陳小華", self.say("設定 陳小華", "U3"))     # any account without a name: at once
        self.assertIsNone(self.name("U2"))

    def test_a_cadre_who_has_a_name_cannot_take_over_the_one_they_released(self):
        self.say("解除登記 陳小華")
        reply = self.say("設定 陳小華")                                 # CAPTAIN is 林大明: a change of name
        self.assertIn("改名申請送出了", reply)
        self.assertEqual(self.name(CAPTAIN), "林大明")

    def test_releasing_a_name_that_is_off_the_roster_says_it_has_to_be_added_first(self):
        self.store.set_staff_name("U3", "路人甲", "路人")
        reply = self.say("解除登記 路人甲")
        self.assertIn("已解除「路人甲」的登記", reply)
        self.assertIn("「路人甲」不在名冊上", reply)
        self.assertIn("新增人員 路人甲", reply)
        self.assertNotIn("本人可以再傳", reply)

    def test_releasing_a_cadre_or_an_administrator_says_they_still_get_in(self):
        reply = self.say("解除登記 林大明", "U9")                        # CAPTAIN, a 幹部
        self.assertIn("仍是幹部", reply)
        self.assertNotIn("看不到班表", reply)
        self.assertIn(f"移除幹部 {CAPTAIN}", reply)
        self.assertNotIn("新增幹部", reply)                               # nobody new to make a 幹部 here
        self.assertIn("人員名冊", self.say("人員名冊"))                   # and so he does
        self.store.set_staff_name("U9", "蕭志明", "公務機")
        reply = self.say("解除登記 蕭志明")
        self.assertIn("仍是管理員", reply)
        self.assertNotIn("看不到班表", reply)

    def test_releasing_an_account_turns_its_notifications_off(self):
        self.store.set_notify("U2", True)
        self.say("解除登記 陳小華")
        self.assertEqual(self.store.user("U2")["notify"], 0)

    def test_with_several_accounts_the_cadre_says_which(self):
        share_a_name(self.store, "U7", "林小明", "冒名的人")
        reply = self.say("解除登記 林小明")
        self.assertIn("「林小明」登記在 2 個 LINE 帳號上", reply)
        self.assertIn("解除登記 林小明 2", reply)
        self.assertEqual(self.store.registered()["林小明"], 2)   # nothing yet
        number = next(line.split(".")[0] for line in reply.splitlines() if "冒名的人" in line)
        self.assertIn("冒名的人", self.say(f"解除登記 林小明 {number}"))
        self.assertEqual([p["user_id"] for p in self.store.users_named("林小明")], ["U1"])

    def test_all_accounts_of_a_name_at_once_and_a_wrong_number(self):
        share_a_name(self.store, "U7", "林小明", "冒名的人")
        self.assertIn("1. ", self.say("解除登記 林小明 9"))
        self.assertEqual(self.store.registered()["林小明"], 2)
        self.assertIn("2 個帳號", self.say("解除登記 林小明 全部"))
        self.assertNotIn("林小明", self.store.registered())

    def test_removing_someone_from_the_roster_points_to_it(self):
        self.assertIn("解除登記 林小明", self.say("刪除人員 林小明"))


class DisplayNameTests(BindingCase):
    """「綁定清單」 shows LINE names: LINE is asked again when the account next writes, once a day at most."""

    def renamed(self, **names):
        calls = []
        self.answered = []                         # how many replies had gone out when LINE was asked
        def display_name(user, chat):
            calls.append(user); self.answered.append(len(self.transport.replies))
            return names.get(user)
        self.transport.display_name = display_name
        return calls

    def test_line_is_asked_only_after_the_answer_has_gone_out(self):
        self.renamed(U1="小明改名了")
        self.age("U1", 25 * 3600)
        before = len(self.transport.replies)
        self.ask("用法說明", "U1")
        self.assertEqual(self.answered, [before + 1])

    def test_an_account_whose_name_was_released_is_not_looked_up(self):
        self.age("U2", 25 * 3600)
        self.store.release("U2")
        calls = self.renamed(U2="華華改名了")
        self.ask("我的設定", "U2")
        self.assertEqual(calls, [])

    def age(self, user, seconds):
        with self.store.lock, self.store.db:
            self.store.db.execute("update users set name_at=name_at-? where user_id=?", (seconds, user))

    def shown(self, name):
        self.say("綁定清單")
        texts = flex_texts(self.card()["contents"])
        return texts[texts.index(name) + 1]

    def test_a_new_line_name_shows_once_the_account_writes_again(self):
        calls = self.renamed(U1="小明改名了")
        self.age("U1", 25 * 3600)
        self.ask("用法說明", "U1")
        self.assertEqual(calls, ["U1"])
        self.assertEqual(self.shown("林小明"), "小明改名了")

    def test_line_is_not_asked_twice_in_a_day(self):
        calls = self.renamed(U1="小明改名了")
        self.ask("用法說明", "U1"); self.ask("我的設定", "U1")
        self.assertEqual(calls, [])
        self.assertEqual(self.shown("林小明"), "明明")

    def test_when_line_gives_nothing_the_known_name_stays_and_it_waits_a_day(self):
        calls = self.renamed()
        self.age("U1", 25 * 3600)
        self.ask("用法說明", "U1"); self.ask("用法說明", "U1")
        self.assertEqual(calls, ["U1"])
        self.assertEqual(self.shown("林小明"), "明明")

    def test_an_error_from_line_does_not_stop_the_answer_and_is_not_asked_again_today(self):
        calls = []
        def broken(user, chat):
            calls.append(user)
            raise RuntimeError("LINE API 500")
        self.transport.display_name = broken
        self.age("U1", 25 * 3600)
        with redirect_stdout(io.StringIO()):
            self.assertIn("用法", self.ask("用法說明", "U1"))
            self.assertIn("用法", self.ask("用法說明", "U1"))
        self.assertEqual(calls, ["U1"])                          # asked once today, not before every answer

    def test_someone_without_a_name_is_not_looked_up(self):
        calls = self.renamed(U3="路人")
        self.ask("用法說明", "U3")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
