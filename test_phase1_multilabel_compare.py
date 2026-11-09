import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image

from backend.phase1_multilabel_compare import (
    MULTILABEL_INSTRUCTIONS,
    PRECISION_REVIEW_INSTRUCTIONS,
    ReviewGlyph,
    WU_GENG_INSTRUCTIONS,
    ModelWuGengVerification,
    ModelGlyphTranscription,
    TargetPresence,
    apply_wu_geng_override,
    intersect_multilabel,
    needs_precision_review,
    assignments_from_final,
    create_employee_evidence_image,
    highlighted_context_cells,
    recognize_source_multilabel,
    resolve_assignments_from_final,
    load_saved_primary,
    load_existing_cell_crops,
    parse_baseline_grid,
    render_comparison,
    wu_geng_candidates,
    wu_only_candidates,
)
from backend.phase1_cell_pipeline import GridTemplate


class MultiLabelComparisonTests(unittest.TestCase):
    def _template(self):
        return GridTemplate(
            output_width=100, output_height=100,
            grid_x=(0.0, 0.5, 1.0), grid_y=(0.0, 0.5, 1.0),
            row_map={0: "06-07", 1: "07-08"},
            column_map={0: "國際線", 1: "國際線（二）"},
        )

    def test_production_keeps_full_glyphs_without_pair_overwrite(self):
        template = self._template()
        captured = []

        def classify(cells, **kwargs):
            captured.extend(cells)
            return {(c.row, c.column): TargetPresence(
                c.row, c.column, False, True, False, False, .99) for c in cells}

        prefix = "backend.phase1_multilabel_compare."
        with patch(prefix + "correct_perspective", return_value=(Image.new("RGB", (100, 100), "white"), "test")), \
             patch(prefix + "validate_grid_alignment", return_value=1.0), \
             patch(prefix + "registered_grid_lines", return_value=([0, 50, 100], [0, 50, 100])), \
             patch(prefix + "registered_row_models", return_value=[(0, 0), (50, 0), (100, 0)]), \
             patch(prefix + "registered_column_models", return_value=[(0, 0), (50, 0), (100, 0)]), \
             patch(prefix + "classify_multilabel", side_effect=classify), \
             patch(prefix + "verify_multilabel", side_effect=lambda cells, **kwargs: {
                 (c.row, c.column): TargetPresence(
                     c.row, c.column, False, True, False, False, .98
                 ) for c in cells
             }), \
             patch(prefix + "verify_wu_geng") as verifier:
            result = recognize_source_multilabel(Path("unused.png"), template, model="test")
        # Both passes retain full glyph edges. The independent closed-vocabulary
        # transcription, rather than clipping, rejects confusing characters.
        self.assertEqual([c.image.size for c in captured], [(240, 240)] * 4)
        verifier.assert_not_called()
        self.assertEqual(result["wu_geng_verifier_count"], 0)
        self.assertEqual(result["cell_inner_fraction"], .96)
        self.assertEqual(result["review_crop"], "highlighted_3x3_context")
        self.assertEqual(result["review_batch_size"], 1)
        self.assertEqual(result["reviewed_cell_count"], 0)
        self.assertEqual(result["recognition_policy"], "selective_claim_verification")
        self.assertTrue(all(c["wu"] and not c["geng"] for c in result["final"]))
        self.assertIn("戊", result["markdown_grid"])

    def test_precision_context_marks_target_cell_and_keeps_neighbors(self):
        image = Image.new("RGB", (90, 90), "white")
        cell = type("Cell", (), {
            "row": 1, "column": 1, "bounds": (30, 30, 60, 60),
        })()
        context = highlighted_context_cells(image, [cell], scale=1)[0]
        self.assertEqual(context.image.size, (90, 90))
        self.assertEqual(context.image.getpixel((30, 30)), (255, 0, 0))
        self.assertEqual(context.bounds, (30, 30, 60, 60))

    def test_loads_existing_pngs_without_calling_the_crop_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for row in (0, 1):
                for column in (0, 1):
                    Image.new("RGB", (20 + row, 30 + column), "white").save(
                        root / f"r{row:02d}_c{column:02d}.png"
                    )
            cells = load_existing_cell_crops(root, self._template())
        self.assertEqual([(cell.row, cell.column) for cell in cells],
                         [(0, 0), (0, 1), (1, 0), (1, 1)])
        self.assertEqual(cells[0].image.size, (20, 30))

    def test_saved_primary_can_be_reused_only_for_the_same_cell_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells_dir = root / "cells"
            cells_dir.mkdir()
            for row in (0, 1):
                for column in (0, 1):
                    Image.new("RGB", (20, 30), "white").save(cells_dir / f"r{row:02d}_c{column:02d}.png")
            cells = load_existing_cell_crops(cells_dir, self._template())
            saved = root / "comparison.json"
            saved.write_text(json.dumps({"primary": [
                {"row": cell.row, "column": cell.column, "ding": False, "wu": False,
                 "ji": cell.row == 1, "geng": False, "confidence": 0.9}
                for cell in cells
            ]}), encoding="utf-8")
            loaded = load_saved_primary(saved, cells)
        self.assertTrue(loaded[(1, 0)].ji)
        self.assertFalse(loaded[(0, 0)].ji)

    def test_wu_geng_verifier_replaces_only_that_confusion_pair(self):
        primary = TargetPresence(0, 0, ding=True, wu=True, ji=False, geng=False, confidence=0.9)
        final = apply_wu_geng_override(
            primary, ModelWuGengVerification(row=0, column=0, wu=False, geng=True, confidence=0.8)
        )
        self.assertEqual(final.active_codes(), ("丁", "庚"))
        self.assertEqual(final.confidence, 0.8)

    def test_precision_intersection_removes_unconfirmed_similar_glyphs(self):
        primary = TargetPresence(5, 5, False, True, True, True, .92)
        review = TargetPresence(5, 5, False, True, False, False, .88)
        final = intersect_multilabel(primary, review)
        self.assertEqual(final.active_codes(), ("戊",))
        self.assertEqual(final.confidence, .88)

    def test_precision_review_names_observed_confusions(self):
        for lookalike in ("子", "丑", "已", "巳", "戌", "戍"):
            self.assertIn(lookalike, PRECISION_REVIEW_INSTRUCTIONS)
        self.assertIn(ReviewGlyph.ZI.value, PRECISION_REVIEW_INSTRUCTIONS)
        self.assertIn(ReviewGlyph.CHOU.value, PRECISION_REVIEW_INSTRUCTIONS)
        self.assertIn("red rectangle", PRECISION_REVIEW_INSTRUCTIONS)
        self.assertIn("compact pair is 己戊", PRECISION_REVIEW_INSTRUCTIONS)

    def test_precision_transcription_normalizes_other_and_duplicates(self):
        item = ModelGlyphTranscription(
            row=1, column=2,
            glyphs=[ReviewGlyph.OTHER, ReviewGlyph.WU, ReviewGlyph.WU],
            confidence=.8,
        )
        self.assertEqual(item.glyphs, [ReviewGlyph.WU])

    def test_only_ambiguous_or_low_confidence_predictions_are_reviewed(self):
        self.assertFalse(needs_precision_review(
            TargetPresence(0, 0, True, False, False, False, .99)))
        self.assertFalse(needs_precision_review(
            TargetPresence(0, 0, False, True, False, False, .95)))
        self.assertTrue(needs_precision_review(
            TargetPresence(0, 0, False, True, False, False, .94)))
        self.assertTrue(needs_precision_review(
            TargetPresence(0, 0, False, False, True, False, .99)))
        self.assertTrue(needs_precision_review(
            TargetPresence(0, 0, True, True, False, False, .99)))

    def test_target_or_dense_cell_without_geng_triggers_the_pair_verifier(self):
        cells = [
            type("Cell", (), {"row": 0, "column": 0})(),
            type("Cell", (), {"row": 0, "column": 1})(),
            type("Cell", (), {"row": 1, "column": 0})(),
            type("Cell", (), {"row": 1, "column": 1})(),
        ]
        predictions = {
            (0, 0): TargetPresence(0, 0, False, True, False, False, 0.9),
            (0, 1): TargetPresence(0, 1, False, False, False, True, 0.9),
            (1, 0): TargetPresence(1, 0, False, True, False, True, 0.9),
            (1, 1): TargetPresence(1, 1, False, False, True, False, 0.9),
        }
        self.assertEqual(
            [(cell.row, cell.column) for cell in wu_geng_candidates(cells, predictions)],
            [(0, 0), (1, 1)],
        )
        self.assertEqual(
            [(cell.row, cell.column) for cell in wu_only_candidates(cells, predictions)],
            [(0, 0), (1, 1)],
        )

    def test_final_assignments_include_every_active_target_cell(self):
        assignments = assignments_from_final([
            {"row": 0, "column": 0, "ding": False, "wu": False,
             "ji": True, "geng": True, "confidence": 0.99},
            {"row": 0, "column": 1, "ding": False, "wu": False,
             "ji": False, "geng": False, "confidence": 0.99},
            {"row": 1, "column": 0, "ding": False, "wu": False,
             "ji": True, "geng": False, "confidence": 0.97},
            {"row": 1, "column": 1, "ding": False, "wu": False,
             "ji": False, "geng": False, "confidence": 0.99},
        ], self._template(), employee_code="己")

        self.assertEqual(
            [(item["time"], item["shift"], item["cell_text"]) for item in assignments],
            [("06-07", "國際線", "己+庚"), ("07-08", "國際線", "己")],
        )

    def test_assignment_conflict_uses_margin_or_requests_review(self):
        final = [
            {"row": 0, "column": 0, "ding": False, "wu": False,
             "ji": True, "geng": False, "confidence": 0.86},
            {"row": 0, "column": 1, "ding": False, "wu": False,
             "ji": True, "geng": False, "confidence": 0.82},
            {"row": 1, "column": 0, "ding": False, "wu": False,
             "ji": True, "geng": False, "confidence": 0.96},
            {"row": 1, "column": 1, "ding": False, "wu": False,
             "ji": True, "geng": False, "confidence": 0.70},
        ]
        assignments, conflicts = resolve_assignments_from_final(
            final, self._template(), employee_code="己",
        )
        self.assertEqual([(item["time"], item["shift"]) for item in assignments],
                         [("07-08", "國際線")])
        self.assertEqual(assignments[0]["column"], 0)
        self.assertEqual(conflicts[0]["time"], "06-07")
        self.assertEqual([item["shift"] for item in conflicts[0]["candidates"]],
                         ["國際線", "國際線（二）"])

    def test_comparison_reads_baseline_without_mutating_it(self):
        template = self._template()
        payload = {
            "markdown_grid": (
                "| 時段 | 國際線 #1 | 國際線（二） #1 |\n"
                "| --- | --- | --- |\n"
                "| 06-07 | 戊→庚 | O |\n"
                "| 07-08 | · | 己 |"
            )
        }
        baseline = parse_baseline_grid(json.loads(json.dumps(payload)), template)
        primary = {
            (0, 0): TargetPresence(0, 0, False, True, False, False, 0.8),
            (0, 1): TargetPresence(0, 1, False, False, False, False, 0.9),
            (1, 0): TargetPresence(1, 0, False, False, False, False, 0.9),
            (1, 1): TargetPresence(1, 1, False, False, False, False, 0.9),
        }
        verifier = {(0, 0): ModelWuGengVerification(row=0, column=0, wu=False, geng=True, confidence=0.9)}
        final = {index: apply_wu_geng_override(value, verifier.get(index)) for index, value in primary.items()}
        comparison = render_comparison(baseline, primary, final, verifier, template)
        self.assertEqual(payload["markdown_grid"].splitlines()[2], "| 06-07 | 戊→庚 | O |")
        self.assertIn("戊=否；庚=是", comparison)
        self.assertIn("| 06-07 | 國際線 | c00 | 戊→庚 | 戊 |", comparison)
        self.assertIn("| 07-08 | 國際線（二） | c01 | 己 | · |", comparison)

    def test_prompts_use_whole_cell_multilabel_and_focused_pair_check(self):
        self.assertIn("multi-label", MULTILABEL_INSTRUCTIONS)
        self.assertIn("without fixed top/bottom splitting", WU_GENG_INSTRUCTIONS)
        self.assertIn("乙", MULTILABEL_INSTRUCTIONS)
        self.assertIn("丙", WU_GENG_INSTRUCTIONS)

    def test_evidence_image_keeps_full_width_and_stops_at_last_configured_hour(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "roster.png"
            Image.new("RGB", (300, 100), "white").save(source)
            template = GridTemplate(
                output_width=300, output_height=100, use_original_image=True,
                table_corners=((0, 0), (299, 0), (299, 99), (0, 99)),
                grid_x=(0.0, 0.25, 0.5, 0.75, 1.0), grid_y=(0.0, 0.25, 0.5, 0.75, 1.0),
                row_map={0: "06-07", 1: "07-08"},
                column_map={
                    0: "國際線", 1: "國際線（二）", 2: "國內工作管制門", 3: "其他欄位",
                },
            )
            card = create_employee_evidence_image(
                source, template,
                final=[
                    {"row": 0, "column": 0, "ding": False, "wu": False,
                     "ji": True, "geng": False, "confidence": 0.99},
                    {"row": 0, "column": 1, "ding": False, "wu": False,
                     "ji": True, "geng": False, "confidence": 0.99},
                    {"row": 0, "column": 2, "ding": False, "wu": False,
                     "ji": False, "geng": False, "confidence": 0.99},
                    {"row": 0, "column": 3, "ding": False, "wu": False,
                     "ji": False, "geng": False, "confidence": 0.99},
                    {"row": 1, "column": 0, "ding": False, "wu": False,
                     "ji": False, "geng": False, "confidence": 0.99},
                    {"row": 1, "column": 1, "ding": False, "wu": False,
                     "ji": False, "geng": False, "confidence": 0.99},
                    {"row": 1, "column": 2, "ding": False, "wu": False,
                     "ji": False, "geng": False, "confidence": 0.99},
                    {"row": 1, "column": 3, "ding": False, "wu": False,
                     "ji": False, "geng": False, "confidence": 0.99},
                ],
                employee_code="己", destination=root / "evidence.jpg",
            )
            self.assertIsNotNone(card)
            with Image.open(card) as image:
                self.assertEqual(image.size, (300, 50))
                red, green, blue = image.getpixel((1, 1))
                self.assertGreater(red, blue)
                self.assertGreater(red, green)
                red, green, blue = image.getpixel((80, 1))
                self.assertGreater(green, blue)
                # Unassigned mapped cells keep the blue debug outline.
                red, green, blue = image.getpixel((0, 40))
                self.assertGreater(blue, red)
                self.assertGreater(blue, green)


if __name__ == "__main__":
    unittest.main()
