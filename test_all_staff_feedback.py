"""「意見 …」: feedback is kept with its sender's name and passed on to the administrators.

A push counts against the LINE quota, so the administrators are pushed at most once a day; whatever arrives
later that day rides along, for free, with the next reply the bot sends one of them.
"""
import unittest
from datetime import date
from unittest.mock import patch

from all_staff.bot import FEEDBACK_A_DAY, FEEDBACK_LENGTH, feedback_digest
from test_all_staff_bot import BotCase, image_event, postback_event, text_event

TODAY = date(2026, 10, 7)


class FeedbackCase(BotCase):
    friends = {"U1", "U2", "U3", "U9"}
    settings = {"admins": ("U9",), "notify": False}     # as in production: automatic notifications are off

    def setUp(self):
        super().setUp()
        self.register("U1", "林小明")
        self.transport.replies.clear(); self.transport.raw.clear()

    def say(self, value, user="U1", day=TODAY):
        with patch("all_staff.bot.today", return_value=day):
            return self.ask(value, user)

    def pushed(self, to="U9"):
        return [messages for target, messages in self.transport.pushes if target == to]

    def rows(self):
        return self.store.recent_feedback(50)[::-1]


class FeedbackTests(FeedbackCase):
    def test_feedback_is_kept_with_the_senders_name_and_pushed_to_the_administrator(self):
        reply = self.say("意見 10/8 的班表日期沒有讀到")
        self.assertIn("收到了", reply)
        self.assertIn("你的姓名（林小明）", reply)
        row, = self.rows()
        self.assertEqual((row["user_id"], row["staff_name"], row["display_name"], row["body"]),
                         ("U1", "林小明", "明明", "10/8 的班表日期沒有讀到"))
        push, = self.pushed()
        self.assertIn("📮 新的意見回饋（1 則）", push[0])
        self.assertIn(f"#{row['id']}・", push[0])
        self.assertIn("林小明（LINE：明明）", push[0])
        self.assertIn("10/8 的班表日期沒有讀到", push[0])
        self.assertEqual(self.pushed("U1"), [])
        self.assertIsNotNone(self.rows()[0]["pushed_at"])

    def test_the_menu_button_types_the_word_and_the_bot_waits_for_the_rest(self):
        from all_staff import rich_menu
        action = next(a["action"] for a in rich_menu.menu()["areas"] if a["action"]["label"] == "意見回饋")
        self.assertEqual((action["inputOption"], action["fillInText"]), ("openKeyboard", "意見 "))
        count = len(self.transport.replies)
        self.bot.handle(postback_event("U1", action["data"]))          # opening the keyboard is not a message
        self.assertEqual(len(self.transport.replies), count)
        self.assertEqual(self.rows(), [])

    def test_the_word_alone_explains_how(self):
        for value in ("意見", "意見回饋", "回饋 ", "建議：", "意見　"):
            reply = self.say(value)
            self.assertIn("接在「意見」後面", reply, value)
            action = self.transport.raw[-1][-1]["quickReply"]["items"][0]["action"]
            self.assertEqual(action["fillInText"], "意見 ")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.transport.pushes, [])

    def test_other_openings_and_punctuation_are_accepted(self):
        for value, body in (("意見：字太小", "字太小"), ("意見回饋 字太小", "字太小"), ("回饋:字太小", "字太小"),
                            ("建議，加一個按鈕", "加一個按鈕"), ("回報 我的名字讀錯了", "我的名字讀錯了"),
                            ("  意見　全形空白也可以  ", "全形空白也可以")):
            self.say(value)
            self.assertEqual(self.rows()[-1]["body"], body, value)

    def test_the_message_is_kept_as_typed(self):
        body = "今天班表 的 10/8 那張，\n查 72 的時候日期不對。\n通知 開 也沒用"
        self.assertIn("收到了", self.say("意見 " + body))
        self.assertEqual(self.rows()[-1]["body"], body)
        self.assertEqual(len(self.transport.replies), 1)               # nothing in it was run as a command
        self.assertEqual((self.store.user("U1") or {}).get("notify"), 0)

    def test_a_pasted_passenger_message_inside_feedback_is_not_recorded_as_one(self):
        self.say("意見 這則讀錯了：\n2026年10月07日\n【國際 – 出境】\n12-13時：2架400人\n16-17時：2架400人")
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(self.store.flow_on("U1", TODAY))

    def test_someone_without_a_name_is_passed_on_with_their_line_name(self):
        reply = self.say("意見 找不到設定姓名的按鈕", "U2")
        self.assertIn("你的 LINE 名稱", reply)
        self.assertIn("未登記姓名（LINE：華華）", self.pushed()[0][0])

    def test_what_the_sender_was_looking_at_goes_along(self):
        self.store.add_roster("Cteam", "run-1", "U2", 5, "2026-10-01")
        self.say("我的班表")
        self.say("意見 我的時數少了一小時")
        self.assertIn("（當時在看：群組", self.pushed()[0][0])
        self.assertIn("上傳的10/1（四）班表）", self.pushed()[0][0])

    def test_a_long_message_is_cut_and_the_sender_is_told(self):
        reply = self.say("意見 " + "很長" * FEEDBACK_LENGTH)
        self.assertIn(f"只留下前 {FEEDBACK_LENGTH} 個字", reply)
        self.assertEqual(len(self.rows()[-1]["body"]), FEEDBACK_LENGTH)

    def test_one_person_cannot_flood_the_list(self):
        for number in range(FEEDBACK_A_DAY):
            self.assertIn("收到了", self.say(f"意見 第 {number} 則"))
        self.assertIn("今天已經收到你很多則意見", self.say("意見 再一則"))
        self.assertEqual(len(self.rows()), FEEDBACK_A_DAY)
        self.assertIn("收到了", self.say("意見 別人的不受影響", "U2"))

    def test_messages_in_a_group_are_not_feedback(self):
        self.bot.handle(text_event("U1", "意見 這是在群組裡講的話", group="Cgroup"))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.transport.replies, [])

    def test_text_the_bot_does_not_understand_offers_the_button(self):
        self.assertIn("看不懂", self.say("今天好熱"))
        action = self.transport.raw[-1][-1]["quickReply"]["items"][0]["action"]
        self.assertEqual((action["label"], action["fillInText"]), ("意見回饋", "意見 "))
        self.assertEqual(self.rows(), [])


class OnePushADayTests(FeedbackCase):
    def test_the_second_message_of_a_day_waits_for_the_administrators_next_visit(self):
        self.say("意見 第一則")
        self.say("意見 第二則", "U2")
        self.assertEqual(len(self.pushed()), 1)
        self.assertNotIn("第二則", self.pushed()[0][0])
        reply = self.say("我的設定", "U9")                              # any reply to the administrator carries it
        self.assertIn("📮 新的意見回饋（1 則）", reply)
        self.assertIn("第二則", reply)
        self.assertIn("姓名：尚未登記", reply)
        self.assertNotIn("第一則", reply)                               # that one was pushed already
        self.assertNotIn("意見回饋", self.say("我的設定", "U9"))        # shown once
        self.assertEqual(len(self.pushed()), 1)

    def test_the_feedback_comes_first_so_the_buttons_of_the_answer_stay(self):
        self.say("意見 第一則"); self.say("意見 第二則")
        self.say("我的設定", "U9")
        first, answer = self.transport.raw[-1]
        self.assertIn("第二則", first["text"])
        self.assertNotIn("quickReply", first)
        self.assertEqual(answer["quickReply"]["items"][0]["action"]["label"], "設定姓名")

    def test_a_button_tap_by_the_administrator_carries_it_too(self):
        self.say("意見 第一則"); self.say("意見 第二則")
        with patch("all_staff.bot.today", return_value=TODAY):
            self.bot.handle(postback_event("U9", "stats|2026-10"))
        self.assertIn("第二則", self.reply())

    def test_other_people_never_see_it(self):
        self.say("意見 第一則"); self.say("意見 第二則")
        for user in ("U1", "U2", "U3"):
            self.assertNotIn("第二則", self.say("我的設定", user))
        self.assertIn("第二則", self.say("我的設定", "U9"))

    def test_the_next_day_there_is_a_push_again_with_everything_still_unseen(self):
        self.say("意見 第一則")
        self.say("意見 第二則")
        self.say("意見 第三則", day=date(2026, 10, 8))
        self.assertEqual(len(self.pushed()), 2)
        self.assertIn("（2 則）", self.pushed()[1][0])
        self.assertIn("第二則", self.pushed()[1][0])
        self.assertIn("第三則", self.pushed()[1][0])
        self.assertEqual(self.store.undelivered_feedback(), [])

    def test_a_push_that_reaches_nobody_does_not_use_up_the_day(self):
        self.transport.friends.discard("U9")                           # no quota left, blocked, not a friend
        self.say("意見 第一則")
        self.assertEqual(self.pushed(), [])
        self.assertEqual(len(self.store.undelivered_feedback()), 1)
        self.transport.friends.add("U9")
        self.say("意見 第二則")
        push, = self.pushed()
        self.assertIn("（2 則）", push[0])
        self.assertEqual(self.store.undelivered_feedback(), [])

    def test_a_reply_that_fails_has_shown_nothing(self):
        self.say("意見 第一則"); self.say("意見 第二則")
        with patch.object(self.transport, "reply", side_effect=RuntimeError("LINE API 400: Invalid reply token")):
            with self.assertRaises(RuntimeError):
                self.say("我的設定", "U9")
        self.assertEqual(len(self.store.undelivered_feedback()), 1)
        self.assertIn("第二則", self.say("我的設定", "U9"))

    def test_a_reply_that_is_already_full_is_left_alone(self):
        self.say("意見 第一則"); self.say("意見 第二則")
        self.bot._turn.admin = True
        five = [{"type": "text", "text": str(n)} for n in range(5)]
        self.bot._reply("token", five)
        self.assertEqual([m["text"] for m in self.transport.raw[-1]], ["0", "1", "2", "3", "4"])
        self.assertEqual(len(self.store.undelivered_feedback()), 1)

    def test_an_administrators_own_feedback_is_treated_like_any_other(self):
        self.say("意見 第一則")
        reply = self.say("意見 我自己測試", "U9")
        self.assertIn("收到了", reply)
        self.assertIn("我自己測試", reply)                              # it rides along with their own reply
        self.assertEqual(self.store.undelivered_feedback(), [])


class FeedbackListTests(FeedbackCase):
    def test_the_administrator_can_list_the_newest_messages(self):
        self.assertIn("目前沒有任何意見回饋", self.say("意見列表", "U9"))
        for number in range(1, 4):
            self.say(f"意見 第 {number} 則")
        reply = self.say("意見列表", "U9")
        self.assertIn("📮 最近的意見回饋（3 則）", reply)
        self.assertLess(reply.index("第 1 則"), reply.index("第 3 則"))
        self.assertEqual(len(self.transport.raw[-1]), 1)                # the list itself: nothing in front of it
        self.assertEqual(self.store.undelivered_feedback(), [])

    def test_when_the_list_is_too_long_for_one_message_the_newest_are_kept(self):
        for number in range(1, 8):
            self.store.add_feedback("U1", "林小明", "明明", f"第 {number} 則" + "字" * 990)
        reply = self.say("意見列表", "U9")
        self.assertIn("第 7 則", reply)
        self.assertNotIn("第 1 則", reply)
        self.assertLess(reply.index("第 6 則"), reply.index("第 7 則"))
        self.assertLessEqual(len(self.transport.raw[-1][0]["text"]), 4900)

    def test_nobody_else_can_and_the_words_are_not_kept_as_feedback(self):
        self.say("意見 第一則")
        for value in ("意見列表", "意見 列表", "回饋清單"):
            self.assertIn("看不懂", self.say(value, "U2"), value)
        self.assertEqual(len(self.rows()), 1)

    def test_a_digest_leaves_out_what_does_not_fit_and_says_so(self):
        items = [{"id": n, "created_at": 1791300000 + n, "staff_name": "林小明", "display_name": "明明",
                  "body": "字" * 2000, "context": None} for n in range(1, 5)]
        message, shown = feedback_digest(items)
        self.assertEqual([item["id"] for item in shown], [1, 2])
        self.assertIn("…還有 2 則，輸入「意見列表」查看。", message["text"])
        self.assertLessEqual(len(message["text"]), 4900)
        self.assertEqual(feedback_digest([]), (None, []))


class FeedbackWithoutAdministratorsTests(FeedbackCase):
    settings = {"notify": False}

    def test_it_is_kept_and_acknowledged_and_nothing_is_pushed(self):
        self.assertIn("收到了", self.say("意見 沒有管理員也要留下來"))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.transport.pushes, [])
        self.assertIn("看不懂", self.say("意見列表", "U9"))
        self.assertEqual(len(self.rows()), 1)


class FeedbackStorageTests(FeedbackCase):
    def test_feedback_survives_a_restart_and_old_databases_get_the_table(self):
        from all_staff.bot import Store
        self.say("意見 第一則")
        self.store.db.close()
        again = Store(self.path)
        self.assertEqual([row["body"] for row in again.recent_feedback()], ["第一則"])
        again.db.execute("drop table feedback"); again.db.commit(); again.db.close()
        upgraded = Store(self.path)
        self.assertEqual(upgraded.recent_feedback(), [])
        self.assertEqual(upgraded.add_feedback("U1", "林小明", "明明", "又一則"), 1)

    def test_a_photo_sent_privately_is_still_a_roster_not_feedback(self):
        self.bot.handle(image_event("U1"))
        self.assertEqual(self.rows(), [])
        self.assertIn("班表辨識完成", self.reply())


if __name__ == "__main__":
    unittest.main()
