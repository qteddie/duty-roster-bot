"""The administrator's upload report says who is missing and why, which cells are unsure, and shows the name table.

Two photos of the 10/14 sheet were reported as 「姓名配對 53／56（3 個沒對到）｜姓名欄空白 3 個（76、未、申）｜信心較低
9 格｜沒對到姓名的代號：32、E、辰」 and 「姓名配對 51／52（1 個沒對到）｜姓名欄空白 3 個（76、卯、未）｜信心較低 6 格｜
沒對到姓名的代號：E」. On that sheet's name table the cells of 26, 54, 55, 56, 76, 77, 78, 83, 84, 寅, 卯, 午, 未, 申, 酉, E
and N are empty, the fixed codes in force leave 寅卯午未申酉 and E, N to nobody, and 辰 is not printed on the table at
all. Of those codes only 32 has someone whose name was not read: 76 (a blank numeric cell), 未, 申, 卯, E (fixed codes
nobody is set to) and 辰 (no such code on the sheet) are codes nobody has that day (``unnamed_codes``).
"""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from all_staff.bot import (FLEX_BUBBLE_LIMIT, NAME_TABLE_CODES, REPORT_NOBODY, REPORT_STRETCHES, REPORT_UNNAMED,
                           REPORT_UNSURE, TAIPEI, Store, flex_size, slot_card, staff_card, unnamed_codes, upload_report,
                           who_is_on, work_card)
from all_staff.name_table import NAMES_VERSION
from all_staff.schema import ALLOWED_CODES
from test_all_staff_blank_names import panel, report_rows
from test_all_staff_bot import BotCase, flex_texts, image_event, postback_event, slot
from test_all_staff_duty_card import check_limits

DAY = "2026-10-14"
SENT = datetime(2026, 10, 14, 7, 5, tzinfo=TAIPEI).timestamp()
ROSTER = {"run_id": "run-1", "roster_date": DAY, "sent_at": SENT}
# The empty name cells of the 10/14 sheet, in table order as ``read_name_cells`` gives them
SHEET_BLANK = ["26", "寅", "卯", "午", "未", "申", "酉", "54", "55", "56", "76", "77", "78", "83", "84", "E", "N"]
BLANK_ROW = "9 個（26、54、55、56、76、77、78、83、84）"
SURNAMES, GIVEN = "王李張劉陳楊黃趙吳周徐孫馬朱胡郭何高", "安柏宸德恩芳冠宏家建杰俊凱立明"
STAFF = ["蘇志明"] + [f"{s}{a}{b}" for s in SURNAMES for a, b in zip(GIVEN, GIVEN[3:] + GIVEN[:3])][:79]
# The fixed codes in force: every one but 寅卯午未申酉 and E, N (辰, 巳 are not on the sheet's table)
FIXED = dict(zip("甲乙丙丁戊己庚辛壬癸子丑ABCDFGHIJKLM", STAFF[50:74]))
NUMERIC = ["72"] + [str(n) for n in range(1, 36) if n not in (26, 32)] + [str(n) for n in range(57, 67)]   # 44 named
NUMERIC_NAMES = dict(zip(NUMERIC, STAFF))
NOBODY_SLOTS = {
    "76": [slot(13, 3, "國際線"), slot(14, 3, "國際線")],
    "E": [slot(5, 20, "國內線二線"), slot(6, 20, "國內線二線"), slot(12, 21, "國內線帶勤")],
    "辰": [slot(16, 4, "國際線")],
    "未": [slot(9, 17, "國內線一線", .62)],
    "申": [slot(10, 9, "國際線備勤")],
    "卯": [slot(8, 5, "國際線")],
}


def sheet(fixed_codes, nobody, unsure, *, with_32=True):
    """One photo of the 10/14 sheet: the 44 numeric codes and ``fixed_codes``, all named; 32, whose name 羅志明 is
    not on the 名冊; and the codes ``nobody`` has. ``unsure`` cells are low-confidence: 72's at 08–09, 32's, 未's,
    then the first of the named codes'."""
    people = [{"code": code, "slots": [slot(4 + index % 18, 2 + index % 15, "國際線")]}
              for index, code in enumerate(NUMERIC + list(fixed_codes))]
    people[0]["slots"] = [slot(7, 2, "國際線", .58)]                         # 08–09 國際線　72 蘇志明 58%
    if with_32:
        people.append({"code": "32", "slots": [slot(13, 4, "國際線", .61)]})  # unsure, and its name not read
    people += [{"code": code, "slots": copy.deepcopy(NOBODY_SLOTS[code])} for code in nobody]
    low = sum(s["confidence"] < .75 for person in people for s in person["slots"])
    for person in people[1:1 + unsure - low]:
        person["slots"][0].update(confidence=.66, needs_review=True)
    readings = {code: [name, name, "", name] for code, name in NUMERIC_NAMES.items()}
    readings["32"] = ["林承", "羅志明", "", "林 承潔"]   # the most frequent is not the first
    return {"run": {"id": "run-1", "codes": len(people), "roster_date": DAY},
            "sheets": [{"cells": [{"pred": [{"code": "72", "conf": .99}]}], "warnings": []}],
            "people": people, "names": readings, "names_blank": list(SHEET_BLANK), "names_version": NAMES_VERSION}


FIRST = sheet("甲乙丙丁戊ABCD", ["76", "E", "辰", "未", "申"], 9)            # 59 codes, 9 unsure
SECOND = sheet("甲乙丙丁ABC", ["76", "E", "卯", "未"], 6, with_32=False)    # 55 codes, 6 unsure


def names_of(result, fixed=FIXED):
    """What ``names_for`` gives for a photo: the numeric codes read, and the fixed codes in force."""
    on_duty = {person["code"] for person in result["people"]}
    return {**{code: name for code, name in NUMERIC_NAMES.items() if code in on_duty}, **fixed}


def report(test, result, **options):
    """The report of one photo, checked against LINE's limits."""
    options.setdefault("names", names_of(result))
    options.setdefault("fixed", set(FIXED))
    card = upload_report("ok", "曾志明", SENT, roster=ROSTER, result=result, link=True, **options)
    check_limits(test, card)
    return card


class ClassificationTests(unittest.TestCase):
    """``unnamed_codes``: who has no name, and of those, whose code nobody has that day and why."""

    def test_the_first_photo(self):
        unread, nobody = unnamed_codes(FIRST, names_of(FIRST), set(FIXED))
        self.assertEqual(unread, ["32"])
        self.assertEqual(nobody, {"76": "姓名欄空白", "E": "固定代號沒有設定", "辰": "班表上沒有這個代號",
                                  "未": "固定代號沒有設定", "申": "固定代號沒有設定"})
        self.assertEqual(list(nobody), ["76", "E", "辰", "未", "申"])                # code order
        self.assertEqual(unnamed_codes(FIRST, names_of(FIRST)), (unread, nobody))   # fixed codes taken from the names

    def test_a_fixed_code_follows_the_settings_whatever_its_cell_holds(self):
        result = {"people": [{"code": c, "slots": []} for c in ("未", "丁", "A")], "names_blank": ["未"]}
        # 丁's cell has ink, 未's is blank: nobody is set to either, so nobody has them
        self.assertEqual(unnamed_codes(result, {}, set()),
                         ([], {"A": "固定代號沒有設定", "丁": "固定代號沒有設定", "未": "固定代號沒有設定"}))
        # A set in the settings but left without a name by whoever made ``names``: someone has it, unread
        self.assertEqual(unnamed_codes(result, {}, {"A"}), (["A"], {"丁": "固定代號沒有設定", "未": "固定代號沒有設定"}))

    def test_codes_the_sheet_does_not_print(self):
        self.assertEqual(len(NAME_TABLE_CODES), 98)
        self.assertNotIn("辰", NAME_TABLE_CODES)
        self.assertNotIn("40", NAME_TABLE_CODES)
        result = {"people": [{"code": c, "slots": []} for c in ("40", "巳", "x")]}
        self.assertEqual(unnamed_codes(result, {}), ([], {"40": "班表上沒有這個代號", "巳": "班表上沒有這個代號",
                                                        "x": "班表上沒有這個代號"}))

    def test_a_result_read_before_blank_cells_keeps_its_numeric_codes_unread(self):
        result = {"people": [{"code": c, "slots": []} for c in ("26", "未", "72")]}
        self.assertEqual(unnamed_codes(result, {"72": "林小明"}), (["26"], {"未": "固定代號沒有設定"}))


class FirstPhotoTests(unittest.TestCase):
    """The first 10/14 photo: 59 codes with duties, 53 named, 32 unread, five codes nobody has."""

    def setUp(self):
        self.card = report(self, FIRST, has_image=True)
        self.rows = report_rows(self.card)

    def test_the_rows(self):
        self.assertEqual(list(self.rows), ["📅 班表日期", "👥 有勤務", "👤 姓名配對", "⚠️ 沒有這個人", "❓ 信心較低",
                                           "⬜ 當天空白的代號"])
        self.assertEqual(self.rows["👥 有勤務"], "59 個代號")
        self.assertEqual(self.rows["👤 姓名配對"], "53／54（1 個沒讀出來）")
        self.assertEqual(self.rows["⚠️ 沒有這個人"], "5 個（76、E、辰、未、申）")
        self.assertEqual(self.rows["❓ 信心較低"], "9 格")
        self.assertEqual(self.rows["⬜ 當天空白的代號"], BLANK_ROW)   # numeric only: fixed codes follow the settings
        self.assertIn("姓名配對 53／54（1 個沒讀出來），沒有這個人 5 個（76、E、辰、未、申），信心較低 9 格，"
                      f"當天空白的代號 {BLANK_ROW}", self.card["altText"])
        self.assertFalse(any("失敗" in text for text in flex_texts(self.card["contents"])))

    def test_where_the_codes_nobody_has_got_duties(self):
        self.assertEqual(panel(self.card, "⚠️ 沒有這個人，卻讀到勤務"), [
            "76（姓名欄空白）", "14–16 國際線",
            "E（固定代號沒有設定）", "06–08 國內二線、13–14 國內帶勤",
            "辰（班表上沒有這個代號）", "17–18 國際線",
            "未（固定代號沒有設定）", "10–11 國內一線",
            "申（固定代號沒有設定）", "11–12 國際備勤",
            "多半是勤務格的代號讀錯了，請對照原圖；兼服（#）的格子除外。",
            "固定代號其實有人的話，傳「代號 E 姓名」設定。"])

    def test_the_unsure_cells_by_time_with_who_is_on_them(self):
        lines = panel(self.card, "❓ 信心較低的格子")
        self.assertEqual(len(lines), 9 + 1)                 # every cell, then the hint
        self.assertIn("08–09 國際線　72 蘇志明 58%", lines)
        self.assertIn(f"07–08 國際線　2 {NUMERIC_NAMES['2']} 66%", lines)
        self.assertIn("10–11 國內一線　未 62%", lines)      # a code nobody has: the code alone
        self.assertIn("14–15 國際線　32 61%", lines)        # a name not read: the code alone
        hours = [line[:5] for line in lines[:-1]]
        self.assertEqual(hours, sorted(hours))
        self.assertEqual(lines[-1], "請對照原圖確認這幾格的代號。")

    def test_what_the_photo_read_for_the_name_not_matched(self):
        self.assertEqual(panel(self.card, "🔍 姓名沒讀出來"), [
            "32：照片上讀到「羅志明」",                       # the most frequent reading, spaces dropped
            "名冊上沒有這個姓名的話，傳「新增人員 姓名」加進名冊，之後就會配對；本人也可以傳「我是 32 10/14」。"])

    def test_the_header_the_date_tile_and_the_footer_are_kept(self):
        texts = flex_texts(self.card["contents"])
        self.assertEqual(texts[:2], ["✅ 群組班表辨識成功", "曾志明｜10/14 07:05 傳到群組"])
        self.assertIn("📅 日期不對？點這裡改這張的日期", texts)
        self.assertEqual(texts[-2:], ["📷 下方是勤務人員（姓名代碼對照表）的原圖，可點開放大", "🖼️ 查看整張班表"])
        self.assertEqual(self.card["contents"]["footer"]["contents"][0]["action"]["data"], "roster|run-1")
        self.assertEqual(self.card["contents"]["size"], "mega")
        self.assertLess(flex_size(self.card["contents"]), 12_000)


class SecondPhotoTests(unittest.TestCase):
    def test_every_name_there_is_matched(self):
        card = report(self, SECOND)
        rows = report_rows(card)
        self.assertEqual(rows["👥 有勤務"], "55 個代號")
        self.assertEqual(rows["👤 姓名配對"], "51／51，全部對到")
        self.assertEqual(rows["⚠️ 沒有這個人"], "4 個（76、E、卯、未）")
        self.assertEqual(rows["❓ 信心較低"], "6 格")
        self.assertEqual(rows["⬜ 當天空白的代號"], BLANK_ROW)
        self.assertIsNone(panel(card, "🔍 姓名沒讀出來"))
        self.assertEqual(panel(card, "⚠️ 沒有這個人，卻讀到勤務")[4:6], ["卯（固定代號沒有設定）", "09–10 國際線"])
        self.assertNotIn("📷 下方是勤務人員（姓名代碼對照表）的原圖，可點開放大", flex_texts(card["contents"]))


class ReportEdgeTests(unittest.TestCase):
    def test_a_fixed_code_set_today_is_matched_though_its_cell_is_blank(self):
        fixed = {**FIXED, "未": "邱志成"}   # 未's cell is blank on the sheet: the settings win
        rows = report_rows(report(self, FIRST, names=names_of(FIRST, fixed), fixed=set(fixed)))
        self.assertEqual(rows["👤 姓名配對"], "54／55（1 個沒讀出來）")
        self.assertEqual(rows["⚠️ 沒有這個人"], "4 個（76、E、辰、申）")

    def test_the_blank_row_says_so_when_no_cell_came_out_empty(self):
        rows = report_rows(report(self, {**FIRST, "names_blank": []}))
        self.assertEqual(rows["⬜ 當天空白的代號"], "沒有")
        self.assertEqual(rows["⚠️ 沒有這個人"], "4 個（E、辰、未、申）")   # 76 is unread now
        self.assertEqual(rows["👤 姓名配對"], "53／55（2 個沒讀出來）")
        before = {key: value for key, value in FIRST.items() if key != "names_blank"}
        for result in ({**FIRST, "names_blank": [], "names": {}}, before):   # the table not read, or read before
            self.assertNotIn("⬜ 當天空白的代號", report_rows(report(self, result)))

    def test_nothing_to_match(self):
        result = {"people": [{"code": "76", "slots": [slot(4, 2, "國際線")]}], "names_blank": ["76"], "names": {}}
        self.assertEqual(report_rows(report(self, result, names={}))["👤 姓名配對"], "0／0")

    def test_the_lists_are_capped(self):
        codes = [c for c in ALLOWED_CODES if c.isdigit() and c in NAME_TABLE_CODES]
        hours = [slot(row, 2 + row % 3, ("國際線", "國內線一線", "防爆")[row % 3], .5) for row in range(4, 23)]
        result = {"people": [{"code": c, "slots": copy.deepcopy(hours)} for c in codes],
                  "names_blank": codes[:30], "names": {c: ["  某某某某某某某某某某  ", ""] for c in codes[30:]}}
        card = report(self, result, names={}, fixed=set())
        nobody = panel(card, "⚠️ 沒有這個人，卻讀到勤務")
        self.assertEqual(nobody[:2], ["1（姓名欄空白）", "05–06 國內一線、06–07 防爆、07–08 國際線 等 19 格"])
        self.assertEqual(nobody[1].count("、"), REPORT_STRETCHES - 1)
        self.assertEqual(nobody[2 * REPORT_NOBODY], f"…還有 {30 - REPORT_NOBODY} 個代號")
        unsure = panel(card, "❓ 信心較低的格子")
        self.assertEqual(unsure[REPORT_UNSURE], f"…還有 {len(codes) * 19 - REPORT_UNSURE} 格")
        unread = panel(card, "🔍 姓名沒讀出來")
        self.assertEqual(unread[0], f"{codes[30]}：照片上讀到「某某某某某某某某…」")   # 8 characters
        self.assertEqual(unread[REPORT_UNNAMED], f"…還有 {len(codes) - 30 - REPORT_UNNAMED} 個代號")
        rows = report_rows(card)
        self.assertEqual(rows["⬜ 當天空白的代號"], f"30 個（{'、'.join(codes[:30])}）")
        self.assertEqual(rows["⚠️ 沒有這個人"], f"30 個（{'、'.join(codes[:REPORT_UNNAMED])} 等）")

    def test_cells_past_the_stretch_cap_are_counted_by_cell(self):
        """A code nobody has, on duty in long stretches: the stretches shown are capped, the rest counted in cells."""
        hours = [slot(row, 2, "國際線") for row in range(4, 10)] + [slot(row, 3, "防爆") for row in (12, 14, 16)]
        result = {"people": [{"code": "76", "slots": hours}], "names_blank": ["76"], "names": {}}
        nobody = panel(report(self, result, names={}, fixed=set()), "⚠️ 沒有這個人，卻讀到勤務")
        self.assertEqual(nobody[:2], ["76（姓名欄空白）", "05–11 國際線、13–14 防爆、15–16 防爆 等 9 格"])

    def test_the_worst_case_stays_well_under_the_bubble_limit(self):
        work = "國際1、2號託運行李安檢站（第15欄未辨識）"
        result = {"people": [{"code": c, "slots": [slot(row, 2, work, .3) for row in range(0, 24, 2)]}
                             for c in ALLOWED_CODES],
                  "names_blank": list(ALLOWED_CODES[:60]),
                  "names": {c: ["某" * 40] * 4 for c in ALLOWED_CODES}}
        names = {c: "歐陽小明明" for c in ALLOWED_CODES[60:70]}
        for fixed in (set(), set(ALLOWED_CODES)):   # most codes nobody has, or most unread
            card = upload_report("ok", "張" * 40, SENT, roster=ROSTER, result=result, names=names, fixed=fixed,
                                 replaced=True, link=True, has_image=True)
            check_limits(self, card)
            self.assertLess(flex_size(card["contents"]), FLEX_BUBBLE_LIMIT * 3 // 4)   # each panel at its cap: ~4 KB

    def test_the_failed_and_skipped_cards_are_as_before(self):
        for status in ("failed", "skipped"):
            card = upload_report(status, "華華", SENT, reason="無法從照片切出勤務格：x", has_image=True)
            texts = flex_texts(card["contents"])
            self.assertEqual(texts[2:6], ["原因", "無法從照片切出勤務格：x", "上傳者", "沒有私訊"])
            self.assertNotIn("📷", "".join(texts))
            self.assertEqual(card["contents"]["size"], "mega")


class PushTests(BotCase):
    """One push per administrator, the card and the 勤務人員 crop together; the card alone when there is no crop."""
    friends = {"U1", "U2", "U8", "U9"}
    settings = {"admins": ("U9", "U8"), "notify": False, "public_base": "https://bot.example"}

    def setUp(self):
        super().setUp()
        root = Path(self.folder.name)
        self.bot.settings.staff_names = root / "staff_names.txt"
        self.bot.settings.fixed_staff_codes = root / "fixed_staff_codes.json"
        self.bot.settings.staff_names.write_text("\n".join(STAFF) + "\n", encoding="utf-8")
        self.fixed(FIXED)
        self.analyzer.data = copy.deepcopy(FIRST)

    def fixed(self, mapping, since=None):
        self.bot.settings.fixed_staff_codes.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        if since:
            self.bot.settings.fixed_staff_codes.with_name("fixed_staff_codes.since.json").write_text(
                json.dumps(since), encoding="utf-8")

    def upload(self):
        """A group photo of the 10/14 sheet; what the administrator U9 was pushed."""
        self.transport.pushes.clear(); self.transport.raw_pushes.clear()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.bot.handle(image_event("U2", "Cgroup"))
        self.log = out.getvalue()
        pushes = [(to, sent) for to, sent in self.transport.raw_pushes if to in ("U8", "U9")]
        self.assertEqual(sorted(to for to, _ in pushes), ["U8", "U9"])   # one push request each
        self.assertEqual(pushes[0][1], pushes[1][1])
        return dict(pushes)["U9"]

    def test_the_card_and_the_name_table_go_in_one_push(self):
        sent = self.upload()
        self.assertEqual([m["type"] for m in sent], ["flex", "image"])
        self.assertEqual(sent[1], {"type": "image",
                                   "originalContentUrl": "https://bot.example/runs/run-1/staff-table-v1.jpg",
                                   "previewImageUrl": "https://bot.example/runs/run-1/staff-table-v1-preview.jpg"})
        rows = report_rows(sent[0])   # names_for and the staff book, as the report module function was given
        self.assertEqual(rows["👤 姓名配對"], "53／54（1 個沒讀出來）")
        self.assertEqual(rows["⚠️ 沒有這個人"], "5 個（76、E、辰、未、申）")
        self.assertEqual(rows["⬜ 當天空白的代號"], BLANK_ROW)
        self.assertIn("📷 下方是勤務人員（姓名代碼對照表）的原圖，可點開放大", flex_texts(sent[0]["contents"]))
        self.assertIn("08–09 國際線　72 蘇志明 58%", panel(sent[0], "❓ 信心較低的格子"))
        self.assertEqual(panel(sent[0], "🔍 姓名沒讀出來")[0], "32：照片上讀到「羅志明」")
        check_limits(self, sent[0])

    def test_the_card_goes_alone_when_the_crop_fails_or_images_are_off(self):
        def broken(run_id):
            raise OSError("photo unavailable")
        self.analyzer.staff_table_image = broken
        sent = self.upload()
        self.assertEqual([m["type"] for m in sent], ["flex"])
        self.assertIn("勤務人員對照圖失敗（run-1）：OSError: photo unavailable", self.log)
        self.assertNotIn("📷 下方是勤務人員（姓名代碼對照表）的原圖，可點開放大", flex_texts(sent[0]["contents"]))
        self.analyzer.staff_table_image = lambda run_id: None
        self.assertEqual([m["type"] for m in self.upload()], ["flex"])
        drawn = []   # a crop that would go out if asked for: _staff_table_image swallows whatever a stub raises
        self.analyzer.staff_table_image = lambda run_id: drawn.append(run_id) or (
            "run-1/staff-table-v1.jpg", "run-1/staff-table-v1-preview.jpg")
        self.bot.settings.images = False
        sent = self.upload()
        self.assertEqual([m["type"] for m in sent], ["flex"])
        self.assertNotIn("footer", sent[0]["contents"])
        self.assertEqual(drawn, [])

    def test_a_failed_photo_brings_no_crop(self):
        def analyze(*args):
            raise RuntimeError("disk full")
        self.analyzer.analyze = analyze
        drawn = []
        self.analyzer.staff_table_image = lambda run_id: drawn.append(run_id) or (
            "run-1/staff-table-v1.jpg", "run-1/staff-table-v1-preview.jpg")
        self.assertEqual([m["type"] for m in self.upload()], ["flex"])
        self.assertEqual(drawn, [])

    def test_a_fixed_code_set_now_is_named_even_on_a_blank_cell(self):
        self.fixed({**FIXED, "未": "邱志成"})   # 未's cell is blank on the 10/14 sheet
        rows = report_rows(self.upload()[0])
        self.assertEqual(rows["👤 姓名配對"], "54／55（1 個沒讀出來）")
        self.assertEqual(rows["⚠️ 沒有這個人"], "4 個（76、E、辰、申）")

    def test_a_fixed_code_set_after_the_roster_day_is_a_daily_code_on_it(self):
        """Set from 10/15 on, 未 was read off the 10/14 sheet like a number: its cell is blank there, so nobody had it
        (姓名欄空白, not 固定代號沒有設定, which 「代號 未 姓名」 could not fix for that day anyway)."""
        self.fixed({**FIXED, "未": "邱志成"}, since={"未": "2026-10-15"})
        card = self.upload()[0]
        self.assertEqual(report_rows(card)["⚠️ 沒有這個人"], "5 個（76、E、辰、未、申）")
        self.assertIn("未（姓名欄空白）", flex_texts(card))
        self.analyzer.data["names_blank"] = [c for c in self.analyzer.data["names_blank"] if c != "未"]
        self.assertEqual(report_rows(self.upload()[0])["👤 姓名配對"], "53／55（2 個沒讀出來）")   # ink there: unread

    def test_the_log(self):
        self.upload()
        self.assertIn("班表 run-1：1 個有勤務的代號沒有對到姓名：32", self.log)
        self.assertIn("班表 run-1：5 個有勤務的代號當天沒有這個人（勤務格的代號可能讀錯）："
                      "76（姓名欄空白）、E、未、申（固定代號沒有設定）、辰（班表上沒有這個代號）", self.log)

    def test_the_staff_table_still_sends_its_crop(self):
        self.upload()
        self.register("U1", NUMERIC_NAMES["72"])
        self.bot.handle(postback_event("U1", "staff|run-1"))
        sent = self.transport.raw[-1]
        self.assertEqual([m["type"] for m in sent], ["flex", "image"])
        texts = "\n".join(flex_texts(sent[0]["contents"]))
        self.assertIn("76 空白", texts)
        self.assertIn("未 沒設定", texts)
        self.assertIn("— 姓名未辨識 1 人，請看下方原圖", texts)


class PeopleCardTests(unittest.TestCase):
    """勤務人員, 查看時段 and 查看工作 label the codes nobody has neutrally, apart from the names not read."""
    NOTE = "⬜ 76（姓名欄空白）、E、未、申（固定代號沒有設定）、辰（班表上沒有這個代號）：當天沒有這個人，請對照原圖"

    def test_the_staff_card(self):
        card = staff_card(ROSTER, FIRST, names_of(FIRST))
        check_limits(self, card)
        texts = "\n".join(flex_texts(card["contents"]))
        for cell in ("76 空白", "E 沒設定", "未 沒設定", "申 沒設定", "辰 表上沒有", "32 —"):
            self.assertIn(cell, texts)
        self.assertIn(self.NOTE, texts)
        self.assertIn("— 姓名未辨識 1 人", texts)
        self.assertNotIn("辨識失敗", texts)

    def test_the_hour_and_the_work(self):
        names = names_of(FIRST)
        hour = flex_texts(slot_card(FIRST, 13, "intl", names, ROSTER)["contents"])
        for words in ("\n".join(hour), who_is_on(FIRST, 13, "intl", names, ROSTER)):
            self.assertIn("⬜ 76（姓名欄空白）：當天沒有這個人，請對照原圖", words)
            self.assertNotIn("辨識失敗", words)
        self.assertIn("🔤 只有代號：姓名未辨識", hour[-1])   # 32 is there too
        work = "\n".join(flex_texts(work_card(ROSTER, FIRST, names, "國際線")["contents"]))
        self.assertIn("⬜ 76（姓名欄空白）、辰（班表上沒有這個代號）：當天沒有這個人，請對照原圖", work)

    def test_a_long_list_is_capped(self):
        codes = [str(n) for n in range(1, 36)]
        result = {"people": [{"code": c, "slots": [slot(4, 2, "國際線")]} for c in codes], "names_blank": codes}
        words = who_is_on(result, 4, "intl", {}, ROSTER)
        self.assertIn(f"⬜ {'、'.join(codes[:REPORT_UNNAMED])}（姓名欄空白）等 35 個：當天沒有這個人，請對照原圖", words)


class RosterCheckTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        Store(self.root / "bot.sqlite3").add_roster("Cgroup", "run-1", "U1", 59, DAY)
        (self.root / "runs/run-1/upload").mkdir(parents=True)
        (self.root / "runs/run-1/result.json").write_text(json.dumps(FIRST, ensure_ascii=False), encoding="utf-8")
        (self.root / "staff_names.txt").write_text("\n".join(STAFF) + "\n", encoding="utf-8")

    def tearDown(self):
        self.folder.cleanup()

    def check(self, fixed, since=None):
        from all_staff.roster_check import check_rosters
        (self.root / "fixed_staff_codes.json").write_text(json.dumps(fixed, ensure_ascii=False), encoding="utf-8")
        if since:
            (self.root / "fixed_staff_codes.since.json").write_text(json.dumps(since), encoding="utf-8")
        lines = []
        with mock.patch("backend.phase1_cell_pipeline.GridTemplate.from_json", lambda path: None):
            report = check_rosters(self.root, Path("t.json"), self.root / "staff_names.txt", out=lines.append)
        return report[0], lines[0]

    def test_line_check_tells_the_codes_nobody_has_apart(self):
        report, line = self.check(FIXED)
        self.assertEqual(report["unread"], ["32"])
        self.assertEqual(list(report["nobody"]), ["76", "E", "辰", "未", "申"])
        self.assertIn("有勤務沒對到：32", line)
        self.assertIn("沒有這個人：76（姓名欄空白）、E、未、申（固定代號沒有設定）、辰（班表上沒有這個代號）", line)

    def test_line_check_uses_the_fixed_codes_in_force_on_the_day(self):
        report, line = self.check({**FIXED, "未": "邱志成"})
        self.assertNotIn("未", report["nobody"])
        self.assertIn(f"固定 {len(FIXED) + 1}", line)
        report, line = self.check({**FIXED, "未": "邱志成"}, since={"未": "2026-10-15"})   # set after 10/14
        self.assertEqual(report["nobody"]["未"], "姓名欄空白")                        # a daily code there
        self.assertIn(f"固定 {len(FIXED)}", line)


if __name__ == "__main__":
    unittest.main()
