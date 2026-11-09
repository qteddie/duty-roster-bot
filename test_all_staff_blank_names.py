"""A blank name cell on the 「服勤人員姓名代碼對照表」 is not a name the bot failed to read.

On the 10/13 sheet nobody is 26 (its name cell is empty), yet 26 has duties in the grid (「#26」, a 兼服 mark). The
administrator's upload report used to count it 「沒對到」, which reads like a recognition failure. The name table now
records which located cells have no ink at all (``read_name_cells``), the run keeps them (``names_blank``), and the
report, the server log and ``line-check`` show such a code among those nobody has that day (``unnamed_codes``), apart
from the names that could not be read. ``test_all_staff_report_detail`` has the rest of the report.
"""
import contextlib
import copy
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from all_staff.bot import (NAME_TABLE_CODES, REPORT_UNNAMED, TAIPEI, Store, slot_card, staff_card, unnamed_codes,
                           upload_report, who_is_on, work_card)
from all_staff.schema import ALLOWED_CODES
from test_all_staff_bot import RESULT, BotCase, flex_texts, image_event, slot
from test_all_staff_duty_card import check_limits

SENT = datetime(2026, 10, 13, 7, 5, tzinfo=TAIPEI).timestamp()
ROSTER = {"run_id": "r", "roster_date": "2026-10-13", "sent_at": SENT}
NAMES = {"72": "林小明", "甲": "陳小華", "5": "某某某"}   # 26 has duties but no name
BLANK_NOTE = "⬜ 26（姓名欄空白）：當天沒有這個人，請對照原圖"


def report_rows(card):
    """The report's (label → value) rows: the white box under the band. A row that counts codes has them on a line
    of their own under it: its value here ends with them, 「1 個（26）」."""
    rows = {}
    for line in card["contents"]["body"]["contents"][0]["contents"]:
        if line["layout"] == "vertical":
            (label, value), codes = line["contents"][0]["contents"], line["contents"][1]["text"]
            rows[label["text"]] = f"{value['text']}（{codes}）"
        else:
            rows[line["contents"][0]["text"]] = line["contents"][1]["text"]
    return rows


def panel(card, title):
    """The lines of the detail panel under the rows with this title; None without it."""
    for box in card["contents"]["body"]["contents"]:
        texts = flex_texts(box)
        if texts and texts[0] == title:
            return texts[1:]
    return None


def with_blank(*codes):
    """The bot tests' roster (72, 甲, 26 and 5 on duty) with these codes' name cells blank."""
    return {**copy.deepcopy(RESULT), "names_blank": list(codes)}


class NameCellTests(unittest.TestCase):
    """``read_name_cells`` on a synthetic page: located boxes, some inked, some empty, one column not located."""

    def page(self):
        page = Image.new("L", (520, 70), 255)
        draw = ImageDraw.Draw(page)
        boxes = {"26": (390, 0, 510, 50),    # blank, given first: the result is in table order all the same
                 "1": (0, 0, 120, 50),       # a name that reads
                 "2": (130, 0, 250, 50),     # ink that no reading comes out of: unknown, not blank
                 "3": (260, 0, 380, 50)}     # only the printed rules around it: blank
        draw.rectangle((10, 15, 100, 35), fill=0)
        draw.rectangle((145, 15, 235, 35), fill=0)
        draw.rectangle(boxes["3"], outline=0, width=3)
        return page, boxes

    def read(self, page, boxes):
        from all_staff.name_table import read_name_cells, read_name_table
        with mock.patch("all_staff.name_table.shutil.which", return_value="tesseract"), \
             mock.patch("all_staff.name_table.warp_page", return_value=page), \
             mock.patch("all_staff.name_table.locate", return_value=(boxes, [])), \
             mock.patch("all_staff.name_table.read_names", return_value={"1": "林小明"}), \
             mock.patch("all_staff.name_table._read_name_cell", return_value=""):
            cells = read_name_cells(Path("photo.jpg"), None, directory=["林小明", "陳小華"])
            table = read_name_table(Path("photo.jpg"), None, directory=["林小明", "陳小華"])
        return cells, table

    def test_blank_cells_are_told_apart_from_unread_ones_in_table_order(self):
        page, boxes = self.page()
        (readings, blank), table = self.read(page, boxes)
        self.assertEqual(blank, ["3", "26"])          # 2 has ink: OCR missed it, so it is not blank
        self.assertEqual(list(readings), ["1"])
        self.assertEqual(table, readings)              # read_name_table: the readings, exactly as before
        self.assertNotIn("15", blank)                  # a code whose column was not located is unknown

    def test_a_faint_grey_name_is_not_blank(self):
        from all_staff.name_table import _blank, _has_name_ink
        page = Image.new("L", (300, 50), 236)          # an off-white sheet
        draw = ImageDraw.Draw(page)
        for x in range(12, 100, 9):                    # light-grey strokes of a printed name, lighter than grey 150
            draw.line((x, 15, x + 4, 35), fill=196, width=2)
        cell, empty = (0, 0, 120, 50), (150, 0, 270, 50)
        self.assertFalse(_has_name_ink(page.crop(cell)))   # what the first version took for blank
        self.assertFalse(_blank(page, cell))               # strokes against its own background: not blank
        self.assertTrue(_blank(page, empty))
        speck = page.copy()
        ImageDraw.Draw(speck).rectangle((200, 20, 205, 25), fill=120)   # a pen dot in an empty cell
        self.assertFalse(_blank(speck, empty))             # unsure: left to 「沒對到」, never called blank

    def test_a_degenerate_box_is_not_blank(self):
        page, boxes = self.page()
        boxes["4"] = (300, 10, 301, 40)                # a fit gone wrong: one pixel wide
        (_, blank), _ = self.read(page, boxes)
        self.assertNotIn("4", blank)

    def test_nothing_is_blank_when_the_table_is_not_read(self):
        from all_staff.name_table import read_name_cells
        with mock.patch("all_staff.name_table.shutil.which", return_value=None):
            self.assertEqual(read_name_cells(Path("photo.jpg"), None), ({}, []))
        with mock.patch("all_staff.name_table.shutil.which", return_value="tesseract"), \
             mock.patch("all_staff.name_table.warp_page", return_value=None):
            self.assertEqual(read_name_cells(Path("photo.jpg"), None), ({}, []))
        page, boxes = self.page()
        with mock.patch("all_staff.name_table.shutil.which", return_value="tesseract"), \
             mock.patch("all_staff.name_table.warp_page", return_value=page), \
             mock.patch("all_staff.name_table.locate", return_value=(boxes, [])), \
             mock.patch("all_staff.name_table.read_names", side_effect=RuntimeError("boom")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(read_name_cells(Path("photo.jpg"), None, directory=["林小明"]), ({}, []))
        with mock.patch("all_staff.name_table.shutil.which", return_value="tesseract"), \
             mock.patch("all_staff.name_table.warp_page", return_value=page), \
             mock.patch("all_staff.name_table.locate", return_value=({}, [])), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(read_name_cells(Path("photo.jpg"), None, directory=["林小明"]), ({}, []))


class UploadReportTests(unittest.TestCase):
    def test_a_blank_name_cell_is_not_counted_as_unmatched(self):
        card = upload_report("ok", "華華", SENT, roster=ROSTER, result=with_blank("26", "27"), names=NAMES)
        rows = report_rows(card)
        self.assertEqual(rows["👥 有勤務"], "4 個代號")
        self.assertEqual(rows["👤 姓名配對"], "3／3，全部對到")
        self.assertEqual(rows["⚠️ 沒有這個人"], "1 個（26）")         # 27 is blank too, but has no duty
        self.assertEqual(rows["⬜ 當天空白的代號"], "2 個（26、27）")   # every blank numeric cell, duties or not
        self.assertEqual(panel(card, "⚠️ 沒有這個人，卻讀到勤務")[:2], ["26（姓名欄空白）", "05–06 國際線、05–06 A1站"])
        texts = flex_texts(card["contents"])
        self.assertFalse(any("沒對到" in t or "辨識失敗" in t for t in texts))
        self.assertIn("姓名配對 3／3，全部對到，沒有這個人 1 個（26）", card["altText"])
        check_limits(self, card)

    def test_the_october_13_sheet(self):
        """47 names, and 26 whose cell is empty: 47／47, nothing failed."""
        codes = [code for code in ALLOWED_CODES if code in NAME_TABLE_CODES and code != "26"][:47] + ["26"]
        result = {"people": [{"code": code, "slots": [slot(4, 3, "國際線")]} for code in codes],
                  "names_blank": ["26", "寅", "卯", "E", "N"]}
        names = {code: f"某{code}" for code in codes if code != "26"}
        rows = report_rows(upload_report("ok", "華華", SENT, roster=ROSTER, result=result, names=names))
        self.assertEqual(rows["👥 有勤務"], "48 個代號")
        self.assertEqual(rows["👤 姓名配對"], "47／47，全部對到")
        self.assertEqual(rows["⚠️ 沒有這個人"], "1 個（26）")
        self.assertEqual(rows["⬜ 當天空白的代號"], "1 個（26）")     # 寅, 卯, E, N follow the settings

    def test_unread_and_blank_codes_are_listed_apart(self):
        names = {"72": "林小明", "甲": "陳小華"}
        card = upload_report("ok", "華華", SENT, roster=ROSTER, result=with_blank("26"), names=names)
        rows = report_rows(card)
        self.assertEqual(rows["👤 姓名配對"], "2／3（1 個沒讀出來）")
        self.assertEqual(rows["⚠️ 沒有這個人"], "1 個（26）")
        self.assertEqual(panel(card, "🔍 姓名沒讀出來")[0], "5：照片上讀到「某某某」")
        self.assertEqual(panel(card, "⚠️ 沒有這個人，卻讀到勤務")[0], "26（姓名欄空白）")
        check_limits(self, card)

    def test_a_name_wins_over_a_blank_cell(self):
        # a 「我是 26」 claim (or any name) on that code: it is matched, not nobody's
        rows = report_rows(upload_report("ok", "華華", SENT, roster=ROSTER, result=with_blank("26"),
                                         names={**NAMES, "26": "王大同"}))
        self.assertEqual(rows["👤 姓名配對"], "4／4，全部對到")
        self.assertNotIn("⚠️ 沒有這個人", rows)
        self.assertEqual(rows["⬜ 當天空白的代號"], "1 個（26）")   # the sheet's cell is blank all the same

    def test_without_blank_codes_the_report_is_as_before(self):
        blank = {}
        for result in (RESULT, with_blank()):
            card = upload_report("ok", "華華", SENT, roster=ROSTER, result=result, names=NAMES)
            rows = report_rows(card)
            self.assertEqual(rows["👤 姓名配對"], "3／4（1 個沒讀出來）")
            self.assertNotIn("⚠️ 沒有這個人", rows)
            self.assertEqual(panel(card, "🔍 姓名沒讀出來")[0], "26：照片上讀到「王大同」")
            blank["names_blank" in result] = rows.get("⬜ 當天空白的代號")
        self.assertEqual(blank, {False: None,     # read before blank cells: no row
                                 True: "沒有"})    # read, and no cell came out empty: worth a look

    def test_many_blank_codes_stay_short_and_within_limits(self):
        codes = [code for code in ALLOWED_CODES if code.isdigit() and code in NAME_TABLE_CODES][:40]
        result = {"people": [{"code": code, "slots": [slot(4, 3, "國際線", .5)]} for code in codes],
                  "names_blank": codes[:20]}
        names = {code: f"某{code}" for code in codes[30:]}
        card = upload_report("ok", "華華", SENT, roster=ROSTER, result=result, names=names, replaced=True, link=True)
        rows = report_rows(card)
        self.assertEqual(rows["👤 姓名配對"], "10／20（10 個沒讀出來）")
        listed = "、".join(codes[:REPORT_UNNAMED]) + " 等"
        self.assertEqual(rows["⚠️ 沒有這個人"], f"20 個（{listed}）")
        self.assertEqual(rows["⬜ 當天空白的代號"], f"20 個（{'、'.join(codes[:20])}）")
        check_limits(self, card)

    def test_unnamed_codes_are_in_code_order(self):
        result = {"people": [{"code": c, "slots": []} for c in ("甲", "26", "5", "A", "72", "辰", "40")],
                  "names_blank": ["A", "5"]}
        self.assertEqual(unnamed_codes(result, {"72": "林小明"}),
                         (["26"], {"5": "姓名欄空白", "40": "班表上沒有這個代號", "A": "固定代號沒有設定",
                                   "甲": "固定代號沒有設定", "辰": "班表上沒有這個代號"}))
        self.assertEqual(list(unnamed_codes(result, {"72": "林小明"})[1]), ["5", "40", "A", "甲", "辰"])
        self.assertEqual(unnamed_codes({"people": []}, None), ([], {}))


class PeopleCardTests(unittest.TestCase):
    """勤務人員, 查看時段 and 查看工作 call a blank cell's code 「空白」, not 「姓名未辨識」."""

    def texts(self, card):
        return "\n".join(flex_texts(card))

    def test_the_staff_card(self):
        names = {"72": "林小明", "甲": "陳小華", "5": "某某某"}
        card = self.texts(staff_card(ROSTER, with_blank("26"), names))
        self.assertIn("26 空白", card)
        self.assertIn(BLANK_NOTE, card)
        self.assertNotIn("未配對", card)
        self.assertNotIn("姓名未辨識", card)
        card = self.texts(staff_card(ROSTER, with_blank("26"), {"72": "林小明"}))   # 5 really missed
        self.assertIn("姓名未辨識 1 人", card)
        self.assertIn("3 人・1 人未配對", card)       # 數字代號: 5 missed, 26 blank, 72 named
        self.assertIn("26 空白", card)
        self.assertIn("甲 沒設定", card)              # no fixed code is set: nobody has 甲 today
        self.assertIn("⬜ 26（姓名欄空白）、甲（固定代號沒有設定）：當天沒有這個人，請對照原圖", card)
        before = self.texts(staff_card(ROSTER, RESULT, names))   # a result read before blank cells: as it was
        self.assertIn("姓名未辨識 1 人", before)
        self.assertNotIn("空白", before)

    def test_the_slot_and_work_cards(self):
        names = {"72": "林小明", "甲": "陳小華", "5": "某某某"}
        result = with_blank("26")
        for column, person in enumerate(result["people"], 2):   # everyone on 國際線 at 08:00: one hour holds all
            person["slots"] = [slot(3, column, "國際線")]
        for words in (self.texts(slot_card(result, 3, "intl", names, ROSTER)),
                      who_is_on(result, 3, "intl", names, ROSTER),
                      self.texts(work_card(ROSTER, result, names, "國際線"))):
            self.assertIn(BLANK_NOTE, words)
            self.assertNotIn("姓名未辨識", words)
            self.assertNotIn("沒辨識出來", words)
        del names["5"]                    # a real miss next to the blank cell: both are said
        words = self.texts(slot_card(result, 3, "intl", names, ROSTER))
        self.assertIn("🔤 只有代號：姓名未辨識", words)
        self.assertIn(BLANK_NOTE, words)


class GroupUploadTests(BotCase):
    """A group photo whose name table has a blank cell: the administrator's report and the server log."""
    friends = {"U1", "U2", "U9"}
    settings = {"admins": ("U9",), "notify": False}

    def test_the_report_and_the_log_tell_a_blank_cell_apart(self):
        self.register("U1", "林小明")
        self.analyzer.data["names"] = {"72": ["林小明"]}
        self.analyzer.data["names_blank"] = ["26"]
        self.transport.pushes.clear(); self.transport.raw_pushes.clear()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.bot.handle(image_event("U2", "Cgroup"))
        card = next(sent[0] for to, sent in self.transport.raw_pushes if to == "U9")
        rows = report_rows(card)
        self.assertEqual(rows["👤 姓名配對"], "1／2（1 個沒讀出來）")
        self.assertEqual(rows["⚠️ 沒有這個人"], "2 個（26、甲）")   # 甲: no fixed code is set here
        check_limits(self, card)
        log = out.getvalue()
        self.assertIn("班表 run-1：1 個有勤務的代號沒有對到姓名：5", log)
        self.assertIn("班表 run-1：2 個有勤務的代號當天沒有這個人（勤務格的代號可能讀錯）："
                      "26（姓名欄空白）、甲（固定代號沒有設定）", log)

    def test_a_failed_reread_drops_the_old_blank_codes_with_the_names(self):
        self.analyzer.data.update(names_version=0, names_blank=["26"])
        self.analyzer.ensure_names = lambda run_id: (_ for _ in ()).throw(OSError("photo unavailable"))
        roster = {"run_id": "run-1", "roster_date": "2026-10-01"}
        with contextlib.redirect_stdout(io.StringIO()):
            self.bot.names_for(roster, self.analyzer.data)
        self.assertEqual(self.analyzer.data["names"], {})
        self.assertNotIn("names_blank", self.analyzer.data)

    def test_a_reread_brings_the_blank_codes(self):
        from all_staff.name_table import NAMES_VERSION
        self.analyzer.data["names_version"] = 0
        self.analyzer.ensure_names = lambda run_id: {"names": {"72": ["林小明"]}, "names_blank": ["26"],
                                                     "names_version": NAMES_VERSION}
        self.bot.names_for({"run_id": "run-1", "roster_date": "2026-10-01"}, self.analyzer.data)
        self.assertEqual(unnamed_codes(self.analyzer.data, {"72": "林小明"}),
                         (["5"], {"26": "姓名欄空白", "甲": "固定代號沒有設定"}))


def demo_app(runs: Path):
    from all_staff.demo import DemoApp
    app = DemoApp.__new__(DemoApp)
    app.template = Path(__file__).resolve().parent / "configs/all_staff_roster_template.local.json"
    app.runs, app.lock = runs, threading.Lock()
    return app


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buffer, "PNG")
    return buffer.getvalue()


class DemoTests(unittest.TestCase):
    """The run keeps the blank codes beside the readings, when first read and when read again."""

    def test_a_new_upload_keeps_the_blank_codes(self):
        sheet = {"cells": [{"pred": [{"code": "1", "conf": .9}] * 20}]}
        with tempfile.TemporaryDirectory() as folder:
            app = demo_app(Path(folder))
            app._pool = ThreadPoolExecutor(1)
            app.context, app.engine = {"fx": .22, "fy": .12}, object()
            app.recognizer = app.roster = app.root = None
            try:
                with mock.patch.object(app, "_has_table", return_value=True), \
                        mock.patch("all_staff.context.prepare_context", return_value={}), \
                        mock.patch("all_staff.schema.load_annotations", return_value=[object()]), \
                        mock.patch("all_staff.demo.sheet_payload", side_effect=lambda *a, **k: copy.deepcopy(sheet)), \
                        mock.patch("all_staff.demo.common_payload", return_value={}), \
                        mock.patch("all_staff.demo.people", return_value=[]), \
                        mock.patch.object(app, "_roster_date", return_value="2026-10-13"), \
                        mock.patch.object(app, "_name_table", return_value=({"1": ["施志明"]}, ["26"])):
                    data = app.analyze(png(), "x.png")
            finally:
                app._pool.shutdown()
            saved = json.loads((Path(folder) / data["run"]["id"] / "result.json").read_text(encoding="utf-8"))
            self.assertEqual((saved["names"], saved["names_blank"]), ({"1": ["施志明"]}, ["26"]))

    def test_name_table_failure_reads_as_no_names_and_no_blanks(self):
        with tempfile.TemporaryDirectory() as folder:
            app = demo_app(Path(folder))
            with mock.patch("all_staff.name_table.read_name_cells", side_effect=RuntimeError("boom")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app._name_table(Path("photo.jpg")), ({}, []))

    def test_a_current_run_saved_before_blank_cells_is_not_read_again(self):
        from all_staff.name_table import NAMES_VERSION
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "old"
            run.mkdir()
            (run / "result.json").write_text(json.dumps({"names": {"1": ["施志明"]}, "names_version": NAMES_VERSION}))
            app = demo_app(Path(folder))
            app._name_table = lambda photo: self.fail("read again")
            self.assertEqual(app.ensure_names("old"), {"names": {"1": ["施志明"]}, "names_blank": [],
                                                       "names_version": NAMES_VERSION})


class RosterCheckTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        Store(self.root / "bot.sqlite3").add_roster("Cgroup", "run-1", "U1", 4, "2026-10-01")
        (self.root / "runs/run-1/upload").mkdir(parents=True)
        (self.root / "staff_names.txt").write_text("林小明\n陳小華\n", encoding="utf-8")

    def tearDown(self):
        self.folder.cleanup()

    def check(self, data, **options):
        from all_staff.roster_check import check_rosters
        (self.root / "runs/run-1/result.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        lines = []
        with mock.patch("backend.phase1_cell_pipeline.GridTemplate.from_json", lambda path: None):
            report = check_rosters(self.root, Path("t.json"), self.root / "staff_names.txt", out=lines.append,
                                   **options)
        return report[0], lines[0]

    def test_blank_cells_are_listed_apart_from_unread_ones(self):
        from all_staff.name_table import NAMES_VERSION
        report, line = self.check({**with_blank("26"), "names_version": NAMES_VERSION})
        self.assertEqual((report["unread"], report["nobody"]), (["5"], {"26": "姓名欄空白"}))
        self.assertIn("有勤務沒對到：5", line)
        self.assertIn("沒有這個人：26（姓名欄空白）", line)

    def test_a_reread_saves_the_blank_codes(self):
        (self.root / "runs/run-1/upload/photo.jpg").write_bytes(b"photo")
        page = Image.new("L", (10, 10), 255)
        with mock.patch("all_staff.roster_check.warp_page", return_value=page), \
                mock.patch("all_staff.roster_check.locate", return_value=({}, [])), \
                mock.patch("all_staff.roster_check.read_name_cells",
                           return_value=({"72": ["林小明"], "甲": ["陳小華"]}, ["5", "26"])):
            report, line = self.check(copy.deepcopy(RESULT), reread=True)
        saved = json.loads((self.root / "runs/run-1/result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["names_blank"], ["5", "26"])
        self.assertEqual((report["unread"], report["nobody"]), ([], {"5": "姓名欄空白", "26": "姓名欄空白"}))
        self.assertNotIn("有勤務沒對到", line)
        self.assertIn("沒有這個人：5、26（姓名欄空白）", line)


if __name__ == "__main__":
    unittest.main()
