"""Notes and call-out boxes laid over the roster grid must not be read as duties."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image, ImageDraw

from all_staff import overlays
from all_staff.overlays import Box, Overlays
from test_all_staff_lines import _font, synthetic_sheet

ROWS, COLUMNS = (4, 22), (2, 36)


def table(*, boxes=(), erase=(), stripes=()):
    """A registered synthetic roster.

    ``boxes``   (rows, columns): merged-cell notes — the rules inside are painted away, the frame stays;
    ``erase``   (row, rule) vertical / ("h", rule, column) horizontal single segments that went missing;
    ``stripes`` (column, top rule, bottom rule): a narrow opaque strip across a column (a pointer's wedge).
    """
    from all_staff.grid import register
    image, grid_x, grid_y, true_x, ys = synthetic_sheet()
    draw = ImageDraw.Draw(image)
    for (r0, r1), (c0, c1) in boxes:
        top, bottom = ys[r0] + 4, ys[r1 + 1] - 4
        draw.polygon([(true_x(c0, top) + 4, top), (true_x(c1 + 1, top) - 4, top),
                      (true_x(c1 + 1, bottom) - 4, bottom), (true_x(c0, bottom) + 4, bottom)], fill=(235, 235, 235))
        draw.text((true_x(c0, top) + 8, top + 6), "72 73 AB", fill=(25, 25, 25), font=_font(14))
    for item in erase:
        if item[0] == "h":
            _, rule, column = item
            y = ys[rule]
            draw.rectangle((true_x(column, y) + 4, y - 3, true_x(column + 1, y) - 4, y + 3), fill=(235, 235, 235))
        else:
            row, rule = item
            top, bottom = ys[row] + 4, ys[row + 1] - 4
            draw.polygon([(true_x(rule, top) - 4, top), (true_x(rule, top) + 4, top),
                          (true_x(rule, bottom) + 4, bottom), (true_x(rule, bottom) - 4, bottom)], fill=(235, 235, 235))
    for column, first, last in stripes:
        x0 = true_x(column, ys[first]) + 14
        draw.polygon([(x0 - 5, ys[first] - 3), (x0 + 5, ys[first] - 3), (x0 + 5, ys[last] + 3), (x0 - 5, ys[last] + 3)],
                     fill=(235, 235, 235))
    registration = register(image, grid_x, grid_y, rows=ROWS, columns=COLUMNS)
    return registration


class DetectionTests(unittest.TestCase):
    def detect(self, **kwargs):
        return overlays.detect(table(**kwargs), rows=ROWS, columns=COLUMNS)

    def test_a_plain_table_has_no_overlay(self):
        found = self.detect()
        self.assertEqual(found.boxes, [])
        self.assertEqual(found.pointer, set())
        self.assertFalse(found.unreliable)
        self.assertEqual(found.summary(), "none")

    def test_merged_note_is_found_where_it_lies(self):
        found = self.detect(boxes=[((10, 10), (5, 8))])
        self.assertEqual(found.boxes, [Box((10, 10), (5, 8))])
        self.assertTrue(found.in_box(10, 5) and found.in_box(10, 8))
        self.assertFalse(found.in_box(10, 9) or found.in_box(9, 6) or found.in_box(11, 6))

    def test_boxes_over_several_rows_and_two_notes_at_once(self):
        found = self.detect(boxes=[((12, 14), (20, 23)), ((7, 7), (28, 31))])
        self.assertEqual(sorted(found.boxes, key=lambda b: b.rows),
                         [Box((7, 7), (28, 31)), Box((12, 14), (20, 23))])

    def test_the_new_right_hand_columns_are_covered_too(self):
        self.assertEqual(self.detect(boxes=[((9, 9), (33, 36))]).boxes, [Box((9, 9), (33, 36))])

    def test_a_small_label_with_a_code_or_two_is_not_an_overlay(self):
        # a little white label on two or three cells hides a few rules but holds real codes (子丙, 乙寅)
        self.assertEqual(self.detect(boxes=[((10, 10), (5, 6))]).boxes, [])
        self.assertEqual(self.detect(boxes=[((10, 10), (5, 7))]).boxes, [])

    def test_a_single_column_strip_is_not_an_overlay(self):
        self.assertEqual(self.detect(boxes=[((10, 13), (6, 6))]).boxes, [])

    def test_a_few_faded_rule_segments_are_not_an_overlay(self):
        found = self.detect(erase=[(8, 12), (12, 20), ("h", 9, 15), ("h", 15, 6), (17, 30), ("h", 20, 27)])
        self.assertEqual(found.boxes, [])
        self.assertFalse(found.unreliable)

    def test_a_pointer_marks_the_cells_it_covers_and_the_one_it_points_at(self):
        found = self.detect(boxes=[((10, 10), (5, 8))], stripes=[(6, 8, 10)])
        self.assertEqual(found.boxes, [Box((10, 10), (5, 8))])
        self.assertTrue({(9, 6), (8, 6), (7, 6)} <= found.pointer, found.pointer)
        self.assertIn((6, 6), found.pointer)          # the cell the tip touches
        self.assertFalse(found.pointer & found.boxes[0].cells())
        self.assertNotIn((10, 9), found.pointer)

    def test_no_pointer_without_a_box(self):
        self.assertEqual(self.detect(stripes=[(6, 8, 10)]).pointer, set())

    def test_lost_ruling_all_over_the_table_disables_the_filter(self):
        # a registration that is off (or a photo too blurred to show the rules) must never throw readings away
        many = [(row, rule) for row in range(6, 10) for rule in range(3, 36)]
        found = self.detect(erase=many)
        self.assertTrue(found.unreliable)
        self.assertEqual(found.boxes, [])
        self.assertGreater(found.open_share, overlays.UNRELIABLE_SHARE)


class ApplyTests(unittest.TestCase):
    def cell(self, row, column, *predictions):
        return {"r": row, "c": column, "pred": [{"code": code, "conf": conf} for code, conf in predictions]}

    def test_everything_read_inside_a_box_is_set_aside(self):
        found = Overlays(boxes=[Box((19, 19), (5, 8))])
        cell = self.cell(19, 7, ("A", .89), ("癸", .89))
        self.assertTrue(found.apply(cell))
        self.assertEqual(cell["pred"], [])
        self.assertEqual([p["code"] for p in cell["ignored"]], ["A", "癸"])
        self.assertEqual(cell["overlay"], "box")

    def test_a_pointer_cell_only_loses_the_readings_it_is_unsure_of(self):
        found = Overlays(boxes=[Box((19, 19), (5, 8))], pointer={(16, 5)})
        cell = self.cell(16, 5, ("A", .52), ("79", .95))
        self.assertTrue(found.apply(cell))
        self.assertEqual([p["code"] for p in cell["pred"]], ["79"])
        self.assertEqual([p["code"] for p in cell["ignored"]], ["A"])
        self.assertEqual(cell["overlay"], "pointer")

    def test_a_pointer_cell_with_a_sure_reading_is_marked_but_not_changed(self):
        found = Overlays(pointer={(16, 5)})
        cell = self.cell(16, 5, ("79", .97))
        self.assertFalse(found.apply(cell))
        self.assertEqual(len(cell["pred"]), 1)
        self.assertNotIn("ignored", cell)

    def test_other_cells_are_left_alone(self):
        found = Overlays(boxes=[Box((19, 19), (5, 8))], pointer={(16, 5)})
        cell = self.cell(19, 9, ("72", .99))
        self.assertFalse(found.apply(cell))
        self.assertEqual(cell, self.cell(19, 9, ("72", .99)))


class StorageTests(unittest.TestCase):
    def test_round_trip_and_missing_file(self):
        found = Overlays(boxes=[Box((19, 19), (5, 8)), Box((12, 14), (20, 23))],
                         pointer={(16, 5), (16, 6)}, open_share=.0123)
        with tempfile.TemporaryDirectory() as folder:
            dataset = Path(folder)
            overlays.save(found, dataset / "overlays" / "0123456789abcdef.json")
            loaded = overlays.load(dataset, "0123456789abcdef" + "0" * 48)   # a full sha256 names the same file
            self.assertEqual(loaded.boxes, found.boxes)
            self.assertEqual(loaded.pointer, found.pointer)
            self.assertFalse(loaded.unreliable)
            self.assertIsNone(overlays.load(dataset, "ffffffffffffffff"))     # older run: nothing recorded
            (dataset / "overlays" / "broken000000000.json").write_text("{not json", encoding="utf-8")
            self.assertIsNone(overlays.load(dataset, "broken000000000"))

    def test_summary_names_the_boxes(self):
        text = Overlays(boxes=[Box((19, 19), (5, 8))]).summary()
        self.assertIn("5", text); self.assertIn("19", text)


class ViewerTests(unittest.TestCase):
    """sheet_payload applies the stored overlays to what the line reader found."""

    def items(self, dataset, group):
        images = dataset / "images"; images.mkdir(parents=True)
        items = []
        for row, column in ((19, 6), (19, 12), (16, 5)):
            Image.new("L", (20, 20), 255).save(images / f"{row}_{column}.png")
            items.append(SimpleNamespace(sample_id=f"{row}_{column}", row=row, column=column, image=f"images/{row}_{column}.png",
                                         source_group=group, split="", reviewed=False))
        return items

    def payload(self, dataset, group):
        from all_staff.viewer import sheet_payload

        def fake(item, image, engine, context, url):
            codes = {(19, 6): [("A", .9)], (19, 12): [("72", .99)], (16, 5): [("A", .5), ("79", .96)]}[(item.row, item.column)]
            return {"id": item.sample_id, "r": item.row, "c": item.column, "img": url, "w": 20, "h": 20,
                    "core": [0, 0, 20, 20], "lines": [],
                    "pred": [{"code": code, "box": [0, 0, 1, 1], "conf": conf, "line": 0} for code, conf in codes]}
        with mock.patch("all_staff.viewer.cell_payload", side_effect=fake):
            return sheet_payload(dataset, self.items(dataset, group), None, {"fx": 0, "fy": 0}, lambda path: path.name,
                                 name="x.jpg", overview=None)

    def test_overlay_readings_do_not_reach_the_people_list(self):
        group = "0123456789abcdef" + "0" * 48
        with tempfile.TemporaryDirectory() as folder:
            dataset = Path(folder)
            overlays.save(Overlays(boxes=[Box((19, 19), (5, 8))], pointer={(16, 5)}),
                          dataset / "overlays" / f"{group[:16]}.json")
            payload = self.payload(dataset, group)
        cells = {(c["r"], c["c"]): c for c in payload["cells"]}
        self.assertEqual(cells[(19, 6)]["pred"], [])
        self.assertEqual([p["code"] for p in cells[(19, 6)]["ignored"]], ["A"])
        self.assertEqual([p["code"] for p in cells[(19, 12)]["pred"]], ["72"])       # outside every overlay
        self.assertEqual([p["code"] for p in cells[(16, 5)]["pred"]], ["79"])        # unsure "A" at the tip dropped
        self.assertEqual(payload["overlays"]["boxes"], [{"rows": [19, 19], "columns": [5, 8]}])

    def test_runs_without_recorded_overlays_are_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = self.payload(Path(folder), "0123456789abcdef")
        self.assertNotIn("overlays", payload)
        self.assertTrue(all("ignored" not in c and "overlay" not in c for c in payload["cells"]))
        self.assertEqual(sorted(p["code"] for c in payload["cells"] for p in c["pred"]), ["72", "79", "A", "A"])


class ColumnNameTests(unittest.TestCase):
    def test_last_four_columns_have_their_printed_names(self):
        from all_staff.viewer import column_names
        names = column_names(Path(__file__).parent)
        self.assertEqual([names.get(str(c)) for c in (31, 32, 33, 34, 35, 36)],
                         ["A1站", "A1A3站帶勤", "A1A3站", "內機", "內部管理", "全區帶勤"])


if __name__ == "__main__":
    unittest.main()
