"""人員管理: 幹部 keep the staff list, the fixed codes and the 幹部 themselves from LINE."""
import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from all_staff.bot import Store
from all_staff.name_table import load_directory, load_fixed_codes
from all_staff.staff_book import StaffBook, live_copy, live_files, load_officers, split_names
from test_all_staff_bot import RESULT, BotCase, flex_texts, postback_event, text_event

TODAY = date(2026, 10, 8)
CAPTAIN = "U376d91504654a03cd4509329b09000fe"
SENIOR = "U86ba21d7c8007518b0d8a7ce5ddbcde7"
STAFF = "# 名冊\n林小明\n陳小華\n\n# 中文固定代號\n蕭志明\n"


def walk(node):
    """Every dict in a Flex message, depth first."""
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


class StaffBookTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.names, self.fixed = root / "staff_names.txt", root / "fixed_staff_codes.json"
        self.names.write_text(STAFF, encoding="utf-8")
        self.fixed.write_text(json.dumps({"己": "蕭志明"}), encoding="utf-8")
        self.book = StaffBook(self.names, self.fixed)

    def tearDown(self):
        self.folder.cleanup()

    def test_new_names_go_at_the_end_with_a_note_and_the_rest_stays_as_it_was(self):
        self.assertEqual(self.book.add(["王大同", "林小明", "王大同"], "10/8 隊長 新增"), (["王大同"], ["林小明"]))
        text = self.names.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(STAFF.rstrip("\n")))
        self.assertTrue(text.endswith("王大同  # 10/8 隊長 新增\n"))
        self.assertEqual(load_directory(self.names), ["林小明", "陳小華", "蕭志明", "王大同"])

    def test_removing_someone_drops_their_line_and_their_fixed_code(self):
        self.assertEqual(self.book.remove(["蕭志明", "沒這人"]), (["蕭志明"], ["沒這人"], {"己": "蕭志明"}))
        self.assertEqual(load_directory(self.names), ["林小明", "陳小華"])
        self.assertIn("# 中文固定代號", self.names.read_text(encoding="utf-8"))   # comments stay
        self.assertEqual(load_fixed_codes(self.fixed), {})

    def test_a_fixed_code_moves_with_its_person_and_puts_them_on_the_list(self):
        self.assertEqual(self.book.set_code("丁", "林小明"),
                         {"before": None, "moved": None, "listed": False, "removed": None})
        self.assertEqual(self.book.set_code("丁", "王大同"),   # 林小明, replaced on a 役男 code, leaves the list
                         {"before": "林小明", "moved": None, "listed": True, "removed": "林小明"})
        self.assertNotIn("林小明", load_directory(self.names))
        self.assertEqual(self.book.set_code("甲", "王大同"),
                         {"before": None, "moved": "丁", "listed": False, "removed": None})
        self.assertEqual(self.book.fixed(), {"甲": "王大同", "己": "蕭志明"})
        self.assertEqual(list(json.loads(self.fixed.read_text(encoding="utf-8"))), ["甲", "己"])   # 甲乙丙… order
        self.assertIn("王大同", load_directory(self.names))
        self.assertEqual(self.book.clear_code("甲"), "王大同")
        self.assertIsNone(self.book.clear_code("甲"))
        self.assertIn("王大同", load_directory(self.names))           # still on the list

    def test_the_live_copy_is_made_once_and_never_overwritten(self):
        target = Path(self.folder.name) / "data" / "staff_names.txt"
        live_copy(self.names, target)
        self.assertEqual(target.read_text(encoding="utf-8"), STAFF)
        target.write_text("王大同\n", encoding="utf-8")
        live_copy(self.names, target)
        self.assertEqual(target.read_text(encoding="utf-8"), "王大同\n")
        self.assertFalse(live_copy(Path(self.folder.name) / "missing.txt", Path(self.folder.name) / "x.txt").exists())
        names, fixed = live_files(Path(self.folder.name), Path(self.folder.name) / "data")
        self.assertEqual((names.name, fixed.name), ("staff_names.txt", "fixed_staff_codes.json"))
        self.assertEqual(load_fixed_codes(fixed), {"己": "蕭志明"})

    def test_names_are_split_on_spaces_and_punctuation(self):
        self.assertEqual(split_names("王小明 陳大華、李四，王小明;林五"), ["王小明", "陳大華", "李四", "林五"])
        self.assertEqual(split_names(""), [])

    def test_the_first_officers_file(self):
        self.assertEqual(load_officers(Path(__file__).resolve().parent / "configs" / "officers.json"),
                         [(SENIOR, "學姊"), (CAPTAIN, "隊長")])
        bad = Path(self.folder.name) / "officers.json"
        bad.write_text(json.dumps({"officers": [{"user_id": "@line-id"}]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_officers(bad)
        self.assertEqual(load_officers(None), [])


class OfficerStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "bot.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def test_the_first_officers_are_imported_once(self):
        self.assertEqual(self.store.seed_officers([]), 0)               # no list yet: the next one still counts
        self.assertEqual(self.store.seed_officers([(SENIOR, "學姊"), (CAPTAIN, "隊長")]), 2)
        self.store.remove_officer(SENIOR)
        self.store.db.close()
        again = Store(self.path)                       # a restart does not bring a removed officer back
        self.assertEqual(again.seed_officers([(SENIOR, "學姊"), (CAPTAIN, "隊長")]), 0)
        self.assertEqual([(o["user_id"], o["title"]) for o in again.officers()], [(CAPTAIN, "隊長")])

    def test_officers_carry_the_name_they_registered(self):
        self.store.add_officer(CAPTAIN, "隊長", "seed")
        self.store.set_staff_name(CAPTAIN, "林大明", "大明")
        self.assertTrue(self.store.is_officer(CAPTAIN))
        self.assertFalse(self.store.is_officer(SENIOR))
        self.assertFalse(self.store.is_officer(None))
        officer, = self.store.officers()
        self.assertEqual((officer["staff_name"], officer["name"]), ("林大明", "大明"))
        self.assertFalse(self.store.add_officer(CAPTAIN, "大隊長"))
        self.assertEqual(self.store.officers()[0]["title"], "大隊長")


class OfficerCase(BotCase):
    friends = {"U1", "U2", "U3", CAPTAIN}
    settings = {"notify": False}

    def setUp(self):
        super().setUp()
        root = Path(self.folder.name)
        self.names, self.fixed = root / "staff_names.txt", root / "fixed_staff_codes.json"
        self.names.write_text(STAFF, encoding="utf-8")
        self.fixed.write_text(json.dumps({"己": "蕭志明"}), encoding="utf-8")
        self.bot.settings.staff_names, self.bot.settings.fixed_staff_codes = self.names, self.fixed
        self.store.seed_officers([(SENIOR, "學姊"), (CAPTAIN, "隊長")])
        self.register("U1", "林小明")
        self.store.set_staff_name(CAPTAIN, "林大明", None)   # registered before the 名冊 was the gate: not on it

    def say(self, value, user=CAPTAIN):
        with patch("all_staff.bot.today", return_value=TODAY):
            return self.ask(value, user)

    def directory(self):
        return load_directory(self.names)

    def names_on_the_roster(self):
        return self.bot.names_for({"run_id": "run-1"}, copy.deepcopy(RESULT))

    def confirm(self):
        """Tap the ✅ quick reply of the last answer (giving a code to a name the bot did not know yet)."""
        action = next(item["action"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]
                      if item["action"]["label"].startswith("✅"))
        with patch("all_staff.bot.today", return_value=TODAY):
            self.bot.handle(postback_event(CAPTAIN, action["data"]))
        return self.reply()


class PermissionTests(OfficerCase):
    COMMANDS = ("人員管理", "人員名冊", "人員管理指令", "固定代號", "役男代號", "幹部名單",
                "新增人員 王大同", "刪除人員 陳小華", "固定代號 丁 王大同", "役男 丁 王大同", "役男 王大同 丁",
                "役男丁王大同", "役男 丁、王大同", "役男代號 丁 王大同", "代號 A 王大同", "英文代號 C 王大同",
                "役男 己 取消", "代號 A 取消", "新增幹部 林小明", "移除幹部 學姊")

    def test_nobody_else_can_and_nothing_changes(self):
        for command in self.COMMANDS:
            self.assertIn("只有幹部可以使用", self.say(command, "U1"), command)
        for data in ("guide|新增人員", "guide|刪除人員", "guide|新增幹部", "guide|移除幹部", "fixed|己", "fixed|A"):
            self.bot.handle(postback_event("U1", data))
            self.assertIn("只有幹部可以使用", self.reply(), data)
        self.assertEqual(self.names.read_text(encoding="utf-8"), STAFF)
        self.assertEqual(load_fixed_codes(self.fixed), {"己": "蕭志明"})
        self.assertEqual(len(self.store.officers()), 2)

    def test_everyday_words_still_get_the_everyday_answer(self):
        self.assertIn("進階說明", self.say("所有指令", "U1"))            # the rich menu and the help both say 所有指令
        self.assertIn("進階說明", self.say("所有指令"))                  # for a 幹部 too; theirs is 人員管理指令
        for words in ("役男", "英文代號", "代號表", "役男名單", "役男 你好", "役男辛苦了", "代號 72"):
            self.assertIn("看不懂", self.say(words, "U1"), words)

    def test_administrators_can_too(self):
        self.bot.settings.admins = ("U2",)
        self.assertIn("已加入名冊：王大同", self.say("新增人員 王大同", "U2"))

    def test_in_a_group_the_bot_stays_quiet(self):
        self.bot.handle(text_event(CAPTAIN, "新增人員 王大同", group="Cteam"))
        self.assertEqual(self.transport.replies[-1][1][0][:4], "已登記：")   # the last reply is still the registration
        self.assertNotIn("王大同", self.directory())

    def test_my_settings_says_who_is_an_officer(self):
        mine = self.say("我的設定")
        self.assertIn("幹部：是", mine)
        labels = [item["action"]["label"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertIn("人員管理", labels)
        self.assertNotIn("幹部", self.say("我的設定", "U1"))
        labels = [item["action"]["label"] for item in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertNotIn("人員管理", labels)


class StaffCommandTests(OfficerCase):
    def test_the_menu_counts_everything_and_offers_the_commands(self):
        reply = self.say("人員管理")
        card = self.transport.raw[-1][-1]
        self.assertEqual(card["altText"], "人員管理｜名冊 3 位、固定代號 1/34、已登記 1 位、幹部 2 位")
        self.assertIn("3\n名冊\n1/34\n代號\n1\n已登記\n2\n幹部", reply)   # only people on the list
        self.assertIn("有 1 位自己登記、但不在名冊", reply)       # 林大明 registered but is not on the list
        tiles = [node for row in card["contents"]["body"]["contents"][:3] for node in row["contents"]]
        self.assertEqual([flex_texts(tile)[1] for tile in tiles],
                         ["人員名冊", "固定代號", "新增人員", "刪除人員", "幹部名單", "新增幹部"])
        self.assertEqual([t["action"]["type"] for t in tiles], ["message", "message", "postback", "postback",
                                                               "message", "postback"])
        typing = [t["action"] for t in tiles if t["action"]["type"] == "postback"]
        self.assertEqual([(a["data"], a["inputOption"], a["fillInText"]) for a in typing],
                         [("guide|新增人員", "openKeyboard", "新增人員 "), ("guide|刪除人員", "openKeyboard", "刪除人員 "),
                          ("guide|新增幹部", "openKeyboard", "新增幹部 ")])
        self.assertNotIn('"button"', json.dumps(card))         # tiles, not LINE buttons whose labels get cut
        fills = [i["action"].get("fillInText") for i in card["quickReply"]["items"]]
        self.assertEqual(fills, ["新增人員 ", "刪除人員 ", "役男 ", "新增幹部 "])
        link = [n for n in walk(card) if n.get("action", {}).get("label") == "人員管理指令"]
        self.assertEqual(flex_texts(link[0]), ["📖 人員管理指令"])
        help_text = self.say("人員管理指令")
        for word in ("役男 丁 王小明", "代號 A 王小明", "人員名冊", "幹部名單", "役男 王小明"):
            self.assertIn(word, help_text)

    def test_the_list_marks_fixed_codes_and_registered_names(self):
        self.say("人員名冊")
        card = self.transport.raw[-1][-1]
        self.assertEqual(card["contents"]["type"], "bubble")
        texts = flex_texts(card["contents"])
        self.assertIn("共 3 位｜✓ 已在機器人登記 1 位", texts)
        self.assertEqual(texts[texts.index("👥 沒有固定代號") + 1:texts.index("👥 沒有固定代號") + 4],
                         ["2 位", "林小明✓", "陳小華"])
        start = texts.index("🎖️ 役男（中文代號）")
        codes = texts[start + 2:start + 42]                     # all twenty, blank where nobody has one
        self.assertEqual(codes[::2], list("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"))
        self.assertEqual(codes[1::2][5], "蕭志明")
        self.assertEqual(set(codes[1::2]) - {"蕭志明"}, {"　"})
        start = texts.index("🔤 英文代號")
        self.assertEqual(texts[start + 1], "0 / 14")
        self.assertEqual(texts[start + 2:start + 30][::2], list("ABCDEFGHIJKLMN"))
        self.assertIn("林大明✓", texts[texts.index("🙋 自己登記、不在名冊"):])
        add_all = [n for n in walk(card) if n.get("action", {}).get("label") == "全部加入名冊"]
        self.assertEqual(add_all[0]["action"]["text"], "新增人員 林大明")
        self.say(add_all[0]["action"]["text"])
        self.assertIn("林大明", self.directory())

    def test_a_new_name_is_matched_on_rosters_at_once(self):
        self.assertNotIn("26", self.names_on_the_roster())
        reply = self.say("新增人員 王大同 陳小華 abc")
        self.assertIn("已加入名冊：王大同（現在共 4 位）", reply)
        self.assertIn("已經在名冊裡：陳小華", reply)
        self.assertIn("沒有處理：abc", reply)
        self.assertEqual(self.directory()[-1], "王大同")
        self.assertIn("# 2026-10-08 隊長 林大明 新增", self.names.read_text(encoding="utf-8"))
        self.assertEqual(self.names_on_the_roster()["26"], "王大同")

    def test_removing_someone_also_drops_their_fixed_code(self):
        reply = self.say("刪除人員 蕭志明 沒這人")
        self.assertIn("已從名冊移除：蕭志明（現在共 2 位）", reply)
        self.assertIn("也清空了固定代號：己（蕭志明）", reply)
        self.assertIn("名冊裡沒有：沒這人", reply)
        self.assertEqual(load_fixed_codes(self.fixed), {})

    def test_removing_someone_who_registered_says_their_registration_stays(self):
        self.assertIn("仍有 LINE 帳號登記這個姓名", self.say("刪除人員 林小明"))
        self.assertNotIn("林小明", self.directory())

    def test_the_commands_alone_explain_themselves(self):
        self.assertIn("例如：新增人員 王小明 陳大華", self.say("新增人員"))
        self.assertIn("例如：刪除人員 王小明", self.say("刪除人員"))
        for command in ("固定代號", "役男代號", "英文代號", "代號表"):
            self.assertIn("固定代號 1/34｜己蕭志明", self.say(command), command)

    def test_fixed_codes_are_set_moved_and_cleared(self):
        reply = self.say("役男 丁 王大同")                              # not on the list yet: one tap to confirm
        self.assertIn("名冊裡還沒有「王大同」", reply)
        self.assertEqual(load_fixed_codes(self.fixed), {"己": "蕭志明"})
        reply = self.confirm()
        self.assertIn("役男代號「丁」：王大同", reply)
        self.assertIn("也把 王大同 加進了名冊", reply)
        self.assertIn("從 10/8 的班表開始套用", reply)
        self.assertIn("目前是 王大同", self.say("役男 丁"))
        self.assertIn("原本是 王大同：役男換人，已經把他移出名冊", self.say("固定代號丁林小明"))
        self.assertNotIn("王大同", self.directory())
        self.assertIn("林小明 原本的代號「丁」已清空", self.say("役男 林小明 甲"))   # name first works too
        self.assertEqual(load_fixed_codes(self.fixed), {"甲": "林小明", "己": "蕭志明"})
        self.assertIn("清空了役男代號「甲」", self.say("役男 甲 清空"))
        self.assertIn("本來就空著", self.say("役男 甲 取消"))
        self.assertIn("固定代號請這樣打", self.say("役男 7 林小明"))
        self.assertIn("固定代號請這樣打", self.say("役男 丁 王 大同"))
        self.assertIn("2–5 個中文字", self.say("役男 丁 Tom"))
        self.assertEqual(load_fixed_codes(self.fixed), {"己": "蕭志明"})

    def test_a_question_or_a_verb_after_a_code_saves_nothing(self):
        self.assertIn("「A」目前空著", self.say("代號A是誰"))
        self.assertIn("「己」目前是 蕭志明", self.say("役男 己 是誰"))
        self.assertIn("名冊裡還沒有「換人」", self.say("役男 己 換人"))      # asked, not done
        self.assertIn("名冊裡還沒有「大同學」", self.say("役男丁大同學"))
        self.assertIn("如果「丁大同學」是一個人的姓名", self.reply())
        self.assertEqual(load_fixed_codes(self.fixed), {"己": "蕭志明"})
        self.assertNotIn("換人", self.directory())
        self.assertIn("看不懂", self.say("代號A是誰", "U1"))                 # a question is no 幹部 command
        self.assertIn("看不懂", self.say("役男 丁", "U1"))

    def test_english_codes_are_fixed_too(self):
        self.say("新增人員 王大同 王大明 王小華 王二明")
        reply = self.say("代號 A 王大同")
        self.assertIn("英文代號「A」：王大同", reply)
        for command, code in (("代號 b 陳小華", "B"), ("英文代號 C 林小明", "C"), ("代號N王大明", "N"),
                              ("固定代號 王小華 D", "D"), ("代號 王二明 Ｅ", "E")):
            self.assertIn(f"英文代號「{code}」", self.say(command), command)
        self.assertEqual(load_fixed_codes(self.fixed)["B"], "陳小華")
        self.assertIn("英文代號「A」目前是 王大同", self.say("代號 a"))
        self.assertIn("清空了英文代號「A」", self.say("代號 A 取消"))
        self.assertIn("固定代號請這樣打", self.say("代號 O 王大同"))           # A–N only
        dated = self.bot.names_for({"run_id": "run-1", "roster_date": "2026-10-01"}, copy.deepcopy(RESULT))
        self.assertIsNone(dated.get("B"))                                    # the roster of 10/1 predates it

    def test_a_bare_code_word_without_a_fixed_code_is_left_alone(self):
        self.assertIn("看不懂", self.say("代號 72 王大同"))
        self.assertIn("看不懂", self.say("代號 72", "U1"))                    # nobody is told it is for 幹部

    def test_a_name_that_starts_with_a_code_is_a_name(self):
        self.say("新增人員 王志明 王大同")
        reply = self.say("役男 王志明")
        self.assertIn("要給 王志明 哪個代號", reply)
        self.assertNotIn("請打「役男 丁 芬玲」", reply)                 # she is on the list: no cut-off reading
        self.assertEqual(load_fixed_codes(self.fixed), {"己": "蕭志明"})
        self.assertIn("役男代號「辛」：王志明", self.say("役男 辛 王志明"))
        self.assertIn("役男代號「丁」：王大同", self.say("役男丁王大同"))     # 丁王大同 is nobody; 王大同 is somebody

    def test_a_short_name_after_a_code_character_is_asked_about_not_saved(self):
        self.say("新增人員 簡志明 王大同")
        self.say("役男 丁 簡志明")
        for typed in ("役男 丁小明", "役男丁小明", "役男 申小華"):
            reply = self.say(typed)
            self.assertIn("名冊裡還沒有", reply, typed)
            self.assertIn("請打「役男 ", reply)                   # the other reading is offered, not taken
        self.assertEqual(load_fixed_codes(self.fixed), {"丁": "簡志明", "己": "蕭志明"})
        self.assertNotIn("小明", self.directory())
        self.assertIn("役男代號「丁」：王大同", self.say("役男丁王大同"))     # a known name after the code still works
        self.assertIn("役男代號「辛」：林小明", self.say("役男辛林小明"))

    def test_punctuation_separates_code_and_name(self):
        self.say("新增人員 王大同")
        for typed, code in (("役男 丁、王大同", "丁"), ("役男 戊，王大同", "戊"), ("代號 王大同、B", "B"),
                            ("役男：庚 王大同", "庚")):
            self.assertIn(f"「{code}」：王大同", self.say(typed), typed)

    def test_a_name_alone_gets_one_tap_cells(self):
        self.say("新增人員 王大同")
        self.assertEqual([i["action"]["text"] for i in self.transport.raw[-1][-1]["quickReply"]["items"][:1]],
                         ["役男 王大同"])                                # 新增人員 offers the next step
        self.say("役男 王大同")
        note, card = self.transport.raw[-1]
        self.assertIn("要給 王大同 哪個代號？點一個空白的格子就好", note["text"])
        taps = {n["action"].get("data") or "" for n in walk(card) if n.get("action", {}).get("type") == "postback"}
        self.assertIn("setcode|甲|王大同|empty", taps)                  # blank: one tap gives the code
        self.assertIn("setcode|A|王大同|empty", taps)
        self.assertIn("fixed|己", taps)                                 # filled: only the keyboard, nobody replaced
        self.assertNotIn("setcode|己|王大同|empty", taps)
        free = [i["action"] for i in card["quickReply"]["items"]][:2]
        self.assertEqual([a["displayText"] for a in free], ["役男 甲 王大同", "役男 乙 王大同"])
        with patch("all_staff.bot.today", return_value=TODAY):
            self.bot.handle(postback_event(CAPTAIN, free[0]["data"]))
        self.assertEqual(load_fixed_codes(self.fixed)["甲"], "王大同")
        self.assertIn("王大同 目前是「甲」", self.say("役男 王大同"))

    def test_a_one_tap_cell_on_an_old_card_does_not_replace_who_took_the_code(self):
        self.say("新增人員 王大同 陳大明")
        self.say("役男 王大同")
        self.say("役男 甲 陳大明")                                     # someone takes 甲 meanwhile
        with patch("all_staff.bot.today", return_value=TODAY):
            self.bot.handle(postback_event(CAPTAIN, "setcode|甲|王大同|empty"))
        self.assertIn("「甲」剛剛已經給了 陳大明。要換成 王大同 嗎？", self.reply())
        self.assertEqual(load_fixed_codes(self.fixed)["甲"], "陳大明")

    def test_an_unknown_name_alone_is_offered_新增人員_first(self):
        reply = self.say("役男 你好")
        self.assertIn("名冊裡還沒有「你好」", reply)
        self.assertEqual(self.transport.raw[-1][-1]["quickReply"]["items"][0]["action"]["text"], "新增人員 你好")
        self.assertNotIn('"setcode', json.dumps(self.transport.raw[-1]))   # no one-tap cells for it

    def test_list_and_clear_words_after_the_code_word_show_the_grid(self):
        for typed in ("役男 列表", "役男代號列表", "役男名冊", "固定代號 清單"):
            self.assertIn("固定代號 1/34", self.say(typed), typed)
        self.assertIn("要清空哪個代號", self.say("役男 取消"))
        self.assertEqual(load_fixed_codes(self.fixed), {"己": "蕭志明"})

    def test_registered_code_holders_are_marked_in_the_roster(self):
        self.register("U2", "蕭志明")
        self.say("人員名冊")
        texts = flex_texts(self.transport.raw[-1][-1]["contents"])
        self.assertIn("共 3 位｜✓ 已在機器人登記 2 位", texts)
        self.assertIn("蕭志明✓", texts)
        cell = next(n for n in walk(self.transport.raw[-1][-1]) if n.get("type") == "box"
                    and flex_texts(n) == ["己", "蕭志明✓"])
        self.assertEqual(cell["backgroundColor"], "#E3F4E8")           # the whole cell turns green
        self.say("固定代號")
        self.assertIn("蕭志明✓", flex_texts(self.transport.raw[-1][-1]["contents"]))

    def test_the_answer_to_a_change_is_the_grid_with_that_code_framed(self):
        self.say("新增人員 王大同")
        self.say("役男 丁 王大同")
        note, card = self.transport.raw[-1]
        self.assertIn("✅", note["text"])
        cells = [n for n in walk(card) if n.get("action", {}).get("data", "").startswith("fixed|")]
        self.assertEqual([c["action"]["data"][6:] for c in cells],
                         list("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉ABCDEFGHIJKLMN"))
        self.assertEqual([c["action"]["fillInText"] for c in (cells[0], cells[20])], ["役男 甲 ", "代號 A "])
        framed = [c["action"]["data"][6:] for c in cells if c.get("borderWidth") == "2px"]
        self.assertEqual(framed, ["丁"])
        self.assertEqual(flex_texts(cells[3]), ["丁", "王大同"])
        self.assertEqual(flex_texts(cells[0]), ["甲", "　"])     # left blank

    def test_tapping_a_code_cell_says_how_to_fill_it(self):
        self.bot.handle(postback_event(CAPTAIN, "fixed|丁"))
        self.assertIn("役男代號「丁」目前空著", self.reply())
        labels = [i["action"]["label"] for i in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertEqual(labels, ["✏️ 填寫 丁", "📌 固定代號"])
        self.bot.handle(postback_event(CAPTAIN, "fixed|己"))
        self.assertIn("「己」目前是 蕭志明", self.reply())
        items = [i["action"] for i in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertEqual(items[1]["text"], "役男 己 取消")
        self.bot.handle(postback_event(CAPTAIN, "fixed|K"))
        self.assertIn("英文代號「K」目前空著", self.reply())
        self.assertEqual(self.transport.raw[-1][-1]["quickReply"]["items"][0]["action"]["fillInText"], "代號 K ")
        self.bot.handle(postback_event("U1", "fixed|丁"))
        self.assertIn("只有幹部可以使用", self.reply())
        count = len(self.transport.replies)
        for data in ("fixed|7", "fixed|", "fixed|丁|x", "fixed|O", "guide|解除", "guide|"):
            self.bot.handle(postback_event(CAPTAIN, data))
        self.assertEqual(len(self.transport.replies), count)

    def test_a_tile_that_types_a_command_also_answers_how(self):
        self.bot.handle(postback_event(CAPTAIN, "guide|新增人員"))
        self.assertIn("例如：新增人員 王小明 陳大華", self.reply())
        self.bot.handle(postback_event(CAPTAIN, "guide|移除幹部"))
        self.assertIn("例如：移除幹部 學姊", self.reply())
        self.bot.handle(postback_event(CAPTAIN, "guide|新增幹部"))
        self.assertIn("例如：新增幹部 王小明 副隊長", self.reply())
        self.bot.handle(postback_event("U1", "guide|新增人員"))
        self.assertIn("只有幹部可以使用", self.reply())
        self.assertEqual(self.names.read_text(encoding="utf-8"), STAFF)

    def test_without_files_the_list_cannot_be_changed(self):
        self.bot.settings.staff_names = None
        self.assertIn("沒有設定名冊檔", self.say("新增人員 王大同"))


class OfficerCommandTests(OfficerCase):
    def test_the_list_names_each_officer(self):
        reply = self.say("幹部名單")
        self.assertIn("幹部名單｜2 位：學姊、隊長 林大明", reply)
        self.assertIn("學姊\n尚未登記姓名\nU6108…0893", reply)
        self.assertIn("隊長\n林大明\n已登記", reply)
        fills = [i["action"].get("fillInText") for i in self.transport.raw[-1][-1]["quickReply"]["items"]]
        self.assertEqual(fills[:2], ["新增幹部 ", "移除幹部 "])

    def test_an_officer_is_added_by_the_name_they_registered(self):
        reply = self.say("新增幹部 林小明 副隊長")
        self.assertIn("林小明 現在是幹部了（副隊長）", reply)
        self.assertTrue(self.store.is_officer("U1"))
        self.assertIn("人員管理", self.say("人員管理", "U1"))
        self.assertIn("林小明 本來就是幹部。", self.say("新增幹部 林小明"))

    def test_or_by_their_user_id(self):
        self.assertIn("現在是幹部了", self.say("新增幹部 U" + "a" * 32))
        self.assertTrue(self.store.is_officer("U" + "a" * 32))

    def test_a_name_nobody_registered_or_two_people_registered(self):
        self.assertIn("找不到登記「王大同」的 LINE 帳號", self.say("新增幹部 王大同"))
        with self.store.lock, self.store.db:              # U1 has it too: only a database from before a name
            self.store.db.execute("drop index if exists users_staff_name")   # belonged to one account holds this
            self.store.db.execute("insert into users(user_id, staff_name, updated_at) values('U2', '林小明', 1.0)")
        reply = self.say("新增幹部 林小明")
        self.assertIn("有 2 個 LINE 帳號登記「林小明」", reply)
        self.assertIn("U2", reply)
        self.assertFalse(self.store.is_officer("U1") or self.store.is_officer("U2"))
        self.assertIn("請輸入對方登記的姓名", self.say("新增幹部 Tom"))
        self.assertIn("例如：新增幹部 王小明", self.say("新增幹部"))

    def test_an_officer_is_removed_by_title_name_or_user_id(self):
        self.assertIn("學姊 已經不是幹部了", self.say("移除幹部 學姊"))
        self.assertIn("沒有「學姊」", self.say("移除幹部 學姊"))
        self.say("新增幹部 林小明")
        self.assertIn("林小明 已經不是幹部了", self.say("移除幹部 林小明"))
        self.assertIn("例如：移除幹部 學姊", self.say("移除幹部"))

    def test_the_last_officer_stays_unless_there_is_an_administrator(self):
        self.say("移除幹部 學姊")
        self.assertIn("最後一位幹部", self.say("移除幹部 隊長"))
        self.assertTrue(self.store.is_officer(CAPTAIN))
        self.bot.settings.admins = ("U2",)
        self.assertIn("你已經不是幹部了", self.say("移除幹部 隊長"))
        # No longer a 幹部, and 林大明 is not on the 名冊: the 名冊守門員 keeps them out of everything
        self.assertIn("你登記的姓名「林大明」目前不在名冊上", self.say("人員管理"))
        self.say("新增人員 林大明", "U2")                   # the administrator puts them on it
        self.assertIn("只有幹部可以使用", self.say("人員管理"))

    def test_an_administrator_who_is_also_an_officer_keeps_managing(self):
        self.bot.settings.admins = (CAPTAIN,)
        self.assertIn("不過你是管理員", self.say("移除幹部 隊長"))
        self.assertIn("👥 人員管理", self.say("人員管理"))

    def test_a_stray_button_does_nothing(self):
        count = len(self.transport.replies)
        self.bot.handle(postback_event(CAPTAIN, "fill:新增人員"))
        self.assertEqual(len(self.transport.replies), count)


class FixedSinceTests(OfficerCase):
    """A code set from LINE applies from that day's roster on; older rosters keep their own name table."""

    def roster(self, day):
        return {"run_id": "run-1", "roster_date": day}

    def test_a_code_set_today_does_not_rewrite_older_rosters(self):
        result = copy.deepcopy(RESULT)                                # its name table: 26 is 王大同
        self.say("新增人員 王大同")
        self.say("代號 A 王大同")
        older = self.bot.names_for(self.roster("2026-10-07"), copy.deepcopy(result))
        today = self.bot.names_for(self.roster("2026-10-08"), copy.deepcopy(result))
        undated = self.bot.names_for({"run_id": "run-1"}, copy.deepcopy(result))
        self.assertEqual((older.get("26"), older.get("A")), ("王大同", None))
        self.assertEqual((today.get("26"), today.get("A")), (None, "王大同"))
        self.assertEqual(undated.get("A"), "王大同")
        self.assertEqual(self.bot.names_for(self.roster("2026-10-01"), copy.deepcopy(result)).get("己"), "蕭志明")

    def test_the_day_follows_the_code_and_goes_with_it(self):
        book = StaffBook(self.names, self.fixed)
        self.say("新增人員 王大同")
        self.say("役男 丁 王大同")
        self.assertEqual(book.since(), {"丁": "2026-10-08"})
        with patch("all_staff.bot.today", return_value=date(2026, 10, 9)):
            self.ask("役男 丁 王大同", CAPTAIN)                         # the same person again: the day stays
        self.assertEqual(book.since(), {"丁": "2026-10-08"})
        self.say("役男 甲 王大同")                                      # moved: 丁 is free, 甲 is new
        self.assertEqual(book.since(), {"甲": "2026-10-08"})
        self.say("役男 甲 取消")
        self.assertEqual(book.since(), {})
        self.say("役男 乙 王大同")
        self.say("刪除人員 王大同")
        self.assertEqual((book.since(), load_fixed_codes(self.fixed)), ({}, {"己": "蕭志明"}))

    def test_a_broken_dates_file_counts_as_none(self):
        book = StaffBook(self.names, self.fixed)
        book.since_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(book.since(), {})
        self.assertEqual(book.fixed_on(date(2026, 1, 1)), {"己": "蕭志明"})


class StaffCardTests(unittest.TestCase):
    """The cards on their own: what LINE accepts, however long the list grows."""

    def size(self, node):   # compact, as LineTransport sends it
        return len(json.dumps(node, ensure_ascii=False, separators=(",", ":")).encode())

    def test_the_real_list_fits_in_one_bubble(self):
        from all_staff.bot import staff_roster_card
        root = Path(__file__).resolve().parent / "configs"
        card = staff_roster_card(load_directory(root / "staff_names.txt"), load_fixed_codes(root / "fixed_staff_codes.json"),
                                 {"賴志明": 1, "簡志明": 1, "劉宇康": 1})
        self.assertEqual(card["contents"]["type"], "bubble")
        self.assertLess(self.size(card["contents"]), 28_000)
        self.assertLessEqual(len(card["altText"]), 400)
        rows = [n for n in walk(card) if n.get("layout") == "horizontal" and n.get("paddingAll") == "7px"]
        self.assertTrue(all(len(row["contents"]) == 4 for row in rows))
        self.assertEqual([("backgroundColor" in row) for row in rows[:4]], [True, False, True, False])   # striped

    def test_a_long_list_goes_on_pages_that_each_fit(self):
        from all_staff.bot import staff_roster_card
        names = [f"王{chr(0x4E00 + n)}{chr(0x4E80 + n)}" for n in range(260)]
        card = staff_roster_card(names, {"甲": "蕭志明"}, {name: 1 for name in names[::3]} | {"外人甲": 1})
        bubbles = card["contents"]["contents"]
        self.assertEqual(card["contents"]["type"], "carousel")
        self.assertLessEqual(len(bubbles), 12)
        self.assertTrue(all(self.size(bubble) < 30_000 for bubble in bubbles))
        self.assertLess(self.size(card["contents"]), 50_000)
        shown = [t.rstrip("✓") for bubble in bubbles[1:] for t in flex_texts(bubble["body"])]
        self.assertEqual(shown, names)
        self.assertIn("🎖️ 役男（中文代號）", flex_texts(bubbles[0]))

    def test_names_in_a_row_are_spaced_and_columns_line_up(self):
        from all_staff.bot import name_rows
        rows = name_rows(["歐陽小明明", "王大同", "李四五", "張三六", "陳七"], {})
        self.assertTrue(all(row["spacing"] == "sm" and len(row["contents"]) == 4 for row in rows))
        self.assertEqual([c["text"] for c in rows[1]["contents"]], ["陳七", "　", "　", "　"])
        self.assertNotIn("filler", json.dumps(rows))

    def test_a_paged_roster_says_where_the_names_are(self):
        from all_staff.bot import staff_roster_card
        for count in (400, 900):
            names = [f"王{chr(0x4E00 + n)}{chr(0x5000 + n)}" for n in range(count)]
            bubbles = staff_roster_card(names, {"甲": "蕭志明"}, {})["contents"]["contents"]
            first = flex_texts(bubbles[0])
            self.assertTrue(first[2].startswith(f"👉 往左滑看沒有固定代號的 {count} 位"), first[2])
            self.assertFalse(any("排在下面" in text for text in first))
            heads = [flex_texts(b["header"])[0] for b in bubbles[1:]]
            self.assertEqual(heads, [f"📋 名冊 {n}/{len(heads)}" for n in range(1, len(heads) + 1)])
            self.assertLess(self.size({"type": "carousel", "contents": bubbles}), 50_000)
            shown = sum(1 for b in bubbles[1:] for t in flex_texts(b["body"]) if t != "　")
            if shown < count:
                self.assertIn(f"最後 {count - shown} 位沒有列出", first[2])

    def test_a_long_officer_list_stays_within_the_limits(self):
        from all_staff.bot import officers_card
        officers = [{"user_id": f"U{n:032x}", "title": "十個字的職稱名稱喔啊", "staff_name": "歐陽娜娜娜", "name": None}
                    for n in range(60)]
        card = officers_card(officers, 0)
        self.assertLessEqual(len(card["altText"]), 400)
        self.assertLess(self.size(card["contents"]), 28_001)
        self.assertTrue(any(t.startswith("…還有 ") for t in flex_texts(card["contents"])))

    def test_every_code_card_stays_small(self):
        from all_staff.bot import fixed_codes_card, staff_menu_card
        from all_staff.schema import FIXED_CODES
        full = fixed_codes_card({code: "歐陽小明" for code in FIXED_CODES}, changed="N")
        self.assertLess(self.size(full["contents"]), 22_000)
        self.assertLessEqual(len(full["altText"]), 400)
        menu = staff_menu_card(names=999, fixed=20, registered=999, unlisted=99, officers=99)
        self.assertLess(self.size(menu["contents"]), 10_000)
        labels = [n["action"]["label"] for n in walk(full) if "action" in n]
        self.assertTrue(all(len(label) <= 20 for label in labels))


if __name__ == "__main__":
    unittest.main()
