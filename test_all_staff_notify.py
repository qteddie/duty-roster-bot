"""Automatic notifications are off unless ``Settings.notify`` (``LINE_V2_NOTIFY=1``) turns them on.

Every push counts against the LINE plan's monthly quota and replies do not, so by default a roster posted
in a group is only stored: people look theirs up with the menu. Also here: what the menu no longer has a
button for (registering a name) is offered where it is needed.
"""
import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from all_staff.bot import NOTIFY_OFF, Settings, help_card
from test_all_staff_bot import (RESULT, BotCase, FakeAnalyzer, HeldCase, flex_buttons, flex_texts, image_event,
                                postback_event, text_event)


def subscribed(store, user, name):
    """Someone who turned notifications on while that was still possible (the flag stays in the database)."""
    store.set_staff_name(user, name, None)
    store.set_notify(user, True)



def walk(node):
    """Every dict in a Flex message, depth first."""
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


class QuietByDefaultTests(BotCase):
    settings = {"notify": False}

    def test_the_switch_is_off_unless_it_is_turned_on(self):
        self.assertFalse(Settings().notify)
        self.assertFalse(self.bot.settings.notify)
        self.assertTrue(Settings(notify=True).notify)

    def test_a_group_roster_is_stored_and_nobody_is_pushed(self):
        subscribed(self.store, "U1", "林小明")
        subscribed(self.store, "U2", "陳小華")
        self.bot.handle(image_event("U3", "Cgroup"))
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.transport.replies, [])                       # and the group stays silent
        self.assertEqual(self.store.team_roster()["run_id"], "run-1")
        with self.at(date(2026, 10, 1)):
            self.assertIn("☀️ 今天 10/1（四）\n代號 72", self.ask("我的班表"))   # looked up, with a reply
            self.assertIn("林小明（72）　10/1（四）", self.ask("今天"))

    def test_an_update_of_the_day_is_stored_and_nobody_is_pushed(self):
        subscribed(self.store, "U1", "林小明")
        self.bot.handle(image_event("U3", "Cgroup"))
        update = copy.deepcopy(RESULT)
        update["run"]["id"] = "run-2"
        self.analyzer.data = update
        self.bot.handle(image_event("U3", "Cgroup"))
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.store.team_roster()["run_id"], "run-2")

    def test_legacy_code_bindings_are_left_alone(self):
        self.store.bind("U2", "72", "華華"); self.store.set_notify("U2", True)
        self.bot.handle(image_event("U1", "Cgroup"))
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.store.legacy_subscribers()[0]["code"], "72")  # asked once the switch is on again

    def test_turning_notifications_on_is_declined_and_nothing_is_recorded(self):
        self.register("U1", "林小明")
        reply = self.ask("通知 開")
        self.assertEqual(reply, NOTIFY_OFF)
        self.assertIn("我的班表", reply)
        self.assertEqual(self.store.user("U1")["notify"], 0)
        self.assertEqual(self.ask("通知 開", "U3"), NOTIFY_OFF)             # whether or not a name is registered
        self.assertIsNone(self.store.user("U3"))

    def test_who_had_notifications_on_is_kept_for_when_the_switch_returns(self):
        subscribed(self.store, "U1", "林小明")
        self.ask("通知 開")
        self.bot.handle(image_event("U3", "Cgroup"))
        self.assertEqual(self.store.user("U1")["notify"], 1)
        self.assertEqual(self.transport.pushes, [])
        self.bot.settings.notify = True                                    # LINE_V2_NOTIFY=1 and a restart
        self.bot.handle(image_event("U3", "Cgroup"))
        self.assertEqual([to for to, _ in self.transport.pushes], ["U1"])
        self.assertIn("林小明（72）", self.transport.pushes[0][1][0])

    def test_turning_them_off_still_works(self):
        subscribed(self.store, "U1", "林小明")
        self.assertIn("不再私訊你新班表", self.ask("通知 關"))
        self.assertEqual(self.store.user("U1")["notify"], 0)

    def test_settings_do_not_mention_notifications(self):
        reply = self.ask("我的設定", "U3")
        self.assertIn("姓名：尚未登記", reply)
        self.assertNotIn("通知", reply)
        self.register("U1", "林小明")
        reply = self.ask("我的設定")
        self.assertEqual(reply, "姓名：林小明")
        actions = [item["action"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertEqual([a["label"] for a in actions], ["改姓名"])

    def test_registering_and_unregistering_do_not_mention_notifications(self):
        reply = self.ask("設定 林小明")
        self.assertIn("已登記：林小明。之後點「我的班表」就能查看。", reply)
        self.assertNotIn("通知", reply)
        self.assertEqual(self.ask("解除"), "已取消姓名登記。")

    def test_the_help_does_not_explain_a_command_that_is_switched_off(self):
        reply = self.ask("用法說明")
        self.assertNotIn("通知", reply)
        self.assertIn("設定 你的姓名", reply)
        advanced = self.ask("進階說明")
        self.assertNotIn("通知 開", advanced)
        self.assertNotIn("通知 關", advanced)
        for phrase in ("設定", "我的設定", "查看登記的姓名", "解除", "取消姓名登記", "我是 代號", "日期 月/日"):
            self.assertIn(phrase, advanced)
        self.assertNotIn("{settings}", advanced)
        self.assertNotIn("通知 開", "\n".join(flex_texts(help_card()["contents"])))

    def test_with_the_switch_on_the_help_explains_it_again(self):
        self.bot.settings.notify = True
        self.assertIn("通知 開", self.ask("用法說明"))
        advanced = self.ask("進階說明")
        for phrase in ("通知 開", "通知 關", "查看姓名與通知狀態", "解除"):
            self.assertIn(phrase, advanced)
        self.assertNotIn("{settings}", advanced)

    def test_an_undated_group_photo_still_reminds_its_uploader(self):
        self.analyzer.data["run"]["roster_date"] = None
        subscribed(self.store, "U1", "林小明")
        self.bot.handle(image_event("U2", "Cgroup"))
        self.assertEqual([to for to, _ in self.transport.pushes], ["U2"])   # one push, to the uploader only
        self.assertIn("日期未辨識", self.transport.pushes[0][1][0])

    def test_a_group_photo_that_cannot_be_read_still_tells_its_uploader(self):
        self.bot.handle(image_event("U2", "Cgroup"))                        # U2 has posted rosters before

        class Broken(FakeAnalyzer):
            def analyze(self, payload, name):
                raise type("DemoError", (Exception,), {})("照片太模糊")
        self.bot.analyzer = Broken()
        self.bot.handle(image_event("U2", "Cgroup"))
        self.assertEqual([to for to, _ in self.transport.pushes], ["U2"])
        self.assertIn("無法辨識", self.transport.pushes[0][1][0])


class QuietWithAdministratorsTests(HeldCase):
    """The administrators are still asked for a date that could not be read; that is all anyone is pushed."""
    settings = {"admins": ("U9",), "notify": False}

    def setUp(self):
        super().setUp()
        self.store.set_notify("U1", True)       # switched on back when that was possible
        self.quiet()

    def test_the_request_is_about_the_date_and_dating_it_notifies_nobody(self):
        self.send("no-date", 0)
        self.assertEqual([to for to, _ in self.transport.pushes], ["U9"])
        card = self.transport.raw_pushes[0][1][-1]
        body = "\n".join(flex_texts(card["contents"]))
        self.assertIn("「我的班表」和日期都查不到這張", body)
        self.assertNotIn("通知", body + card["altText"])
        self.assertEqual([action["label"] for action in flex_buttons(card)][-1], "略過這張")
        self.quiet()
        reply = self.tap("run-no-date", "2026-10-05")
        self.assertIn("日期已設為 10/5（一）", reply)
        self.assertIn("現在大家可以用「我的班表」或日期查到這張", reply)
        self.assertNotIn("通知", reply)
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.store.roster_on("U1", date(2026, 10, 5))["run_id"], "run-no-date")

    def test_a_typed_date_asks_which_photo_without_speaking_of_notifying(self):
        self.send("no-date", 0)
        self.quiet()
        self.ask("日期 2026/10/5", "U9")
        confirm = self.transport.raw[-1][0]
        self.assertIn("要設成這個日期嗎？", flex_texts(confirm["contents"]))
        self.assertEqual([action["label"] for action in flex_buttons(confirm)], ["10/5（一）", "略過這張"])

    def test_a_dated_group_photo_is_stored_and_only_reported_to_the_administrator(self):
        self.send("first", 0)
        self.assertEqual([to for to, _ in self.transport.pushes], ["U9"])
        self.assertIn("✅ 群組班表辨識成功", self.transport.pushes[0][1][0])
        self.assertEqual(self.store.team_roster()["run_id"], "run-first")


class WhereTheMenuHasNoButtonTests(BotCase):
    """Registering a name is a one-off and has no menu button any more: it is offered wherever it is needed."""

    def fill(self, message):
        action = message["quickReply"]["items"][0]["action"]
        return action["label"], action.get("inputOption"), action.get("fillInText")

    def test_the_greeting_offers_it(self):
        """A newcomer gets one card about registering their name, not the help carousel."""
        event = text_event("U3", ""); event["type"] = "follow"; del event["message"]
        self.bot.handle(event)
        welcome, = self.transport.raw[-1]
        self.assertEqual(welcome["contents"]["type"], "bubble")
        self.assertIn("第一步：登記你的姓名", flex_texts(welcome["contents"]))
        self.assertNotIn("用法說明", welcome["altText"])
        self.assertEqual(self.fill(welcome), ("✏️ 設定姓名", "openKeyboard", "設定 "))
        tile = next(node for node in walk(welcome) if node.get("action", {}).get("data") == "guide|設定姓名")
        self.assertEqual((tile["action"]["inputOption"], tile["action"]["fillInText"]), ("openKeyboard", "設定 "))
        self.bot.handle(postback_event("U3", "guide|設定姓名"))
        self.assertIn("例如：設定 王小明", self.reply())   # nobody on the real 名冊 (蕭志明 is): the gate suggests no one

    def test_someone_who_comes_back_registered_is_not_sent_to_register_again(self):
        self.register("U1", "林小明")
        event = text_event("U1", ""); event["type"] = "follow"; del event["message"]
        self.bot.handle(event)
        greeting, = self.transport.raw[-1]
        self.assertIn("歡迎回來，林小明", greeting["text"])
        self.assertNotIn("設定姓名", greeting["text"])
        self.assertEqual(greeting["quickReply"]["items"][0]["action"]["text"], "我的班表")

    def test_every_reply_that_needs_a_name_offers_it(self):
        self.bot.handle(image_event("U2", "Cgroup"))
        for command in ("我的班表", "今天班表", "下個時段", "本月統計", "我是 26", "通知 開"):
            self.assertIn("設定 你的姓名", self.ask(command, "U3"), command)
            self.assertEqual(self.fill(self.transport.raw[-1][-1]), ("設定姓名", "openKeyboard", "設定 "), command)

    def test_the_button_only_opens_the_keyboard(self):
        count = len(self.transport.replies)
        self.bot.handle(postback_event("U3", "fill:設定"))
        self.assertEqual(len(self.transport.replies), count)

    def test_settings_offer_the_name_and_with_notifications_on_their_switch(self):
        self.ask("我的設定", "U3")
        self.assertEqual(self.fill(self.transport.raw[-1][-1]), ("設定姓名", "openKeyboard", "設定 "))
        self.register("U1", "林小明")
        for expected in ("通知 開", "通知 關"):
            self.ask("我的設定")
            actions = [item["action"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
            self.assertEqual([a["label"] for a in actions], ["改姓名", expected])
            self.assertEqual(actions[1]["text"], expected)
            self.ask(expected)


class NotifySwitchFromTheEnvironmentTests(unittest.TestCase):
    def test_only_a_clear_yes_turns_it_on(self):
        from all_staff.line_webhook import switched_on
        for value in ("1", "true", "TRUE", " yes ", "on", "'1'", "開"):
            self.assertTrue(switched_on(value), value)
        for value in (None, "", "0", "false", "off", "no", "2", "關", "enable"):
            self.assertFalse(switched_on(value), value)

    def serve(self, env):
        import os
        from unittest import mock
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
                "LINE_V2_REGRID_LIMIT": "0"}
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {**base, **env}), \
                mock.patch.object(demo, "DemoApp", lambda *a, **k: FakeAnalyzer()), \
                mock.patch.object(line_webhook, "RosterBot", Bot):
            for name in ("LINE_V2_NOTIFY", "LINE_V2_PUSH"):
                if name not in env:
                    os.environ.pop(name, None)
            line_webhook.create_app(Path(folder), Path("m.pt"), roster=None, template=Path("t.json"),
                                    folder=Path(folder))
        return made[0]

    def test_the_server_is_quiet_unless_the_variable_says_otherwise(self):
        self.assertFalse(self.serve({}).notify)
        self.assertFalse(self.serve({"LINE_V2_NOTIFY": "0", "LINE_V2_PUSH": "1"}).notify)
        self.assertTrue(self.serve({"LINE_V2_NOTIFY": "1", "LINE_V2_PUSH": "1"}).notify)
        self.assertFalse(self.serve({"LINE_V2_NOTIFY": "1"}).notify)   # notifications are pushes (LINE_V2_PUSH)


class MenuDrawingTests(unittest.TestCase):
    def setUp(self):
        from all_staff import rich_menu
        self.menu = rich_menu
        try:
            rich_menu._font(10)
        except SystemExit:
            self.skipTest("no Chinese font on this machine")

    def test_the_menu_draws_at_the_size_line_takes(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            path = self.menu.draw(Path(folder) / "menu.png")
            with Image.open(path) as image:
                self.assertEqual(image.size, (2500, 1686))
            self.assertLess(path.stat().st_size, 1_000_000)                 # LINE's limit for the image

    def test_a_label_that_does_not_fit_its_cell_stops_the_drawing(self):
        buttons = list(self.menu.BUTTONS)
        buttons[[label for label, _, _ in buttons].index("本月統計")] = ("本月工作時數統計", "太長了", ("message", "本月統計"))
        with tempfile.TemporaryDirectory() as folder, patch.object(self.menu, "BUTTONS", buttons):
            with self.assertRaises(ValueError) as raised:
                self.menu.draw(Path(folder) / "menu.png")
        self.assertIn("本月工作時數統計", str(raised.exception))

    def test_the_wide_button_has_room_for_its_words_and_says_when_not(self):
        buttons = list(self.menu.BUTTONS)
        buttons[0] = ("我的班表與下個小時還有明天", "現在、下個小時、明天，還有今天每一段勤務", ("message", "我的班表"))
        with tempfile.TemporaryDirectory() as folder, patch.object(self.menu, "BUTTONS", buttons):
            with self.assertRaises(ValueError) as raised:
                self.menu.draw(Path(folder) / "menu.png")
        self.assertIn("我的班表與下個小時還有明天", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
