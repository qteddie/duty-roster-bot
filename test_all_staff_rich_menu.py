"""圖文選單: everyone's menu, the 幹部 and administrator menus with their 「管理」 row, who is linked to which, and the
administrators' 姓名申請 and 意見列表, which left their menu for the 人員管理 card."""
import contextlib
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from all_staff import rich_menu
from all_staff.bot import (FEEDBACK_LIST, FIXED_LIST, MAILBOX, NAME_REQUESTS, STAFF_LIST, STAFF_MENU, STAFF_UI, RosterBot,
                           Store, flex_size, staff_menu_card)
from test_all_staff_bot import FakeAnalyzer, FakeTransport, flex_texts
from test_all_staff_duty_card import check_limits
from test_all_staff_officers import CAPTAIN, OfficerCase, walk

UNKNOWN = "我看不懂這句話"   # the bot's answer to words it has no command for
ADMIN, OFFICER, GONE = "U" + "a" * 32, "U" + "b" * 32, "U" + "c" * 32


def menu_ids(version=1):
    return {name: f"rm-{role or 'all'}-{version}" for role, name in rich_menu.NAMES.items()}


class FakeMenus(FakeTransport):
    """FakeTransport with LINE's rich menus: ``uploaded`` (name → id, empty before ``line-menu``), ``links`` (user →
    the id linked to them) and every menu call in ``calls``."""

    def __init__(self, friends=(), uploaded=True):
        super().__init__(friends)
        self.uploaded = menu_ids() if uploaded else {}
        self.links, self.calls = {}, []

    def menu_id(self, name, refresh=False):
        self.calls.append(("menu_id", name))
        return self.uploaded.get(name)

    def link_menu(self, user_id, name):
        menu = self.uploaded[name]
        self.calls.append(("link", user_id, menu))
        self.links[user_id] = menu
        return menu

    def unlink_menu(self, user_id):
        self.calls.append(("unlink", user_id))
        self.links.pop(user_id, None)

    def changes(self):
        return [call for call in self.calls if call[0] != "menu_id"]


def follow_event(user):
    return {"type": "follow", "replyToken": f"t-{time.time_ns()}", "source": {"type": "user", "userId": user}}


class MenuLayoutTests(unittest.TestCase):
    def rows(self, role):
        """The areas of ``role``'s menu grouped by row (by their y), each row left to right."""
        rows = {}
        for area in rich_menu.areas(role):
            rows.setdefault(area["bounds"]["y"], []).append(area)
        return [sorted(rows[y], key=lambda a: a["bounds"]["x"]) for y in sorted(rows)]

    def test_each_menu_fits_what_line_takes(self):
        expected = {None: ("roster-bot-v2", "選單", 10), "officer": ("roster-bot-v2-officer", "幹部選單", 13),
                    "admin": ("roster-bot-v2-admin", "管理員選單", 13)}
        for role in rich_menu.ROLES:
            menu = rich_menu.menu(role)
            self.assertEqual((menu["name"], menu["chatBarText"], len(menu["areas"])), expected[role], role)
            self.assertLessEqual(len(menu["areas"]), 20)                               # LINE: 20 areas at most
            self.assertLessEqual(len(menu["chatBarText"]), 14)
            size = menu["size"]
            self.assertEqual((size["width"], size["height"]), (2500, 1686))
            self.assertGreaterEqual(size["width"] / size["height"], 1.45)
            for area in menu["areas"]:
                b = area["bounds"]
                self.assertTrue(0 <= b["x"] and b["x"] + b["width"] <= 2500 and 0 <= b["y"]
                                and b["y"] + b["height"] <= 1686, (role, b))
                self.assertLessEqual(len(area["action"]["label"]), 20)                 # LINE: action labels ≤ 20
        self.assertEqual(len(set(rich_menu.NAMES.values())), 3)
        self.assertEqual(len({str(path) for path in rich_menu.IMAGES.values()}), 3)

    def test_the_rows_tile_everything_right_of_the_categories(self):
        for role, counts in ((None, [1, 3, 3, 3]), ("officer", [1, 3, 3, 3, 3]), ("admin", [1, 3, 3, 3, 3])):
            rows = self.rows(role)
            self.assertEqual([len(row) for row in rows], counts, role)
            bottom = 0
            for row in rows:
                y, height = row[0]["bounds"]["y"], row[0]["bounds"]["height"]
                self.assertEqual(y, bottom, role)                                      # no gap above, no overlap
                right = rich_menu.CATEGORY_WIDTH
                for area in row:
                    b = area["bounds"]
                    self.assertEqual((b["y"], b["height"]), (y, height))
                    self.assertEqual(b["x"], right, role)
                    right = b["x"] + b["width"]
                self.assertEqual(right, rich_menu.WIDTH)
                bottom = y + height
            self.assertEqual(bottom, rich_menu.HEIGHT)

    def test_everyones_menu_has_no_help_button(self):
        rows = self.rows(None)
        self.assertEqual([row[0]["bounds"]["height"] for row in rows], [421, 422, 421, 422])
        self.assertEqual([a["action"]["label"] for a in rich_menu.areas()],
                         ["我的班表", "查看時段", "查看工作", "查詢人員", "勤務人員", "休假人員", "人流預測",
                          "本月統計", "我的設定", "意見回饋"])
        self.assertNotIn("用法說明", json.dumps(rich_menu.menu(), ensure_ascii=False))   # 「說明」 is typed now
        self.assertEqual([a["bounds"]["x"] for a in rows[3]], [a["bounds"]["x"] for a in rows[1]])   # 更多: three
        self.assertEqual(set(rich_menu.ROW_FONTS[1:]), {(124, 54)})                    # all in the same type
        self.assertEqual(rich_menu.IMAGE.name, "line_rich_menu.png")
        self.assertEqual(rich_menu.NAME, "roster-bot-v2")
        for role in ("officer", "admin"):   # the role menus begin with the same buttons, doing the same
            self.assertEqual([a["action"] for a in rich_menu.areas(role)[:10]], [a["action"] for a in rich_menu.areas()])

    def test_the_management_row_sends_the_bots_commands(self):
        officer = [a["action"] for a in rich_menu.areas("officer")[10:]]
        admin = [a["action"] for a in rich_menu.areas("admin")[10:]]
        self.assertEqual([(a["type"], a["label"], a["text"]) for a in officer],
                         [("message", "人員管理", "人員管理"), ("message", "人員名冊", "人員名冊"),
                          ("message", "固定代號", "固定代號")])
        self.assertEqual([(a["type"], a["label"], a["text"]) for a in admin],   # 姓名申請, 意見列表: in 人員管理
                         [("message", "人員管理", "人員管理"), ("message", "人員名冊", "人員名冊"),
                          ("message", "辨識回報", "辨識回報")])
        self.assertIn("人員管理", STAFF_MENU)
        self.assertIn("人員名冊", STAFF_LIST)
        self.assertIn("固定代號", FIXED_LIST)
        self.assertIn("辨識回報", MAILBOX)

    def test_the_management_row_lines_up_with_the_rows_above(self):
        for role in ("officer", "admin"):
            rows = self.rows(role)
            self.assertEqual([a["bounds"]["x"] for a in rows[4]], [a["bounds"]["x"] for a in rows[3]], role)
            self.assertEqual(rich_menu._layout(role)[3][4], rich_menu.ROW_FONTS[3], role)   # and the type of 更多's

    def test_there_are_three_menus_and_no_other(self):
        with self.assertRaises(ValueError):
            rich_menu.areas("captain")


class MenuCommandTests(OfficerCase):
    """Every message a menu sends is a command the bot answers, for a user who has that menu."""

    def messages(self, role):
        return [a["action"]["text"] for a in rich_menu.areas(role) if a["action"]["type"] == "message"]

    def test_everyones_buttons(self):
        for words in self.messages(None):
            self.assertNotIn(UNKNOWN, self.say(words, "U1"), words)
        fills = [a["action"] for a in rich_menu.areas() if a["action"]["type"] == "postback"]
        self.assertEqual([(a["data"], a["fillInText"], a["inputOption"]) for a in fills],
                         [("fill:查", "查 ", "openKeyboard"), ("fill:意見", "意見 ", "openKeyboard")])

    def test_the_officer_buttons(self):
        for words in self.messages("officer"):
            reply = self.say(words)
            self.assertNotIn(UNKNOWN, reply, words)
            self.assertNotIn("只有幹部可以使用", reply, words)

    def test_the_administrator_buttons(self):
        self.bot.settings.admins = ("U2",)
        for words in self.messages("admin"):
            reply = self.say(words, "U2")
            self.assertNotIn(UNKNOWN, reply, words)
            self.assertNotIn("只有幹部可以使用", reply, words)
        self.assertIn("沒有新的訊息", self.say("辨識回報", "U2"))                      # their 收件匣

    def test_the_help_is_still_there_when_typed(self):
        for words in ("用法說明", "說明"):
            self.say(words, "U1")
            self.assertTrue(self.transport.raw[-1][-1]["altText"].startswith("📋 用法說明"), words)


class StaffMenuTileTests(OfficerCase):
    """姓名申請 and 意見列表 left the administrators' menu for two tiles of their 人員管理 card; 幹部 get neither."""
    EVERYONES = ["人員名冊", "固定代號", "新增人員", "刪除人員", "幹部名單", "新增幹部"]

    def setUp(self):
        super().setUp()
        self.bot.settings.admins = ("U2",)
        self.bot.settings.push = False   # as the server runs by default: new feedback waits for the next reply

    def card(self, user):
        self.say("人員管理", user)
        card = self.transport.raw[-1][-1]
        check_limits(self, card)
        return card

    @staticmethod
    def rows(card):
        """The titles of the card's tiles, row by row."""
        return [[flex_texts(tile)[1] for tile in row["contents"] if tile.get("action")]
                for row in card["contents"]["body"]["contents"] if row.get("layout") == "horizontal"]

    @staticmethod
    def tile(card, title):
        return next(node for node in walk(card["contents"]) if node.get("action") and node.get("contents")
                    and flex_texts(node)[1:2] == [title])

    def wait(self):
        """A name request (林小明 wants to be 陳小華) and a message for the administrators, neither seen yet."""
        self.say("設定 陳小華", "U1")
        self.say("意見 時段卡片的字太小了", "U1")

    def test_an_administrator_gets_the_two_tiles_in_a_row_of_their_own(self):
        card = self.card("U2")
        self.assertEqual(self.rows(card), [self.EVERYONES[:2], self.EVERYONES[2:4], self.EVERYONES[4:],
                                           ["姓名申請", "意見列表"], ["綁定清單"]])
        self.assertEqual([self.tile(card, title)["action"] for title in ("姓名申請", "意見列表")],
                         [{"type": "message", "label": "姓名申請", "text": "姓名申請"},
                          {"type": "message", "label": "意見列表", "text": "意見列表"}])
        self.assertEqual(flex_texts(self.tile(card, "姓名申請")), ["📮", "姓名申請", "審核改名與登記"])
        self.assertEqual(flex_texts(self.tile(card, "意見列表")), ["💬", "意見列表", "大家的意見回饋"])
        self.assertEqual(flex_texts(card["contents"]["header"])[1], "管理員專用｜點下面的方塊開始")

    def test_the_tiles_say_what_waits(self):
        self.wait()
        card = self.card("U2")
        requests, feedback = self.tile(card, "姓名申請"), self.tile(card, "意見列表")
        self.assertEqual(flex_texts(requests)[2], "1 筆等你確認")
        self.assertEqual(requests["backgroundColor"], STAFF_UI["alert"])     # an amber of its own, ringed:
        self.assertNotIn(STAFF_UI["alert"], STAFF_UI["tints"])               # no other tile has it
        self.assertEqual((requests["borderWidth"], requests["borderColor"]), ("2px", STAFF_UI["alert_edge"]))
        self.assertEqual(requests["contents"][2]["weight"], "bold")
        tints = [tile["backgroundColor"] for row in card["contents"]["body"]["contents"]
                 if row.get("layout") == "horizontal" for tile in row["contents"] if tile.get("action")]
        self.assertEqual(tints.count(STAFF_UI["alert"]), 1)
        self.assertEqual(flex_texts(feedback)[2], "1 則新的意見")
        self.assertTrue(card["altText"].endswith("｜姓名申請 1 筆等你確認"))
        self.assertNotIn("姓名申請在等確認", "\n".join(flex_texts(card["contents"])))   # the tile says it: no tip
        self.assertTrue(self.transport.raw[-1][0]["text"].startswith("📮 新的意見回饋"))   # shown with this reply…
        feedback = self.tile(self.card("U2"), "意見列表")
        self.assertEqual(flex_texts(feedback)[2], "大家的意見回饋")              # …so no longer new the next time
        self.assertEqual(flex_texts(self.tile(self.card("U2"), "姓名申請"))[2], "1 筆等你確認")   # until answered

    def test_nothing_waiting_leaves_the_tile_plain(self):
        requests = self.tile(self.card("U2"), "姓名申請")
        self.assertEqual(flex_texts(requests)[2], "審核改名與登記")
        self.assertNotIn("borderWidth", requests)
        self.assertIn(requests["backgroundColor"], STAFF_UI["tints"])

    def test_an_administrators_own_request_is_not_on_their_tile(self):
        """Only requests they can answer: with two administrators, A's own request waits on B's tile, not on A's."""
        self.bot.settings.admins = ("U2", "U3")
        self.say("設定 陳小華", "U2")
        self.say("設定 蕭志明", "U2")   # U3 can approve it: pending
        own = self.tile(self.card("U2"), "姓名申請")
        self.assertEqual(flex_texts(own)[2], "審核改名與登記")
        self.assertNotIn("borderWidth", own)
        self.assertEqual(flex_texts(self.tile(self.card("U3"), "姓名申請"))[2], "1 筆等你確認")

    def test_the_help_lists_the_feedback_list(self):
        from all_staff.bot import FEEDBACK_SHOWN, MORE_HELP
        self.assertIn("意見列表：管理員查看最近的意見回饋", MORE_HELP)
        self.assertIn(f"最近 {FEEDBACK_SHOWN} 則意見回饋", self.say("人員管理指令", "U2"))

    def test_an_officer_gets_neither(self):
        self.wait()
        card = self.card(CAPTAIN)
        self.assertEqual(self.rows(card), [self.EVERYONES[:2], self.EVERYONES[2:4], self.EVERYONES[4:], ["綁定清單"]])
        shown = "\n".join(flex_texts(card["contents"])) + card["altText"]
        for words in ("姓名申請", "意見"):
            self.assertNotIn(words, shown)
        self.assertEqual(flex_texts(card["contents"]["header"])[1], "幹部專用｜點下面的方塊開始")
        self.assertNotIn("📮 姓名申請", [item["action"]["label"] for item in card["quickReply"]["items"]])

    def test_tapping_the_tiles_gives_their_answers(self):
        card = self.card("U2")
        answers = {"姓名申請": "目前沒有等待確認的姓名申請", "意見列表": "目前沒有任何意見回饋"}
        for title, answer in answers.items():
            self.assertIn(answer, self.say(self.tile(card, title)["action"]["text"], "U2"), title)
        self.wait()
        card = self.card("U2")
        self.say(self.tile(card, "姓名申請")["action"]["text"], "U2")
        listing = self.transport.raw[-1][-1]
        self.assertIn("陳小華", flex_texts(listing["contents"]))
        self.assertTrue(any(node.get("action", {}).get("data", "").startswith("rename|") for node in walk(listing)))
        self.say(self.tile(card, "意見列表")["action"]["text"], "U2")
        listing = self.transport.raw[-1][-1]["text"]
        self.assertTrue(listing.startswith("📮 最近的意見回饋"))
        self.assertIn("時段卡片的字太小了", listing)
        for words in (NAME_REQUESTS[0], FEEDBACK_LIST[0]):   # what the tiles send is still the administrators' alone
            reply = self.say(words, CAPTAIN)
            self.assertNotIn("時段卡片的字太小了", reply, words)
            self.assertNotIn("陳小華", reply, words)

    def test_the_card_stays_within_lines_limits(self):
        for admin in (False, True):
            card = staff_menu_card(names=999, fixed=34, registered=999, unlisted=99, officers=99, admin=admin,
                                   requests=999, feedback=9999)
            check_limits(self, card)
            self.assertLess(flex_size(card["contents"]), 12_000)
            self.assertEqual(self.rows(card)[-1], ["綁定清單"])
            self.assertEqual(len(self.rows(card)), 5 if admin else 4)
            self.assertEqual("999 筆等你確認" in flex_texts(card["contents"]), admin)   # an officer's: no count at all


class InstallTests(unittest.TestCase):
    def install(self, relink=None):
        self.calls = calls = []   # kept on the test, so a test whose install raises still sees what was sent

        def call(token, method, url, body=None, content_type=None):
            calls.append((method, url.replace(rich_menu.API, "").replace(rich_menu.DATA_API, "DATA"), body))
            if url.endswith("/richmenu/list"):
                return json.dumps({"richmenus": [
                    {"richMenuId": "old-all", "name": "roster-bot-v2"},
                    {"richMenuId": "old-officer", "name": "roster-bot-v2-officer"},
                    {"richMenuId": "someone-elses", "name": "another-bot"}]}).encode()
            if method == "POST" and url.endswith("/richmenu"):
                return json.dumps({"richMenuId": "new-" + json.loads(body)["name"].replace("roster-bot-v2", "all")}).encode()
            return b""

        def relinked(ids):
            calls.append(("relink", ids, None))
            if relink:
                relink(ids)
        with patch.object(rich_menu, "_call", call):
            return rich_menu.install("token", relink=relinked), calls

    def test_three_menus_are_made_and_the_old_ones_go_last(self):
        ids, calls = self.install()
        self.assertEqual(ids, {None: "new-all", "officer": "new-all-officer", "admin": "new-all-admin"})
        made = [json.loads(body) for method, url, body in calls if (method, url) == ("POST", "/richmenu")]
        self.assertEqual([m["name"] for m in made], ["roster-bot-v2", "roster-bot-v2-officer", "roster-bot-v2-admin"])
        self.assertEqual([len(m["areas"]) for m in made], [10, 13, 13])
        uploads = {url: body for method, url, body in calls if method != "relink" and url.startswith("DATA")}
        for role, menu_id in ids.items():
            self.assertEqual(uploads[f"DATA/richmenu/{menu_id}/content"], rich_menu.IMAGES[role].read_bytes())
        steps = [(method, url) for method, url, _ in calls if method != "relink"]
        order = [call[0] if call[0] == "relink" else f"{call[0]} {call[1]}" for call in calls]
        default, relink = order.index("POST /user/all/richmenu/new-all"), order.index("relink")
        deletes = [i for i, step in enumerate(order) if step.startswith("DELETE")]
        self.assertLess(max(i for i, step in enumerate(order) if step.startswith("POST")), relink)
        self.assertLess(default, relink)                       # everyone's default first, then the 幹部 and 管理員
        self.assertLess(relink, min(deletes))                  # links moved before any old menu (and its links) goes
        self.assertEqual(deletes, list(range(len(order) - 2, len(order))))   # the old copies are the last calls
        self.assertEqual([step for step in steps if step[0] == "DELETE"],
                         [("DELETE", "/richmenu/old-all"), ("DELETE", "/richmenu/old-officer")])
        self.assertEqual(calls[order.index("relink")][1], ids)

    def test_old_menus_stay_when_the_links_could_not_be_moved(self):
        def broken(ids):
            raise RuntimeError("LINE is down")
        with self.assertRaises(RuntimeError):
            self.install(broken)
        steps = [f"{method} {url}" for method, url, _ in self.calls if method != "relink"]
        self.assertIn("POST /user/all/richmenu/new-all", steps)   # it got as far as the relink
        self.assertEqual([step for step in steps if step.startswith("DELETE")], [])   # and deleted nothing


class DrawingTests(unittest.TestCase):
    def test_the_committed_pictures_fit_line(self):
        from PIL import Image
        for role, path in rich_menu.IMAGES.items():
            self.assertTrue(path.is_file(), path)
            with Image.open(path) as image:
                self.assertEqual(image.size, (2500, 1686), role)
            self.assertLessEqual(path.stat().st_size, 1_000_000, role)

    def font(self):
        try:
            return rich_menu._font(10)
        except SystemExit:
            self.skipTest("no Chinese font on this machine")

    def test_every_menu_draws_and_matches_the_committed_picture(self):
        from PIL import Image, ImageChops
        same_font = str(getattr(self.font(), "path", "")).endswith("NotoSansCJK-Bold.ttc")   # what drew the assets
        with tempfile.TemporaryDirectory() as folder:
            for role in rich_menu.ROLES:
                path = rich_menu.draw(Path(folder) / f"{role}.png", role=role)
                with Image.open(path) as drawn:
                    self.assertEqual(drawn.size, (2500, 1686))
                    if same_font:   # another build of the font may move a few glyph pixels; a moved word moves thousands
                        with Image.open(rich_menu.IMAGES[role]) as committed:
                            difference = ImageChops.difference(drawn.convert("RGB"), committed.convert("RGB"))
                            changed = difference.convert("L").point(lambda v: 255 if v > 64 else 0).histogram()[255]
                            self.assertLess(changed / (2500 * 1686), .002, role)
                self.assertLess(path.stat().st_size, 1_000_000)

    def test_a_management_label_that_does_not_fit_stops_the_drawing(self):
        self.font()
        buttons = {**rich_menu.ROLE_BUTTONS, "officer": [("人員名冊與固定代號管理", "太長了", ("message", "人員管理")),
                                                         *rich_menu.ROLE_BUTTONS["officer"][1:]]}
        with tempfile.TemporaryDirectory() as folder, patch.object(rich_menu, "ROLE_BUTTONS", buttons):
            with self.assertRaises(ValueError) as raised:
                rich_menu.draw(Path(folder) / "menu.png", role="officer")
        self.assertIn("人員名冊與固定代號管理", str(raised.exception))

    def test_the_rows_shrink_more_gently_than_their_height(self):
        self.assertEqual(rich_menu._scale(4), 1)
        self.assertGreater(rich_menu._scale(5), .8)
        self.assertLess(rich_menu._scale(5), 1)


class MenuCase(OfficerCase):
    """幹部 (學姊, 隊長) with a transport that has LINE's rich menus."""

    def setUp(self):
        super().setUp()
        self.transport = self.bot.transport = FakeMenus(self.friends)
        self.out = io.StringIO()
        quiet = contextlib.redirect_stdout(self.out)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def linked(self, user):
        return self.transport.links.get(user)


class MenuSyncTests(MenuCase):
    def test_an_administrator_gets_the_administrator_menu(self):
        self.bot.settings.admins = ("U2",)
        self.assertEqual(self.bot.menu_role("U2"), "admin")
        self.assertEqual(self.bot.sync_menu("U2"), "linked")
        self.assertEqual(self.linked("U2"), "rm-admin-1")
        self.assertEqual(self.store.menu_link("U2")["role"], "admin")

    def test_an_officer_gets_the_officer_menu(self):
        self.assertEqual(self.bot.menu_role(CAPTAIN), "officer")
        self.assertEqual(self.bot.sync_menu(CAPTAIN), "linked")
        self.assertEqual(self.linked(CAPTAIN), "rm-officer-1")
        self.assertIsNone(self.bot.menu_role("U1"))
        self.assertIsNone(self.bot.sync_menu("U1"))
        self.assertNotIn("U1", self.transport.links)

    def test_an_administrator_who_is_also_an_officer_gets_the_administrator_menu(self):
        self.bot.settings.admins = (CAPTAIN,)
        self.assertEqual(self.bot.sync_menus(), {"admin": 1, "officer": 1, "default": 0, "failed": 0})
        self.assertEqual(self.linked(CAPTAIN), "rm-admin-1")
        self.assertEqual(sum(call[1] == CAPTAIN for call in self.transport.changes()), 1)   # once, not twice

    def test_a_new_officer_gets_the_menu_and_is_told_so(self):
        reply = self.say("新增幹部 林小明")
        self.assertIn("林小明 現在是幹部了", reply)
        self.assertIn("他重新打開聊天室後，選單最下面會多一排「管理」。", reply)
        self.assertEqual(self.linked("U1"), "rm-officer-1")

    def test_a_removed_officer_goes_back_to_everyones_menu(self):
        self.say("新增幹部 林小明")
        reply = self.say("移除幹部 林小明")
        self.assertIn("林小明 已經不是幹部了", reply)
        self.assertIn("他重新打開聊天室後，選單的「管理」那排會收起來。", reply)
        self.assertNotIn("U1", self.transport.links)
        self.assertIsNone(self.store.menu_link("U1"))
        self.assertEqual(self.transport.changes()[-1], ("unlink", "U1"))

    def test_an_administrator_removed_as_officer_keeps_the_administrator_menu(self):
        self.bot.settings.admins = (CAPTAIN,)
        self.bot.sync_menus()
        reply = self.say("移除幹部 隊長")
        self.assertIn("不過你是管理員", reply)
        self.assertNotIn("收起來", reply)
        self.assertEqual(self.linked(CAPTAIN), "rm-admin-1")

    def test_an_administrator_dropped_from_the_settings_goes_back_at_the_next_sync(self):
        self.bot.settings.admins = ("U2",)
        self.bot.sync_menus()
        self.assertEqual(self.linked("U2"), "rm-admin-1")
        self.bot.settings.admins = ()
        self.assertEqual(self.bot.sync_menus(), {"admin": 0, "officer": 2, "default": 1, "failed": 0})
        self.assertNotIn("U2", self.transport.links)
        self.assertIsNone(self.store.menu_link("U2"))

    def test_menus_uploaded_again_are_linked_again(self):
        self.bot.settings.admins = ("U2",)
        self.bot.sync_menus()
        self.transport.uploaded = menu_ids(2)          # line-menu: new menus, new ids
        self.assertEqual(self.bot.sync_menus(), {"admin": 1, "officer": 2, "default": 0, "failed": 0})
        self.assertEqual((self.linked("U2"), self.linked(CAPTAIN)), ("rm-admin-2", "rm-officer-2"))
        self.assertEqual(self.store.menu_link(CAPTAIN)["menu_id"], "rm-officer-2")

    def test_nothing_is_linked_again_while_nothing_changed(self):
        self.bot.settings.admins = ("U2",)
        self.bot.sync_menus()
        before = len(self.transport.changes())
        self.assertEqual(self.bot.sync_menus(), {"admin": 1, "officer": 2, "default": 0, "failed": 0})
        self.assertEqual(self.bot.sync_menu("U2"), "same")
        self.assertEqual(len(self.transport.changes()), before)

    def test_my_settings_says_which_menu(self):
        self.bot.sync_menus()
        self.assertIn("選單：幹部版（最下面一排「管理」）", self.say("我的設定"))
        self.assertNotIn("選單：", self.say("我的設定", "U1"))
        self.bot.settings.admins = ("U2",)
        self.assertNotIn("選單：", self.say("我的設定", "U2"))       # not linked yet: nothing is claimed
        self.bot.sync_menus()
        self.assertIn("選單：管理員版（最下面一排「管理」）", self.say("我的設定", "U2"))
        self.transport.uploaded = menu_ids(2)          # uploaded again, U2 not moved yet: that menu is gone
        self.assertNotIn("選單：", self.say("我的設定", "U2"))
        self.bot.sync_menus()
        self.assertIn("選單：管理員版", self.say("我的設定", "U2"))
        self.bot.settings.admins = ()                  # the record still says admin, the role is gone
        self.assertNotIn("選單：", self.say("我的設定", "U2"))

    def test_an_officer_who_adds_the_bot_again_gets_the_menu_again(self):
        self.bot.sync_menus()
        self.transport.links.clear()                    # LINE may have forgotten it while the bot was blocked
        self.bot.handle(follow_event(CAPTAIN))
        self.assertEqual(self.linked(CAPTAIN), "rm-officer-1")
        self.assertIn("歡迎回來", self.reply())          # the welcome is still sent
        self.bot.handle(follow_event("U3"))             # an ordinary newcomer: no menu call at all
        self.assertFalse([call for call in self.transport.changes() if call[1] == "U3"])

    def test_the_welcome_goes_out_before_the_menu_is_linked(self):
        self.bot.sync_menus()
        order, reply, link = [], self.transport.reply, self.transport.link_menu
        self.transport.reply = lambda token, messages: (order.append("reply"), reply(token, messages))[1]
        self.transport.link_menu = lambda user, name: (order.append("link"), link(user, name))[1]
        self.bot.handle(follow_event(CAPTAIN))
        self.assertEqual(order, ["reply", "link"])

    def test_an_administrator_who_is_also_an_officer_keeps_the_officer_menu_when_dropped(self):
        self.bot.settings.admins = (CAPTAIN,)
        self.bot.sync_menus()
        self.assertEqual(self.linked(CAPTAIN), "rm-admin-1")
        self.bot.settings.admins = ()
        self.bot.sync_menus()
        self.assertEqual(self.linked(CAPTAIN), "rm-officer-1")
        self.assertEqual(self.store.menu_link(CAPTAIN)["role"], "officer")

    def test_a_slow_line_does_not_hold_the_reply(self):
        go = threading.Event()
        link = self.transport.link_menu

        def slow(user_id, name):
            go.wait(5)
            return link(user_id, name)
        self.transport.link_menu = slow
        with patch.object(RosterBot, "MENU_WAIT", .05):
            started = time.monotonic()
            reply = self.say("新增幹部 林小明")
            self.assertLess(time.monotonic() - started, 2)
        self.assertIn("現在是幹部了", reply)
        self.assertNotIn("多一排", reply)               # not linked yet when the reply went: nothing is claimed
        go.set()
        deadline = time.monotonic() + 3
        while not self.store.menu_link("U1") and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.linked("U1"), "rm-officer-1")


class MenuFailureTests(MenuCase):
    def test_menus_gone_from_line_unlink_whoever_had_them(self):
        self.bot.sync_menus()
        self.assertEqual(self.store.menu_link(CAPTAIN)["role"], "officer")
        self.transport.uploaded = {}                   # the role menus were deleted from LINE
        self.assertEqual(self.bot.sync_menus(), {"admin": 0, "officer": 0, "default": 2, "failed": 0})
        self.assertIsNone(self.store.menu_link(CAPTAIN))
        self.assertIn(("unlink", CAPTAIN), self.transport.changes())
        self.assertNotIn("選單：", self.say("我的設定"))

    def test_someone_without_a_menu_does_not_wait_for_a_slow_line(self):
        self.bot.sync_menus()
        done = []
        with self.bot._menu_lock:                      # LINE is slow: another change holds the lock
            worker = threading.Thread(target=lambda: done.append(self.bot.sync_menu("U3")), daemon=True)
            worker.start()
            worker.join(1)
            self.assertEqual(done, [None])             # an ordinary user needs nothing and did not wait
            self.bot.handle(follow_event("U3"))        # nor does their follow
        self.assertTrue(self.reply())

    def test_menus_not_uploaded_link_nobody_and_say_so_once(self):
        self.transport.uploaded = {}
        self.bot.settings.admins = ("U2",)
        self.assertEqual(self.bot.sync_menus(), {"admin": 0, "officer": 0, "default": 0, "failed": 0})   # nothing failed
        self.bot.sync_menus()
        reply = self.say("新增幹部 林小明")
        self.assertIn("現在是幹部了", reply)
        self.assertNotIn("多一排", reply)
        self.assertEqual(self.transport.links, {})
        self.assertEqual(self.out.getvalue().count("管理員／幹部選單還沒上傳，請執行 python -m all_staff.cli line-menu"), 1)

    def test_a_transport_without_menus_works_as_before(self):
        self.transport = self.bot.transport = FakeTransport(self.friends)
        self.assertEqual(self.bot.sync_menus(), {"admin": 0, "officer": 0, "default": 0, "failed": 0})
        self.assertIsNone(self.bot.sync_menu(CAPTAIN))
        reply = self.say("新增幹部 林小明")
        self.assertIn("現在是幹部了", reply)
        self.assertNotIn("選單", reply)
        self.assertIn("已經不是幹部了", self.say("移除幹部 林小明"))
        self.bot.handle(follow_event(CAPTAIN))
        self.assertIn("歡迎回來", self.reply())

    def test_a_link_that_fails_does_not_stop_the_reply(self):
        def broken(user_id, name):
            raise RuntimeError("LINE API 500: try again later")
        self.transport.link_menu = broken
        reply = self.say("新增幹部 林小明")
        self.assertIn("現在是幹部了", reply)
        self.assertNotIn("多一排", reply)
        self.assertIsNone(self.store.menu_link("U1"))
        self.assertIn("圖文選單切換失敗（U1）：RuntimeError", self.out.getvalue())
        self.assertEqual(self.bot.sync_menus()["failed"], 3)    # 學姊, 隊長 and the new 林小明
        self.bot.handle(follow_event(CAPTAIN))
        self.assertIn("歡迎回來", self.reply())

    def test_an_unlink_that_fails_keeps_the_record_for_the_next_try(self):
        self.say("新增幹部 林小明")

        def broken(user_id):
            raise RuntimeError("LINE API 500")
        self.transport.unlink_menu = broken
        reply = self.say("移除幹部 林小明")
        self.assertIn("已經不是幹部了", reply)
        self.assertNotIn("收起來", reply)
        self.assertEqual(self.store.menu_link("U1")["role"], "officer")   # sync_menus tries it again
        self.assertNotIn("選單：", self.say("我的設定", "U1"))             # the record no longer fits: nothing is claimed
        del self.transport.unlink_menu
        self.assertEqual(self.bot.sync_menus()["default"], 1)
        self.assertIsNone(self.store.menu_link("U1"))


class LineTransportMenuTests(unittest.TestCase):
    def transport(self, listings, refuse=(), refuse_users=()):
        """A LineTransport whose LINE lists ``listings`` (name → id, or (name, id) pairs for two copies of a name) in
        turn (the last one again after that), refuses links to the ids in ``refuse`` and any link of ``refuse_users``."""
        from all_staff.line_webhook import LineError, LineTransport
        calls, listings = [], list(listings)

        class Fake(LineTransport):
            def _call(inner, method, url, payload=None):
                calls.append((method, url.replace("https://api.line.me/v2/bot", "")))
                if url.endswith("/richmenu/list"):
                    listed = listings.pop(0) if len(listings) > 1 else listings[0]
                    pairs = listed.items() if isinstance(listed, dict) else listed
                    return json.dumps({"richmenus": [{"richMenuId": i, "name": n} for n, i in pairs]}).encode()
                if method == "POST" and url.rsplit("/", 1)[-1] in refuse:
                    raise LineError(400, '{"message":"richmenu not found"}')
                if method == "POST" and any(f"/user/{user}/" in url for user in refuse_users):
                    raise LineError(400, '{"message":"the user has not added the bot"}')
                return b"{}"
        return Fake("token"), calls

    def test_ids_are_looked_up_once_and_again_when_a_name_is_missing(self):
        line, calls = self.transport([{"roster-bot-v2-admin": "a1"}, {"roster-bot-v2-admin": "a1",
                                                                      "roster-bot-v2-officer": "o1"}])
        self.assertEqual(line.menu_id("roster-bot-v2-admin"), "a1")
        self.assertEqual(line.menu_id("roster-bot-v2-admin"), "a1")
        self.assertEqual(calls.count(("GET", "/richmenu/list")), 1)
        self.assertEqual(line.menu_id("roster-bot-v2-officer"), "o1")
        self.assertEqual(calls.count(("GET", "/richmenu/list")), 2)
        self.assertEqual(line.menu_id("roster-bot-v2-admin", refresh=True), "a1")
        self.assertEqual(calls.count(("GET", "/richmenu/list")), 3)

    def test_menus_just_made_are_taken_as_handed_over(self):
        line, calls = self.transport([{}])
        line.remember_menus({"roster-bot-v2-admin": "new"})
        self.assertEqual(line.link_menu(ADMIN, "roster-bot-v2-admin"), "new")
        self.assertEqual(calls, [("POST", f"/user/{ADMIN}/richmenu/new")])

    def test_a_link_to_a_menu_that_is_gone_is_tried_once_with_the_new_id(self):
        line, calls = self.transport([{"roster-bot-v2-officer": "old"}, {"roster-bot-v2-officer": "new"}],
                                     refuse={"old"})
        self.assertEqual(line.link_menu(OFFICER, "roster-bot-v2-officer"), "new")
        self.assertEqual(calls, [("GET", "/richmenu/list"), ("POST", f"/user/{OFFICER}/richmenu/old"),
                                 ("GET", "/richmenu/list"), ("POST", f"/user/{OFFICER}/richmenu/new")])

    def test_menus_handed_over_survive_a_refresh_while_line_lists_both_copies(self):
        from all_staff.line_webhook import LineError
        both = [("roster-bot-v2-officer", "new"), ("roster-bot-v2-officer", "old")]   # the old copy listed last
        line, calls = self.transport([both], refuse_users={OFFICER})
        line.remember_menus({"roster-bot-v2-officer": "new"})   # line-menu just made it
        with self.assertRaises(LineError):             # a user's refusal is raised, not retried on the old copy
            line.link_menu(OFFICER, "roster-bot-v2-officer")
        self.assertNotIn(("POST", f"/user/{OFFICER}/richmenu/old"), calls)
        self.assertEqual(line.menu_id("roster-bot-v2-officer", refresh=True), "new")   # still the new one
        self.assertEqual(line.link_menu(ADMIN, "roster-bot-v2-officer"), "new")

    def test_a_refused_link_with_no_newer_menu_is_an_error(self):
        from all_staff.line_webhook import LineError
        line, _ = self.transport([{"roster-bot-v2-officer": "old"}], refuse={"old"})
        with self.assertRaises(LineError):
            line.link_menu(OFFICER, "roster-bot-v2-officer")
        with self.assertRaises(LookupError):
            line.link_menu(OFFICER, "roster-bot-v2-admin")

    def test_unlinking_someone_with_no_menu_of_their_own_is_fine(self):
        from all_staff.line_webhook import LineError, LineTransport
        answers = []

        class Fake(LineTransport):
            def _call(inner, method, url, payload=None):
                raise answers.pop(0)
        line = Fake("token")
        answers.append(LineError(404, "no rich menu"))
        line.unlink_menu(OFFICER)
        answers.append(LineError(400, "invalid user"))
        with self.assertRaises(LineError):
            line.unlink_menu(OFFICER)


class LineMenuCommandTests(unittest.TestCase):
    """``python -m all_staff.cli line-menu``: upload, then move the links, then delete the old menus."""

    def run_command(self, folder, *extra):
        from all_staff import cli, line_webhook
        self.events = events = []   # kept on the test, so a command that stops still shows what it sent

        def menu_call(token, method, url, body=None, content_type=None):
            events.append((method, url.replace(rich_menu.API, "")))
            if url.endswith("/richmenu/list"):
                return json.dumps({"richmenus": [{"richMenuId": "old-" + (role or "all"), "name": name}
                                                 for role, name in rich_menu.NAMES.items()]}).encode()
            if method == "POST" and url.endswith("/richmenu"):
                return json.dumps({"richMenuId": "new-" + json.loads(body)["name"]}).encode()
            return b""

        def line_call(transport, method, url, payload=None):
            events.append((method, url.replace(rich_menu.API, "")))
            self.assertFalse(url.endswith("/richmenu/list"), "the new ids are handed over, not looked up")
            return b"{}"
        out = io.StringIO()
        env = {"LINE_V2_CHANNEL_ACCESS_TOKEN": "token", "LINE_V2_ADMIN_USER_IDS": ADMIN}
        with patch.dict(os.environ, env), patch.object(rich_menu, "_call", menu_call), \
                patch.object(line_webhook.LineTransport, "_call", line_call), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["line-menu", "--folder", str(folder), *extra]), 0)
        return events, out.getvalue()

    def test_officers_and_administrators_are_moved_to_the_new_menus_before_the_old_ones_go(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "bot.sqlite3")
            store.add_officer(OFFICER, "隊長", "seed")
            store.set_menu_link(OFFICER, "officer", "old-officer")
            store.set_menu_link(GONE, "officer", "old-officer")       # no longer a 幹部
            store.db.close()
            events, out = self.run_command(folder)
            store = Store(Path(folder) / "bot.sqlite3")
            self.assertEqual(store.menu_link(OFFICER)["menu_id"], "new-roster-bot-v2-officer")
            self.assertEqual(store.menu_link(ADMIN)["menu_id"], "new-roster-bot-v2-admin")
            self.assertIsNone(store.menu_link(GONE))
            store.db.close()
        links = [i for i, (method, url) in enumerate(events) if url.startswith("/user/U")]
        deletes = [i for i, (method, url) in enumerate(events) if method == "DELETE" and url.startswith("/richmenu/")]
        self.assertEqual(sorted(events[i] for i in links),
                         sorted([("POST", f"/user/{ADMIN}/richmenu/new-roster-bot-v2-admin"),
                                 ("POST", f"/user/{OFFICER}/richmenu/new-roster-bot-v2-officer"),
                                 ("DELETE", f"/user/{GONE}/richmenu")]))
        self.assertLess(max(links), min(deletes))
        self.assertEqual(len(deletes), 3)
        self.assertIn("已換上：管理員選單 1 位、幹部選單 1 位；1 位改回一般選單", out)

    def test_without_the_database_nothing_is_uploaded(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(SystemExit) as stop:
                self.run_command(Path(folder) / "elsewhere")
            self.assertFalse((Path(folder) / "elsewhere" / "bot.sqlite3").exists())
        self.assertIn("找不到", str(stop.exception.code))
        self.assertIn("--folder", str(stop.exception.code))
        self.assertEqual(self.events, [])              # the old menus, and the 幹部's links to them, stay

    def test_a_dry_run_draws_the_three_pictures_and_uploads_nothing(self):
        from all_staff import cli
        drawn = []
        with patch.object(rich_menu, "draw", lambda path=None, role=None: drawn.append(role) or Path("x.png")), \
                patch.object(rich_menu, "_call", side_effect=AssertionError("nothing is uploaded")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["line-menu", "--dry-run"]), 0)
        self.assertEqual(drawn, [None, "officer", "admin"])


class StartUpTests(unittest.TestCase):
    def test_the_server_links_the_administrators_menu_when_it_starts(self):
        try:
            import fastapi  # noqa: F401  (create_app needs it)
        except ImportError:
            self.skipTest("fastapi unavailable")
        from all_staff import demo, line_webhook
        calls = []

        class Transport(line_webhook.LineTransport):
            def _call(self, method, url, payload=None):
                calls.append((method, url.replace(line_webhook.API, "")))
                if url.endswith("/richmenu/list"):
                    return json.dumps({"richmenus": [{"richMenuId": f"rm-{role or 'all'}", "name": name}
                                                     for role, name in rich_menu.NAMES.items()]}).encode()
                return b"{}"
        env = {"LINE_V2_CHANNEL_SECRET": "s", "LINE_V2_CHANNEL_ACCESS_TOKEN": "t", "LINE_V2_PUBLIC_BASE_URL": "",
               "LINE_V2_ADMIN_USER_IDS": ADMIN, "LINE_V2_REGRID_LIMIT": "0"}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, env), \
                patch.object(demo, "DemoApp", lambda *a, **k: FakeAnalyzer()), \
                patch.object(line_webhook, "LineTransport", Transport), contextlib.redirect_stdout(io.StringIO()) as out:
            line_webhook.create_app(Path(folder), Path("m.pt"), roster=None, template=Path("t.json"), folder=Path(folder))
            deadline = time.monotonic() + 3
            while ("POST", f"/user/{ADMIN}/richmenu/rm-admin") not in calls and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn(("POST", f"/user/{ADMIN}/richmenu/rm-admin"), calls)
            while "圖文選單：管理員版 1 位" not in out.getvalue() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn("圖文選單：管理員版 1 位", out.getvalue())

    def test_the_simulator_links_the_menus_again_when_it_restarts(self):
        from all_staff.line_sim import Simulator
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            seen = []
            for admins in (("U_ming",), ("U_ming",), ("U_hua",)):
                sim = Simulator(FakeAnalyzer(), Path(folder), "http://localhost:8779", admins=admins)
                deadline = time.monotonic() + 3
                while sim.world.pending and time.monotonic() < deadline:
                    time.sleep(.01)
                sim.pool.shutdown(wait=True)
                seen.append(dict(sim.world.state["menus"]))
                sim.world.save()
        self.assertEqual(seen, [{"U_ming": "admin"}, {"U_ming": "admin"}, {"U_hua": "admin"}])


class SimulatorMenuTests(unittest.TestCase):
    def test_the_simulator_shows_the_management_row_of_whoever_has_it(self):
        from all_staff.line_sim import Simulator, menu_rows
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            sim = Simulator(FakeAnalyzer(), Path(folder), "http://localhost:8779", admins=("U_ming",))

            def idle():
                deadline = time.monotonic() + 3
                while sim.world.pending and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(sim.world.pending, 0)
            try:
                idle()
                self.assertEqual(sim.world.state["menus"], {"U_ming": "admin"})
                sim.send_text("dm:U_hua", "U_hua", "設定 陳志明"); idle()
                sim.send_text("dm:U_ming", "U_ming", "新增幹部 陳志明"); idle()
                self.assertEqual(sim.world.state["menus"], {"U_ming": "admin", "U_hua": "officer"})
                self.assertIn("多一排「管理」", sim.world.state["messages"][-1]["text"])
                sim.send_text("dm:U_ming", "U_ming", "移除幹部 陳志明"); idle()
                self.assertEqual(sim.world.state["menus"], {"U_ming": "admin"})
                self.assertTrue(any(item["kind"] == "menu" for item in sim.world.state["log"]))
            finally:
                sim.pool.shutdown(wait=True)
        rows = menu_rows()
        self.assertEqual([b["label"] for b in rows["officer"]["buttons"]], ["人員管理", "人員名冊", "固定代號"])
        self.assertEqual([b["text"] for b in rows["admin"]["buttons"]], ["人員管理", "人員名冊", "辨識回報"])
        self.assertEqual(rows["admin"]["category"], "管理")

    def test_the_page_draws_everyones_rows_as_the_menu_has_them(self):
        """The four rows everybody has are written into the simulator's page; they must not drift from BUTTONS."""
        from all_staff.line_sim import PAGE
        page = PAGE.read_text(encoding="utf-8")
        groups = re.findall(r'<div class="quick-group[^"]*"><span class="quick-category">([^<]+)</span>'
                            r'<div class="quick-actions">(.*?)</div></div>', page)
        self.assertEqual([category for category, _ in groups], [label for label, _ in rich_menu.GROUPS])
        drawn = [[(label, ("message" if kind == "q" else "fill", value))
                  for kind, value, label in re.findall(r'<button data-(q|fill)="([^"]*)">([^<]*)</button>', buttons)]
                 for _, buttons in groups]
        expected, start = [], 0
        for count in rich_menu.ROW_COUNTS:
            expected.append([(label, action) for label, _, action in rich_menu.BUTTONS[start:start + count]])
            start += count
        self.assertEqual(drawn, expected)
        self.assertNotIn("用法說明", page)


if __name__ == "__main__":
    unittest.main()
