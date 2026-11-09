import json
import tempfile
import unittest
from pathlib import Path

from all_staff.schema import (ALLOWED_CODES, CHARSET, Annotation, Box, CellAnnotation,
                              load_annotations, save_annotations)
from all_staff.evaluate import _iou
from all_staff.dataset import FULL_DUTY_COLUMNS, FULL_TIME_ROWS, full_grid_template
from all_staff.auto_label import candidate_boxes
from all_staff.annotate import _clamped_box
from PIL import Image


class AllStaffSchemaTests(unittest.TestCase):
    def sample(self, **changes):
        payload = dict(sample_id="sheet_r01_c02", image="images/x.png",
                       source_group="same-sheet", row=1, column=2,
                       width=100, height=60, reviewed=True,
                       annotations=(Annotation(Box(3, 4, 30, 40), "57"),))
        payload.update(changes)
        return CellAnnotation(**payload)

    def test_allowed_codes_cover_requested_roster(self):
        self.assertIn("1", ALLOWED_CODES); self.assertIn("84", ALLOWED_CODES)
        self.assertIn("A", ALLOWED_CODES); self.assertIn("N", ALLOWED_CODES)
        self.assertIn("甲", ALLOWED_CODES); self.assertIn("癸", ALLOWED_CODES)
        self.assertNotIn("85", ALLOWED_CODES); self.assertNotIn("O", ALLOWED_CODES)

    def test_chinese_codes_through_you_can_be_saved_and_encoded(self):
        for code in "子丑寅卯辰巳午未申酉":
            with self.subTest(code=code):
                self.sample(annotations=(Annotation(Box(1, 1, 10, 10), code),)).validate()
                self.assertIn(code, CHARSET)
        self.assertNotIn("戌", ALLOWED_CODES)

    def test_rejects_bad_personnel_code_and_box(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.sample(annotations=(Annotation(Box(1, 1, 10, 10), "85"),)).validate()
        with self.assertRaisesRegex(ValueError, "invalid box"):
            self.sample(annotations=(Annotation(Box(1, 1, 110, 10), "1"),)).validate()

    def test_split_is_stable_per_source_photo(self):
        first = self.sample(sample_id="a")
        second = self.sample(sample_id="b", row=9)
        self.assertEqual(first.split, second.split)

    def test_jsonl_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"annotations.jsonl"
            save_annotations(path, [self.sample()])
            loaded = load_annotations(path)
        self.assertEqual(loaded, [self.sample()])
        payload = loaded[0].to_dict()
        self.assertIn(payload["split"], {"train", "val", "test"})

    def test_machine_source_and_confidence_round_trip(self):
        machine = self.sample(annotations=(Annotation(Box(3, 4, 30, 40), "57",
                                                      source="tesseract", confidence=.91),))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"annotations.jsonl"
            save_annotations(path, [machine])
            loaded = load_annotations(path)[0]
        self.assertEqual(loaded.annotations[0].source, "tesseract")
        self.assertEqual(loaded.annotations[0].confidence, .91)

    def test_blank_cell_has_no_automatic_candidate(self):
        self.assertEqual(candidate_boxes(Image.new("L", (140, 210), 255)), [])

    def test_save_clamps_small_box_overflow_to_current_image(self):
        box = _clamped_box({"x1": 80, "y1": 118, "x2": 143, "y2": 193}, 140, 195)
        self.assertEqual(box, Box(80, 118, 140, 193))

    def test_annotation_page_is_dark_and_keyboard_first(self):
        page = (Path(__file__).parent/"all_staff/annotation_page.html").read_text()
        self.assertIn("--black:#303236", page)
        self.assertIn('id="quickCode"', page)
        self.assertIn("quick.focus();quick.select()", page)
        self.assertIn("直接輸入人員代號", page)
        self.assertNotIn("rows.splice(i,1)", page)

    def test_seed_reset_preserves_annotations_and_backup(self):
        from all_staff.prepare_seed import prepare_seed
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "images").mkdir()
            items = [self.sample(sample_id=f"s{n}", column=n) for n in range(4)]
            items[1] = self.sample(sample_id="s1", column=1,
                                  annotations=(Annotation(Box(1, 1, 10, 10), "", "unknown"),))
            save_annotations(root / "annotations.jsonl", items)
            original = (root / "annotations.jsonl").read_bytes()
            report = prepare_seed(root, keep=3)
            actual = load_annotations(root / "annotations.jsonl")
            self.assertEqual([item.reviewed for item in actual], [True, False, True, False])
            self.assertEqual([item.annotations for item in actual], [item.annotations for item in items])
            self.assertEqual(Path(report["backup"]).read_bytes(), original)
            seed = load_annotations(Path(report["training_snapshot"]) / "annotations.jsonl")
            self.assertEqual([item.sample_id for item in seed], ["s0", "s2"])

    def test_model_merge_preserves_manual_and_concurrent_edits(self):
        from dataclasses import replace
        from all_staff.annotate import merge_model_proposals
        original = self.sample(reviewed=False, annotations=())
        proposal = {"sample_id": original.sample_id, "expected": original.to_dict(),
                    "annotations": [{"box": {"x1": 1, "y1": 1, "x2": 10, "y2": 10},
                                     "text": "28", "role": "personnel_code", "confidence": .7}]}
        proposal = json.loads(json.dumps(proposal))
        result, count = merge_model_proposals([original], [proposal])
        self.assertEqual(count, 1); self.assertFalse(result[0].reviewed)
        self.assertEqual(result[0].annotations[0].source, "model")
        for current in [replace(original, reviewed=True), self.sample(reviewed=False),
                        replace(original, width=101)]:
            result, count = merge_model_proposals([current], [proposal])
            self.assertEqual(count, 0); self.assertEqual(result[0], current)

    def test_iou(self):
        self.assertEqual(_iou(Box(0, 0, 10, 10), Box(0, 0, 10, 10)), 1)
        self.assertEqual(_iou(Box(0, 0, 5, 5), Box(6, 6, 10, 10)), 0)
        self.assertAlmostEqual(_iou(Box(0, 0, 10, 10), Box(5, 0, 15, 10)), 1/3)

    def test_full_grid_template_covers_all_times_and_real_duty_cells(self):
        self.assertEqual(list(FULL_TIME_ROWS.values()),
                         [f"{hour:02d}-{hour+1:02d}" for hour in range(5, 24)])
        # C28 is a real column right after a bold rule; C33-C36 (A1A3站, 內機, 內部管理, 全區帶勤) are the
        # last four printed columns (see dataset.FULL_DUTY_COLUMNS)
        self.assertEqual(len(FULL_DUTY_COLUMNS), 35)
        self.assertEqual((min(FULL_DUTY_COLUMNS), max(FULL_DUTY_COLUMNS)), (2, 36))
        self.assertIn(28, FULL_DUTY_COLUMNS)
        self.assertTrue({33, 34, 35, 36} <= set(FULL_DUTY_COLUMNS))
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder)/"source.json", Path(folder)/"full.json"
            source.write_text(json.dumps({"grid_x": [i/33 for i in range(34)],
                                          "grid_y": [i/24 for i in range(25)],
                                          "row_map": {"5": "06-07"},
                                          "column_map": {"2": "old"}}))
            report = full_grid_template(source, output)
            payload = json.loads(output.read_text())
        self.assertEqual(report["cells_per_photo"], 19 * 35)
        self.assertEqual(payload["row_map"]["4"], "05-06")
        self.assertEqual(payload["row_map"]["22"], "23-24")
        self.assertEqual(len(payload["column_map"]), 35)


if __name__ == "__main__": unittest.main()
