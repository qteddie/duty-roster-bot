"""不推播 (``Settings.push`` off, the default in production): the bot never pushes. LINE's free plan has 200 pushed
messages a month, a push request counting once per recipient; a reply, with the token of the person's own event, is
free. Whatever the bot would push someone waits in their 收件匣 and comes in front of the next reply they get, whole
groups oldest first, or when they ask (「收件匣」, the administrators' menu button 「辨識回報」)."""
import contextlib
import copy
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from all_staff.bot import (MAILBOX, MAILBOX_KEEP, NOTIFY_NO_PUSH, STILL_READING, help_card, mail_left, text, today,
                           undated_notice)
from test_all_staff_bot import (RESULT, BotCase, FakeAnalyzer, FakeTransport, flex_nodes, flex_texts, image_event,
                                postback_event, text_event)
from test_all_staff_flow import MESSAGE as FLOW_MESSAGE, FlowNoticeCase
from test_all_staff_roster_gate import GATE_TILE, GateCase

ADMIN = "U9"


class DemoError(Exception):
    """As ``all_staff.demo`` raises it: a roster that could not be read, with a reason for the uploader."""


class NotARoster(Exception):
    """As the roster gate raises it: a photo that is not a roster at all."""


class StrictTransport(FakeTransport):
    """FakeTransport that remembers every push call, one that fails too, and refuses a reply token used twice."""

    def __init__(self, friends=()):
        super().__init__(friends)
        self.push_calls, self.used = [], set()

    def push(self, to, messages):
        self.push_calls.append(to)
        return super().push(to, messages)

    def reply(self, token, messages):
        if token in self.used:
            raise RuntimeError("LINE API 400: Invalid reply token")
        self.used.add(token)
        return super().reply(token, messages)


def strict(case):
    """Put a ``StrictTransport`` under ``case``'s bot."""
    case.transport = StrictTransport(case.friends)
    case.bot.transport = case.transport


def kinds(messages):
    return [message["type"] for message in messages]


def pickers(message):
    """The date pickers on a card, as (data, the label of the box)."""
    return [node["action"]["data"] for node in flex_nodes(message.get("contents", {}))
            if node.get("action", {}).get("type") == "datetimepicker"]


class NoPushCase(BotCase):
    """Production's way: no pushes, no automatic notifications, an administrator, pictures on."""
    friends = {"U1", "U2", "U3"}   # the administrator is not even a friend: nothing is pushed to anybody
    settings = {"push": False, "notify": False, "admins": (ADMIN,), "public_base": "https://bot.example"}

    def setUp(self):
        super().setUp()
        strict(self)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.out = self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.assertEqual(self.transport.push_calls, [])                      # whatever happened, nothing was pushed
        super().tearDown()

    def mail(self, user):
        return [group["messages"] for group in self.store.undelivered_mail(user)]

    def post_photo(self, user="U2"):
        self.bot.handle(image_event(user, "Cgroup"))

    def fail_with(self, kind, reason):
        def analyze(payload, name):
            raise kind(reason)
        self.analyzer.analyze = analyze


class GroupRosterTests(NoPushCase):
    def test_a_read_roster_files_its_report_and_picture_as_one_group(self):
        self.post_photo()
        group, = self.mail(ADMIN)
        report, picture = group
        self.assertTrue(report["altText"].startswith("✅ 群組班表辨識成功"))
        self.assertEqual(picture["originalContentUrl"], "https://bot.example/runs/run-1/staff-table-v1.jpg")
        self.assertEqual((self.transport.replies, self.mail("U2")), ([], []))   # the group and the uploader: silent
        self.ask("我的設定", ADMIN)
        sent = self.transport.raw[-1]
        self.assertEqual(sent[:2], group)                                    # the report first, with its picture
        self.assertIn("姓名：", sent[2]["text"])                             # the reply itself, last, whole
        self.assertIn("quickReply", sent[2])
        self.assertEqual(self.mail(ADMIN), [])                               # shown once
        self.ask("我的設定", ADMIN)
        self.assertEqual(len(self.transport.raw[-1]), 1)

    def test_an_unreadable_roster_tells_the_uploader_on_their_next_reply(self):
        self.fail_with(DemoError, "找不到表格")
        self.post_photo()
        notice, = self.mail("U2")
        self.assertRegex(notice[0]["text"], r"^⚠ 你在群組 \d+/\d+ \d\d:\d\d 傳的班表無法辨識：找不到表格")   # which photo
        self.assertIn("這張沒有更新班表", notice[0]["text"])   # still true after a later good one
        report, = self.mail(ADMIN)
        self.assertTrue(report[0]["altText"].startswith("⚠️ 群組班表辨識失敗"))
        self.assertIn("已放進他的收件匣", flex_texts(report[0]["contents"]))   # not 「已私訊告知」: nothing was sent
        self.ask("說明", "U2")
        self.assertIn("無法辨識", self.transport.raw[-1][0]["text"])
        self.assertEqual(self.mail("U2"), [])

    def test_a_photo_that_is_no_roster_is_only_reported(self):
        self.fail_with(NotARoster, "不像班表")
        self.post_photo()
        self.assertEqual(self.mail("U2"), [])                                # never uploaded a roster: left alone
        report, = self.mail(ADMIN)
        self.assertTrue(report[0]["altText"].startswith("🚫 不像班表"))
        self.assertIn("沒有私訊", flex_texts(report[0]["contents"]))

    def test_an_undated_roster_is_published_at_once_and_dated_on_the_report(self):
        self.analyzer.data["run"]["roster_date"] = None
        self.post_photo()
        self.assertEqual(self.store.held_rosters(), [])                      # not held: nobody could be asked in time
        self.assertEqual(self.store.roster_for_user("U1", dated=True)["run_id"], "run-1")   # everyone sees it now
        (report, _), = self.mail(ADMIN)                                      # the report, not a date question
        self.assertIn("日期未辨識", flex_texts(report["contents"]))
        self.assertEqual(pickers(report), ["redate|run-1"])                  # 📅 日期不對？點這裡改這張的日期
        self.assertIn("日期讀不到：不推播，所以不等管理員，直接發布", self.out.getvalue())
        day = today()
        self.bot.handle({**postback_event(ADMIN, "redate|run-1"),
                         "postback": {"data": "redate|run-1", "params": {"date": day.isoformat()}}})
        self.assertEqual(self.store.roster_by_run("run-1")["roster_date"], day.isoformat())
        self.assertIn("的日期已改為", self.transport.raw[-1][-1]["text"])

    def test_nobody_hears_of_a_roster_even_with_notifications_asked_for(self):
        self.bot.settings.notify = True                                      # LINE_V2_NOTIFY=1 without LINE_V2_PUSH
        self.register("U1", "林小明")
        self.store.set_notify("U1", True)
        self.assertEqual(self.bot._audience(), [])
        self.post_photo()
        self.assertEqual(self.mail("U1"), [])                                # not everybody's 收件匣 either
        self.assertEqual(self.ask("通知 開"), NOTIFY_NO_PUSH)
        self.assertNotIn("通知", self.ask("我的設定"))


class UploaderWithoutAdministratorsTests(NoPushCase):
    settings = {**NoPushCase.settings, "admins": ()}

    def test_the_undated_notice_carries_its_picker_on_the_card(self):
        self.register("U2", "陳小華")
        self.analyzer.data["run"]["roster_date"] = None
        self.post_photo()
        (notice,), = self.mail("U2")
        self.assertEqual(notice["type"], "flex")                             # a quick reply would be lost in front
        self.assertNotIn("quickReply", notice)                               # of another reply
        self.assertEqual(pickers(notice), ["redate|run-1"])
        self.assertIn("選這張的日期", "\n".join(flex_texts(notice["contents"])))
        self.ask("我的設定", "U2")
        self.assertEqual(self.transport.raw[-1][0], notice)
        self.assertIn("quickReply", self.transport.raw[-1][-1])

    def test_with_pushes_the_notice_is_the_text_with_a_quick_reply_as_before(self):
        roster = {"run_id": "run-1", "roster_date": None, "sent_at": time.time()}
        self.assertEqual(undated_notice(roster)["type"], "text")
        self.assertIn("quickReply", undated_notice(roster))
        self.assertEqual(undated_notice(roster, inline=True)["altText"], undated_notice(roster)["text"].splitlines()[0])


class FlowTests(FlowNoticeCase):
    settings = {**FlowNoticeCase.settings, "push": False}

    def setUp(self):
        super().setUp()
        strict(self)

    def tearDown(self):
        self.assertEqual(self.transport.push_calls, [])
        super().tearDown()

    def test_each_group_message_files_its_card_and_the_digest_does_not_repeat_it(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.post()
        (card,), = [group["messages"] for group in self.store.undelivered_mail("U9")]
        self.assertTrue(card["altText"].startswith("✅ 群組人流資料讀取成功"))
        self.assertEqual(self.pending(), [])                                 # filed counts as reaching them
        self.say("我的設定")
        sent = self.transport.raw[-1]
        self.assertEqual(sent[0], card)
        self.assertFalse(any("人流資料讀取結果" in message.get("text", "") for message in sent))
        self.assertIn("姓名：", sent[-1]["text"])


class FeedbackTests(NoPushCase):
    def test_feedback_is_never_pushed_and_rides_on_the_next_reply(self):
        self.assertIn("收到了", self.ask("意見 9/30 的日期讀錯了"))
        self.assertIsNone(self.store.last_feedback_push())
        self.assertEqual(self.mail(ADMIN), [])                               # not in the 收件匣: in the digest
        self.ask("我的設定", ADMIN)
        digest, answer = self.transport.raw[-1]
        self.assertIn("📮 新的意見回饋（1 則）", digest["text"])
        self.assertIn("9/30 的日期讀錯了", digest["text"])
        self.assertEqual(self.store.undelivered_feedback(), [])

    def test_the_digests_come_first_then_the_mailbox_then_the_reply(self):
        self.store.add_feedback("U1", "林小明", "明明", "第一則")
        self.bot._file(ADMIN, [text("回報一")])
        self.ask("我的設定", ADMIN)
        digest, report, answer = self.transport.raw[-1]
        self.assertIn("意見回饋", digest["text"])
        self.assertEqual(report["text"], "回報一")
        self.assertIn("姓名：", answer["text"])


class NameRequestTests(NoPushCase):
    def test_the_request_and_its_answer_each_wait_for_a_reply(self):
        self.store.add_roster("Cgroup", "run-1", "U2", 5, today().isoformat())   # today's roster: 我的班表 has a card
        self.register("U1", "林小明")
        reply = self.ask("設定 王小明")
        self.assertIn("有結果之後，你下次傳訊息給我就會看到", reply)
        self.assertNotIn("私訊不到管理員", reply)
        (card,), = self.mail(ADMIN)
        number = self.store.pending_name_request("U1")["id"]
        self.assertIsNotNone(self.store.pending_name_request("U1")["pushed_at"])   # the 12-hour gap still holds
        again = self.ask("設定 陳小明")                                          # changed their mind at once
        self.assertEqual(len(self.mail(ADMIN)), 1)                               # one card, not a flood
        self.assertIn("管理員傳「姓名申請」就會看到這次的申請", again)
        self.assertNotIn("私訊", again)                                          # nobody was messaged
        self.ask("我的設定", ADMIN)
        self.assertEqual(self.transport.raw[-1][0], card)
        number = self.store.pending_name_request("U1")["id"]
        self.bot.handle(postback_event(ADMIN, f"rename|{number}|yes"))
        self.assertIn("對方下次傳訊息給我時就會看到", self.reply())
        self.assertEqual(self.name("U1"), "陳小明")
        (answer,), = self.mail("U1")                                         # no 我的班表 card frozen in it:
        self.assertIn("改名申請通過了", answer["text"])                       # read later, its hour would be gone
        self.ask("說明")
        self.assertEqual(self.transport.raw[-1][0], answer)

    def name(self, user):
        return (self.store.user(user) or {}).get("staff_name")


class LateAnswerTests(NoPushCase):
    def test_a_late_answer_still_tries_the_reply(self):
        self.bot._answer("late", "U1", time.monotonic() - 3600, [text("勤務卡")])
        self.assertEqual(self.transport.raw[-1], [text("勤務卡")])
        self.assertEqual(self.mail("U1"), [])

    def test_a_refused_reply_files_the_answer(self):
        self.bot._turn.user = "U1"
        self.bot._file("U1", [text("先前的")])
        self.transport.used.add("spent")
        self.bot._answer("spent", "U1", time.monotonic() - 3600, [text("勤務卡")])
        self.assertEqual(self.mail("U1"), [[text("先前的")], [text("勤務卡")]])   # nothing lost, in order
        self.bot._answer(None, "U1", time.monotonic(), [text("沒有 token")])
        self.assertEqual(self.mail("U1")[-1], [text("沒有 token")])


class PrivatePhotoTests(NoPushCase):
    def slow(self, seconds):
        data = self.analyzer.data

        def analyze(payload, name):
            time.sleep(seconds)
            return data
        self.analyzer.analyze = analyze

    def test_a_photo_still_being_read_gets_one_interim_reply_and_its_result_waits(self):
        self.register("U1", "林小明")
        self.bot.settings.reply_window = .4                                  # the interim reply at 0.2 s
        self.slow(1.0)
        event = image_event("U1")
        self.bot.handle(event)
        token = event["replyToken"]
        self.assertEqual([texts for used, texts in self.transport.replies if used == token], [[STILL_READING]])
        result, = self.mail("U1")
        self.assertEqual(kinds(result), ["text", "flex", "image"])           # summary, duty card, outlined photo
        self.assertIn("班表辨識完成", result[0]["text"])
        self.ask("說明")                                                      # any word
        sent = self.transport.raw[-1]
        self.assertEqual(sent[:3], result)
        self.assertEqual(sent[3]["type"], "flex")                            # the help, last
        self.assertEqual(self.mail("U1"), [])

    def test_a_photo_read_in_time_is_answered_as_ever_and_no_interim_follows(self):
        self.bot.settings.reply_window = .4
        event = image_event("U1")
        self.bot.handle(event)
        time.sleep(.5)                                                       # past the moment the timer was set for
        replies = [texts for used, texts in self.transport.replies if used == event["replyToken"]]
        self.assertEqual(len(replies), 1)
        self.assertIn("班表辨識完成", replies[0][0])
        self.assertEqual(self.mail("U1"), [])

    def test_an_unreadable_private_photo_after_the_interim_waits_too(self):
        self.bot.settings.reply_window = .2

        def analyze(payload, name):
            time.sleep(.5)
            raise DemoError("太模糊")
        self.analyzer.analyze = analyze
        self.bot.handle(image_event("U1"))
        (notice,), = self.mail("U1")
        self.assertIn("這張照片無法辨識：太模糊", notice["text"])

    def test_the_token_goes_to_whoever_takes_it_first(self):
        take = self.bot._interim("first", None)
        self.assertEqual(take(), "first")                                    # the result took it
        self.assertIsNone(take())                                            # and nobody else can
        self.bot.settings.reply_window = .02
        take = self.bot._interim("second", None)
        time.sleep(.2)                                                       # the interim reply took it
        self.assertIsNone(take())
        self.assertEqual([texts for used, texts in self.transport.replies if used == "second"], [[STILL_READING]])
        self.bot.settings.push = True
        self.assertEqual(self.bot._interim("third", None)(), "third")         # with pushes: no interim reply at all
        self.bot.settings.push = False

    def test_the_interim_reply_is_timed_from_when_line_sent_the_event(self):
        """At the production window the interim reply is set for 45 s after LINE sent the photo: a photo that waited 20 s
        for a worker gets it 25 s after it is picked up, while its reply token still works."""
        self.bot.settings.reply_window = 50.0
        seen = []

        class Recording(threading.Timer):
            def __init__(inner, interval, *args, **kwargs):
                seen.append(interval)
                super().__init__(interval, *args, **kwargs)
        with mock.patch("all_staff.bot.threading.Timer", Recording):
            self.bot._interim("a", None)()
            event = image_event("U1")
            event["timestamp"] = int((time.time() - 20) * 1000)
            self.bot._interim("b", None, self.bot._waited(event))()
            event["timestamp"] = int((time.time() - 48) * 1000)                   # nearly too late: at once, nearly
            self.bot._interim("c", None, self.bot._waited(event))()
        self.assertAlmostEqual(seen[0], 45.0, places=1)
        self.assertAlmostEqual(seen[1], 25.0, delta=.5)
        self.assertLess(seen[2], 2.0)

    def test_a_photo_that_fails_after_reading_gets_no_late_interim(self):
        self.bot.settings.reply_window = .2                                  # the interim reply would be at 0.1 s

        def locked(*args, **kwargs):
            raise RuntimeError("database is locked")
        self.store.add_roster = locked
        event = image_event("U1")
        with self.assertRaises(RuntimeError):
            self.bot.handle(event)
        time.sleep(.3)
        self.assertEqual([texts for used, texts in self.transport.replies if used == event["replyToken"]], [])

    def test_the_interim_reply_carries_nothing_else(self):
        self.bot._file("U1", [text("等著的")])
        self.bot.settings.reply_window = .02
        done = threading.Event()
        self.bot._interim("tok", done)
        time.sleep(.2)
        self.assertEqual(self.transport.raw[-1], [text(STILL_READING)])
        self.assertTrue(done.is_set())                                       # the typing indicator stops
        self.assertEqual(self.mail("U1"), [[text("等著的")]])


class DeliveryTests(NoPushCase):
    def file(self, user, *sizes):
        for index, size in enumerate(sizes):
            self.bot._file(user, [text(f"{chr(97 + index)}{n}") for n in range(1, size + 1)])

    def test_whole_groups_oldest_first_in_the_room_the_reply_leaves(self):
        self.file("U1", 1, 2, 3)
        self.ask("我的設定")
        sent = self.transport.raw[-1]
        self.assertEqual(len(sent), 5)
        self.assertEqual([message["text"] for message in sent[:3]], ["a1", "b1", "b2"])
        self.assertEqual(sent[3]["text"], mail_left(1))                      # c1–c3 do not fit: they wait, whole
        self.assertEqual(sent[3]["text"], "📬 還有 1 則訊息，傳「收件匣」查看。")
        self.assertIn("姓名：", sent[4]["text"])
        self.assertIn("quickReply", sent[4])                                 # the reply keeps its buttons, last
        self.assertFalse(any("quickReply" in message for message in sent[:4]))
        self.assertEqual(self.mail("U1"), [[text("c1"), text("c2"), text("c3")]])

    def test_a_group_that_does_not_fit_holds_back_the_later_ones(self):
        self.file("U1", 4, 1)
        self.bot._reply("two", [text("一"), text("二")])                     # no event: nobody's mailbox
        self.assertEqual(self.transport.raw[-1], [text("一"), text("二")])
        self.bot._turn.user = "U1"
        self.bot._reply("two-again", [text("一"), text("二")])
        self.assertEqual(self.transport.raw[-1], [text(mail_left(2)), text("一"), text("二")])
        self.assertEqual(len(self.mail("U1")), 2)

    def test_the_line_names_the_menu_button_for_an_administrator(self):
        self.file(ADMIN, 3, 3)
        self.ask("我的設定", ADMIN)
        self.assertEqual(self.transport.raw[-1][3]["text"], "📬 還有 1 則訊息，傳「收件匣」或按選單「辨識回報」查看。")

    def test_a_full_reply_carries_nothing_and_a_failed_one_shows_nothing(self):
        self.file("U1", 1)
        self.bot._turn.user = "U1"
        five = [text(str(n)) for n in range(5)]
        self.bot._reply("full", five)
        self.assertEqual(self.transport.raw[-1], five)
        self.transport.used.add("spent")
        self.bot._turn.user = "U1"
        with self.assertRaises(RuntimeError):
            self.bot._reply("spent", [text("x")])
        self.assertEqual(self.mail("U1"), [[text("a1")]])                    # only a reply that went out shows it

    def test_only_so_many_wait_and_the_oldest_go(self):
        for number in range(MAILBOX_KEEP + 2):
            self.bot._file("U1", [text(f"#{number}")])
        waiting = self.mail("U1")
        self.assertEqual(len(waiting), MAILBOX_KEEP)
        self.assertEqual(waiting[0], [text("#2")])
        self.assertIn(f"收件匣等著的超過 {MAILBOX_KEEP} 則，刪掉了最舊的 1 則", self.out.getvalue())

    def test_a_group_longer_than_a_reply_is_filed_in_fives_and_only_people_have_one(self):
        self.assertTrue(self.bot._file("U1", [text(str(n)) for n in range(7)]))
        self.assertEqual([len(group) for group in self.mail("U1")], [5, 2])
        self.assertFalse(self.bot._file("Cgroup", [text("x")]))
        self.assertFalse(self.bot._file("U1", []))

    def test_delivered_groups_are_cleared_after_a_week(self):
        self.file("U1", 1)
        self.ask("說明")
        self.bot._file("U2", [text("還沒看")])                                # waiting: never cleared, however old
        with mock.patch("all_staff.bot.time.time", return_value=time.time() + 8 * 86400):
            self.bot._file("U1", [text("新的")])
        self.assertEqual(self.mail("U2"), [[text("還沒看")]])
        self.assertEqual(self.mail("U1"), [[text("新的")]])
        self.assertEqual(self.store._one("select count(*) from mailbox")[0], 2)

    def test_groups_that_fill_the_room_leave_no_line_and_the_reply_whole(self):
        self.file("U1", 2, 2, 1)
        self.ask("我的設定")
        sent = self.transport.raw[-1]
        self.assertEqual([message["text"] for message in sent[:4]], ["a1", "a2", "b1", "b2"])
        self.assertIn("姓名：", sent[4]["text"])                               # the reply itself, last, with its buttons
        self.assertIn("quickReply", sent[4])
        self.assertEqual(self.mail("U1"), [[text("c1")]])                     # waits; no room for the 還有 line

    def test_two_replies_at_once_do_not_both_carry_the_same_groups(self):
        """Two events of one person handled at the same time (a double tap): the groups go with one reply only."""
        self.file("U1", 1)
        reply, nested = self.transport.reply, []

        def meanwhile(token, messages):
            if not nested:   # another worker answers U1 while this reply is on its way
                nested.append(True)
                self.bot._turn.user = "U1"
                self.bot._reply("other", [text("另一個回覆")])
            return reply(token, messages)
        self.transport.reply = meanwhile
        self.ask("說明")
        carried = [[message.get("text") for message in sent] for sent in self.transport.raw[-2:]]
        self.assertEqual(sum(texts.count("a1") for texts in carried), 1)
        self.assertEqual(self.mail("U1"), [])
        self.file("U1", 1)                                                   # read, then taken by another reply
        with mock.patch.object(self.store, "claim_mail", return_value=[]):
            self.assertEqual(self.ask("收件匣"), "📭 剛剛的回覆已經帶上收件匣的訊息了。")
        self.assertEqual(self.mail("U1"), [[text("a1")]])

    def test_a_reply_refused_for_a_bad_group_goes_out_without_it(self):
        """One filed message LINE will not take must not hold up every reply to the person: the reply goes again
        without it, and after MAIL_TRIES refusals the group is set aside."""
        self.bot._file("U1", [text("壞掉的")])
        reply = self.transport.reply

        def picky(token, messages):
            if any(message.get("text") == "壞掉的" for message in messages):
                raise RuntimeError('LINE API 400: {"message":"A message (messages[0]) is invalid"}')
            return reply(token, messages)
        self.transport.reply = picky
        self.assertIn("姓名：", self.ask("我的設定"))                          # answered all the same
        self.assertEqual(self.mail("U1"), [[text("壞掉的")]])                 # one try: back in the queue
        self.ask("我的設定")
        self.assertEqual(self.mail("U1"), [])                                # refused twice: set aside
        self.ask("我的設定")
        self.assertEqual(len(self.transport.raw[-1]), 1)
        self.bot._file("U1", [text("好的")])
        self.ask("說明")
        self.assertEqual(self.transport.raw[-1][0], text("好的"))             # later mail flows again

    def test_a_reply_that_failed_on_its_token_gives_the_groups_back(self):
        self.file("U1", 1)
        self.transport.used.add("spent")
        self.bot._turn.user = "U1"
        with self.assertRaises(RuntimeError):
            self.bot._reply("spent", [text("x")])
        self.assertEqual(self.mail("U1"), [[text("a1")]])                     # not refused for what it held: no try
        self.assertEqual(self.store._one("select attempts from mailbox")[0], 0)


class MailboxCommandTests(NoPushCase):
    def test_empty(self):
        self.assertEqual(self.ask("收件匣"), "📭 沒有新的訊息。")
        self.assertIn("辨識回報會放在這裡", self.ask("辨識回報", ADMIN))

    def test_every_word_opens_it(self):
        for words in MAILBOX:
            self.bot._file("U1", [text(words)])
            self.assertEqual(self.ask(words), words)

    def test_more_than_fits_then_the_rest(self):
        for number in range(7):
            self.bot._file("U1", [text(f"#{number}")])
        self.ask("收件匣")
        sent = self.transport.raw[-1]
        self.assertEqual([message["text"] for message in sent[:4]], ["#0", "#1", "#2", "#3"])
        self.assertEqual(sent[4]["text"], "📬 還有 3 則訊息，再傳「收件匣」繼續看。")
        self.assertEqual([item["action"]["text"] for item in sent[4]["quickReply"]["items"]], ["收件匣"])
        self.ask("收件匣")
        self.assertEqual([message["text"] for message in self.transport.raw[-1]], ["#4", "#5", "#6"])
        self.assertEqual(self.ask("收件匣"), "📭 沒有新的訊息。")

    def test_five_groups_exactly_fill_it_and_a_big_one_keeps_its_place(self):
        for number in range(5):
            self.bot._file("U1", [text(f"#{number}")])
        self.ask("我的訊息")
        self.assertEqual(len(self.transport.raw[-1]), 5)
        self.assertNotIn("quickReply", self.transport.raw[-1][-1])
        self.bot._file("U1", [text(str(n)) for n in range(5)])
        self.bot._file("U1", [text("後")])
        self.ask("收件匣")
        sent = self.transport.raw[-1]
        self.assertEqual([message["text"] for message in sent], ["0", "1", "2", "3", "4"])   # no room for the line
        self.assertEqual(sent[-1]["quickReply"]["items"][0]["action"]["label"], "📬 繼續看")

    def test_only_the_mailbox_rides_on_it(self):
        self.store.add_feedback("U1", "林小明", "明明", "意見一")
        self.bot._file(ADMIN, [text("回報")])
        self.ask("辨識回報", ADMIN)
        self.assertEqual(self.transport.raw[-1], [text("回報")])
        self.assertEqual(len(self.store.undelivered_feedback()), 1)          # with the next reply
        self.ask("我的設定", ADMIN)
        self.assertIn("意見一", self.transport.raw[-1][0]["text"])

    def test_without_a_reply_token_nothing_is_marked(self):
        self.bot._file("U1", [text("等著")])
        self.bot._show_mailbox(None, "U1")
        self.assertEqual(self.mail("U1"), [[text("等著")]])

    def test_a_refused_reply_marks_nothing(self):
        self.bot._file("U1", [text("等著")])
        with mock.patch.object(self.transport, "reply", side_effect=RuntimeError("LINE API 400")):
            with self.assertRaises(RuntimeError):
                self.ask("收件匣")
        self.assertEqual(self.mail("U1"), [[text("等著")]])


class HelpTests(NoPushCase):
    def test_the_advanced_help_and_the_administrators_settings_tell_where_things_are(self):
        self.ask("進階說明")
        words = "\n".join(flex_texts(self.transport.raw[-1][0]["contents"]))
        self.assertIn("收件匣", words)
        self.assertIn("辨識回報", words)
        self.assertIn("辨識回報：放在收件匣", self.ask("我的設定", ADMIN))
        self.assertNotIn("辨識回報", self.ask("我的設定"))
        plain = "\n".join(flex_texts(help_card(advanced=True)["contents"]))
        self.assertNotIn("收件匣", plain)                                    # with pushes, as before


class WithPushesTests(BotCase):
    settings = {"admins": (ADMIN,)}
    friends = {"U1", "U2", ADMIN}

    def test_the_mailbox_is_empty_and_the_reports_are_pushed(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.bot.handle(image_event("U2", "Cgroup"))
        self.assertEqual([to for to, _ in self.transport.pushes], [ADMIN])
        self.assertEqual(self.store.undelivered_mail(ADMIN), [])
        self.assertIn("會直接私訊給你", self.ask("辨識回報", ADMIN))
        self.assertNotIn("辨識回報：", self.ask("我的設定", ADMIN))


class GateTests(GateCase):
    settings = {**GateCase.settings, "push": False}

    def test_anyone_may_open_it_and_a_gate_card_carries_it_too(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.bot._file("U3", [text("⚠ 你在群組傳的班表無法辨識")])
            self.bot._file("U4", [text("再一則")])
        self.ask("收件匣", "U3")
        self.assertEqual(self.transport.raw[-1], [text("⚠ 你在群組傳的班表無法辨識")])
        self.assertNotIn(GATE_TILE, self.reply())
        self.ask("查看時段", "U4")
        notice, gate = self.transport.raw[-1]
        self.assertEqual(notice, text("再一則"))
        self.assertIn(GATE_TILE, flex_texts(gate["contents"]))


class StartUpTests(unittest.TestCase):
    def serve(self, env):
        import importlib.util
        if importlib.util.find_spec("fastapi") is None:
            self.skipTest("fastapi is not installed")
        from all_staff import demo, line_webhook
        made = []

        class Bot(line_webhook.RosterBot):
            def __init__(inner, analyzer, store, transport, settings):
                made.append(settings)
                super().__init__(analyzer, store, transport, settings)
        base = {"LINE_V2_CHANNEL_SECRET": "s", "LINE_V2_CHANNEL_ACCESS_TOKEN": "t", "LINE_V2_PUBLIC_BASE_URL": "",
                "LINE_V2_REGRID_LIMIT": "0", "LINE_V2_ADMIN_USER_IDS": "U" + "a" * 32}
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {**base, **env}), \
                mock.patch.object(demo, "DemoApp", lambda *a, **k: FakeAnalyzer()), \
                mock.patch.object(line_webhook, "RosterBot", Bot), \
                mock.patch.object(Bot, "sync_menus", lambda self, force=False: {}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            for name in ("LINE_V2_PUSH", "LINE_V2_NOTIFY"):
                if name not in env:
                    os.environ.pop(name, None)
            line_webhook.create_app(Path(folder), Path("m.pt"), roster=None, template=Path("t.json"),
                                    folder=Path(folder))
        return made[0], out.getvalue()

    def test_the_server_does_not_push_unless_told_to(self):
        settings, out = self.serve({})
        self.assertEqual((settings.push, settings.notify), (False, False))
        self.assertIn("推播：關（預設，不用每月的訊息額度）", out)
        self.assertIn("辨識回報放進管理員的收件匣", out)
        settings, out = self.serve({"LINE_V2_PUSH": "1"})
        self.assertEqual((settings.push, settings.notify), (True, False))
        self.assertIn("推播：開（LINE_V2_PUSH）", out)
        self.assertIn("確認後才通知", out)

    def test_notifications_need_pushes(self):
        settings, out = self.serve({"LINE_V2_NOTIFY": "1"})
        self.assertEqual((settings.push, settings.notify), (False, False))
        self.assertIn("LINE_V2_NOTIFY=1 要推播，不推播時不會生效", out)
        settings, _ = self.serve({"LINE_V2_NOTIFY": "1", "LINE_V2_PUSH": "yes"})
        self.assertEqual((settings.push, settings.notify), (True, True))


class SimulatorTests(unittest.TestCase):
    def test_the_simulator_does_not_push_either_unless_told_to(self):
        from all_staff.line_sim import Simulator
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            sim = Simulator(FakeAnalyzer(copy.deepcopy(RESULT)), Path(folder), "http://localhost:8779",
                            admins=("U_ming",), notify=True)

            def idle():
                deadline = time.monotonic() + 3
                while sim.world.pending and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(sim.world.pending, 0)

            def from_bot(chat):
                return [m for m in sim.world.state["messages"] if m["sender"] == "bot" and m["chat"] == chat]
            try:
                idle()
                self.assertFalse(sim.bot.settings.push)
                self.assertFalse(sim.bot.settings.notify)                    # --notify needs --push
                group = next(c for c in sim.world.state["chats"] if c["type"] == "group")
                before = len(from_bot("dm:U_ming"))                          # the welcome card
                sim.send_image(group["key"], "U_hua", b"jpeg"); idle()
                self.assertEqual(len(from_bot("dm:U_ming")), before)
                sim.send_text("dm:U_ming", "U_ming", "辨識回報"); idle()
                report, picture = from_bot("dm:U_ming")[before:]
                self.assertTrue(report["altText"].startswith("✅ 群組班表辨識成功"))
                self.assertEqual(picture["type"], "image")
                self.assertFalse(any(item["kind"] == "push" for item in sim.world.state["log"]))
            finally:
                sim.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
