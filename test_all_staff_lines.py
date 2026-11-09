"""Tests for the V2 context crops, line segmenter and line recogniser plumbing."""
import random
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from all_staff.context import _offset, core_box, load_context, migrate_annotations
from all_staff.schema import Annotation, Box, CellAnnotation, load_annotations, save_annotations
from all_staff.segment import parse_codes, segment_lines, split_line


def _font(size):
    for path in ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
                 "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
                 "C:/Windows/Fonts/times.ttf", "/mnt/c/Windows/Fonts/times.ttf"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def ruled_cell(lines: list[str], size=(300, 260), core=(66, 30, 234, 230)) -> Image.Image:
    """A context crop: core cell with thick rules plus neighbour margins."""
    image = Image.new("L", size, 205); draw = ImageDraw.Draw(image)
    for x in (core[0], core[2]):
        draw.rectangle((x - 4, 0, x + 4, size[1]), fill=20)
    for y in (core[1], core[3]):
        draw.rectangle((0, y - 3, size[0], y + 3), fill=20)
    font = _font(56); y = core[3] - 70 * len(lines) - 5
    for text in lines:
        draw.text((core[0] + 30, y), text, fill=25, font=font); y += 70
    draw.text((core[2] + 20, core[3] - 70), "57", fill=25, font=font)  # neighbour's code
    return image


class ParseTests(unittest.TestCase):
    def test_parse_codes(self):
        self.assertEqual(parse_codes("72"), ["72"])
        self.assertEqual(parse_codes("#72"), ["72"])
        self.assertEqual(parse_codes("IB"), ["I", "B"])
        self.assertEqual(parse_codes("甲戊"), ["甲", "戊"])
        self.assertEqual(parse_codes(""), [])


class ContextTests(unittest.TestCase):
    def test_core_box_inverts_margin(self):
        self.assertEqual(core_box(144, 124, {"fx": .22, "fy": .12}), (22, 12, 122, 112))
        self.assertEqual(core_box(100, 80, {"fx": 0, "fy": 0}), (0, 0, 100, 80))

    def test_legacy_dataset_has_no_context(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(load_context(Path(folder)), {"fx": 0.0, "fy": 0.0})

    def test_prepare_refuses_empty_photo_folder_without_writing(self):
        from all_staff.context import prepare_context
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(FileNotFoundError):
                prepare_context(root/"none", root/"ctx", root/"template.json")
            self.assertFalse((root/"ctx").exists())

    def test_template_match_finds_old_crop(self):
        rng = np.random.default_rng(0)
        big = rng.integers(0, 255, (120, 90), dtype=np.uint8)
        dx, dy, score = _offset(big[17:97, 11:71].copy(), big)
        self.assertEqual((dx, dy), (11, 17)); self.assertGreater(score, .99)

    def test_migration_shifts_boxes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); old, new = root/"old", root/"new"
            for path in (old/"images", new/"images"): path.mkdir(parents=True)
            rng = np.random.default_rng(1)
            big = rng.integers(0, 255, (130, 100), dtype=np.uint8)
            Image.fromarray(big).save(new/"images/a.png")
            Image.fromarray(big[20:110, 15:85]).save(old/"images/a.png")
            base = dict(sample_id="a", image="images/a.png", source_group="g", row=4, column=2)
            save_annotations(old/"annotations.jsonl", [CellAnnotation(
                **base, width=70, height=90, reviewed=True,
                annotations=(Annotation(Box(5, 50, 40, 80), "72"),))])
            save_annotations(new/"annotations.jsonl", [CellAnnotation(
                **base, width=100, height=130, reviewed=False)])
            report = migrate_annotations(old, new)
            item = load_annotations(new/"annotations.jsonl")[0]
        self.assertEqual(report["migrated"], 1); self.assertTrue(item.reviewed)
        self.assertEqual(item.annotations[0].box, Box(20, 70, 55, 100))


class SegmentTests(unittest.TestCase):
    def test_blank_ruled_cell_has_no_lines(self):
        self.assertEqual(segment_lines(ruled_cell([]), (66, 30, 234, 230)), [])

    def test_stacked_lines_are_separate_and_neighbour_is_ignored(self):
        lines = segment_lines(ruled_cell(["79", "68"]), (66, 30, 234, 230))
        self.assertEqual(len(lines), 2)
        self.assertLess(lines[0].box.y2, lines[1].box.y2)
        for line in lines:
            self.assertLess(line.box.x2, 250)  # the neighbour's 57 is not attached

    def test_side_by_side_letters_split_into_two_boxes(self):
        [line] = segment_lines(ruled_cell(["I  B"]), (66, 30, 234, 230))
        left, right = split_line(line, ["I", "B"])
        self.assertLessEqual(left.x2, right.x1 + 6)


def synthetic_sheet(slant=.02, shift=(12, 10), cut_left=False):
    """A ruled roster: wide time column, 36 duty rules, 代號 row, 19 hours, legend rows.

    The drawn table has the real 38 rules (the left edge of c02 … the right edge of c36), but its template —
    like the production one — knows only the first 37 of them.
    """
    width, height = 1100, 1500
    xs = [40, 70] + [100 + 28 * (k - 2) for k in range(2, 38)]
    ys = [20, 70, 95, 230, 255] + [255 + 40 * (k - 4) for k in range(5, 24)] + \
         [1015 + 20 * (k - 23) for k in range(24, 45)]
    image = Image.new("L", (width, height), 235); draw = ImageDraw.Draw(image)
    true_x = lambda k, y: xs[k] + slant * (y - 700)
    for k in range(len(xs)):
        if k == 1:
            continue  # the time column is merged in the body
        if cut_left and k == 0:
            continue
        top = ys[3] if k else ys[0]
        draw.line([(true_x(k, top), top), (true_x(k, ys[-1]), ys[-1])], fill=15, width=2)
    for k, y in enumerate(ys):
        draw.line([(true_x(0, y), y), (true_x(len(xs) - 1, y), y)], fill=15, width=2)
    grid_x = [(x + shift[0]) / width for x in xs[:37]]; grid_y = [(y + shift[1]) / height for y in ys]
    return image.convert("RGB"), grid_x, grid_y, true_x, ys


class GridTests(unittest.TestCase):
    def check(self, **kwargs):
        from all_staff.grid import register
        image, grid_x, grid_y, true_x, ys = synthetic_sheet(**kwargs)
        reg = register(image, grid_x, grid_y, rows=(4, 22), columns=(2, 36))
        for k in range(2, 38):   # every rule of c02..c36, the last four columns included
            for y in (300, 700, 1000):
                self.assertAlmostEqual(reg.x_at(k, y), true_x(k, y), delta=2.5, msg=f"rule {k} y {y}")
        for k in range(4, 24):
            self.assertAlmostEqual(reg.y_at(k, 500), ys[k], delta=2.5, msg=f"row rule {k}")
        return reg

    def test_slanted_sheet_keeps_column_identity(self):
        self.check()

    def test_offset_template_does_not_shift_columns(self):
        self.check(shift=(-26, 28), slant=-.015)

    def test_time_column_cut_off_by_photo_edge(self):
        self.check(cut_left=True)

    def test_last_columns_beyond_the_template_are_registered(self):
        """C33-C36 (A1A3站, 內機, 內部管理, 全區帶勤) lie right of the template's reliable rules."""
        reg = self.check()
        for column in (33, 34, 35, 36):
            left, right = reg.corner(column, 4), reg.corner(column + 1, 4)
            self.assertAlmostEqual(right[0] - left[0], 28, delta=3, msg=f"column {column} width")
            self.assertAlmostEqual(reg.corner(column, 23)[1] - left[1], 19 * 40, delta=4, msg=f"column {column} height")

    def test_context_crop_is_upright_and_contains_core(self):
        from all_staff.grid import context_crop
        from all_staff.context import core_box
        reg = self.check()
        crop, quad = context_crop(reg, 10, 15, .22, .12)
        x1, y1, x2, y2 = core_box(crop.width, crop.height, {"fx": .22, "fy": .12})
        gray = np.asarray(crop.convert("L"))
        # the cell's own left/right rules sit on the core edges
        self.assertLess(gray[:, x1 - 6:x1 + 6].mean(), gray[:, (x1 + x2) // 2].mean() - 20)
        self.assertLess(gray[:, x2 - 6:x2 + 6].mean(), gray[:, (x1 + x2) // 2].mean() - 20)


class MonitorTests(unittest.TestCase):
    def test_status_and_stop_endpoints(self):
        import json as _json
        import socket
        from urllib.request import Request, urlopen
        from all_staff.monitor import start
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "recognizer.json"
            server = start(run, port=port, open_browser=False)
            try:
                base = f"http://127.0.0.1:{port}"
                self.assertEqual(_json.load(urlopen(base + "/api/status"))["status"], "waiting")
                run.write_text(_json.dumps({"status": "running", "progress": {"global_step": 3}}))
                payload = _json.load(urlopen(base + "/api/status"))
                self.assertEqual(payload["progress"]["global_step"], 3); self.assertFalse(payload["stop_requested"])
                urlopen(Request(base + "/api/stop", data=b"", method="POST"))
                self.assertTrue(run.with_suffix(".stop").exists())
                self.assertIn("訓練監控", urlopen(base + "/").read().decode())
            finally:
                server.shutdown()


class ViewerTests(unittest.TestCase):
    def test_summary_counts_cell_level_matches(self):
        from all_staff.viewer import _summary
        cells = [{"pred": [{"code": "72"}], "truth": [{"code": "72", "role": "personnel_code"}]},
                 {"pred": [{"code": "4"}], "truth": [{"code": "H", "role": "personnel_code"}]},
                 {"pred": [{"code": "57"}]}]
        result = _summary([{"cells": cells}])
        self.assertEqual(result["labelled_cells"], 2); self.assertEqual(result["labelled_exact"], .5)
        self.assertEqual(result["precision"], .5); self.assertEqual(result["predicted_codes"], 3)

    def test_grid_warning_flags_squeezed_rows(self):
        from all_staff.viewer import grid_warnings
        cells = [{"r": r, "core": [0, 0, 100, 40 if r == 22 else 150]} for r in range(4, 23)]
        [warning] = grid_warnings(cells)
        self.assertIn("23-24", warning)
        self.assertEqual(grid_warnings([{"r": r, "core": [0, 0, 100, 150]} for r in range(4, 23)]), [])

    def test_people_lists_slots_in_time_order(self):
        from all_staff.viewer import people
        data = {"allowed": ["1", "72", "A"], "time_rows": {"4": "05-06", "5": "06-07"},
                "column_names": {"2": "國際線"}, "roster": {"72": "某人"}}
        sheet = {"cells": [{"r": 5, "c": 2, "pred": [{"code": "72", "conf": .9}]},
                           {"r": 4, "c": 3, "pred": [{"code": "72", "conf": .5}, {"code": "1", "conf": .99}]}]}
        result = people(sheet, data)
        self.assertEqual([p["code"] for p in result], ["1", "72"])
        self.assertEqual([(s["time"], s["duty"]) for s in result[1]["slots"]], [("05-06", "c03"), ("06-07", "國際線")])
        self.assertTrue(result[1]["needs_review"]); self.assertEqual(result[1]["name"], "某人")

    def test_demo_rejects_non_image_upload(self):
        from all_staff.demo import DemoApp, DemoError
        app = DemoApp.__new__(DemoApp)
        with self.assertRaises(DemoError):
            app.analyze(b"not an image", "x.jpg")

    def test_page_ships_with_package(self):
        page = (Path(__file__).parent / "all_staff/viewer_page.html").read_text(encoding="utf-8")
        self.assertIn('src="data.js"', page); self.assertIn("prefers-color-scheme:dark", page)

    def test_touching_glyphs_split_evenly(self):
        from all_staff.segment import Line
        line = Line(Box(0, 0, 100, 40), (Box(5, 5, 95, 35),))
        self.assertEqual(split_line(line, ["K", "I"]), [Box(0, 0, 50, 40), Box(50, 0, 100, 40)])


class LineTensorTests(unittest.TestCase):
    def test_keeps_aspect_and_pads(self):
        from all_staff.lines import line_tensor
        tensor = line_tensor(Image.new("L", (40, 80), 255))
        self.assertEqual(tuple(tensor.shape), (1, 32, 128))
        self.assertEqual(float(tensor[0, :, 20:].abs().sum()), 0.0)

    def test_synthetic_line_renders(self):
        try:
            from all_staff.synth import font_paths, random_line, render
            font_paths()
        except RuntimeError:
            self.skipTest("no fonts for synthesis")
        rng = random.Random(0)
        for _ in range(20):
            text, label = random_line(rng)
            image = render(text, rng, [np.full((80, 80), 200, np.uint8)])
            self.assertGreater(image.width, 10); self.assertTrue(label == "" or label in text)


if __name__ == "__main__":
    unittest.main()
