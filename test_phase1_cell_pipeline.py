import tempfile
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageDraw

from backend.phase1_cell_pipeline import (
    CLASSIFIER_INSTRUCTIONS,
    CONFIRMATION_INSTRUCTIONS,
    CellLabel,
    ClassifiedCell,
    GridTemplate,
    PROJECT_ROOT,
    _registration_corners,
    _sectional_template_vertical_lines,
    accepted_assignments,
    analysis_time_window,
    align_semantic_row_lines,
    calibrated_template,
    correct_perspective,
    crop_mapped_cells,
    detect_timetable_corners,
    export_crop_artifacts,
    GridAlignmentError,
    grid_alignment_score,
    registered_grid_lines,
    registered_column_models,
    cell_polygon,
    original_grid_projector,
    validate_grid_alignment,
    render_markdown_grid,
    render_corrected_roster_preview,
)


class Phase1CellPipelineTests(unittest.TestCase):
    def _template(self):
        return GridTemplate(
            output_width=200,
            output_height=120,
            grid_x=(0.0, 0.5, 1.0),
            grid_y=(0.0, 0.5, 1.0),
            row_map={0: "05-06", 1: "06-07"},
            column_map={0: "國際線", 1: "國際線（二）"},
        )

    def test_row_alignment_recovers_missing_header_separator(self):
        expected = [16, 70, 93, 233, 257, 298, 344, 382]
        detected = [28, 52, 199, 224, 268, 316, 359, 402]
        aligned, offset = align_semantic_row_lines(
            detected, expected, first_body_row=5,
        )
        self.assertEqual(offset, -1)
        self.assertEqual(aligned[5:8], [268, 316, 359])

    def test_daily_photos_keep_time_axis_and_first_hour_after_registration(self):
        template_path = PROJECT_ROOT / "configs/phase1_roster_template.local.json"
        photos = [PROJECT_ROOT / "data" / name for name in ("9:17.jpg", "9:18.jpg")]
        if not template_path.exists() or not all(path.exists() for path in photos):
            self.skipTest("Local daily-photo regression fixtures are unavailable")
        template = GridTemplate.from_json(template_path)
        for photo in photos:
            with self.subTest(photo=photo.name):
                corrected, _ = correct_perspective(photo, template)
                self.assertGreaterEqual(validate_grid_alignment(corrected, template), 0.9)
                x_lines, y_lines = registered_grid_lines(corrected, template)
                # Time column spans two regular pitches before the first duty
                # column C02. Losing the left border shifts every duty index.
                self.assertLess(x_lines[0], corrected.width * 0.015)
                self.assertAlmostEqual(x_lines[2] / corrected.width, 0.056, delta=0.012)
                self.assertAlmostEqual(y_lines[5] / corrected.height, 0.187, delta=0.012)

    def test_production_template_includes_first_two_international_columns(self):
        template = GridTemplate.from_json(
            PROJECT_ROOT / "configs/phase1_roster_template.local.json"
        )
        self.assertEqual(template.column_map[2], "國際線")
        self.assertEqual(template.column_map[3], "國際線")

    def test_september14_international_third_fourth_divider_tracks_rule(self):
        source = PROJECT_ROOT / "data/9:14.jpg"
        template_path = PROJECT_ROOT / "configs/phase1_roster_template.local.json"
        if not source.exists() or not template_path.exists():
            self.skipTest("Local daily-photo regression fixture is unavailable")
        template = GridTemplate.from_json(template_path)
        image, _ = correct_perspective(source, template)
        xs, _ = registered_grid_lines(image, template)
        models = registered_column_models(image, xs)
        # C05 separates international #3 (C04) and #4 (C05).
        # Measured physical rule, not the glyph stroke around x=153 at y=310.
        a, b = models[5]
        self.assertAlmostEqual(a+b*310, 165, delta=2)
        self.assertAlmostEqual(a+b*790, 160, delta=2)
        self.assertLess(b, 0)

    def test_slanted_vertical_rules_and_cell_crops_follow_height(self):
        image = Image.new("RGB", (400, 600), "white")
        draw = ImageDraw.Draw(image)
        xs = [40, 100, 160, 220, 280, 340]
        slope = .04
        for x in xs:
            draw.line((x-slope*288,0,x+slope*(599-288),599),fill="black",width=2)
        models = registered_column_models(image,xs)
        for x,(a,b) in zip(xs,models):
            self.assertAlmostEqual(b,slope,delta=.012)
            self.assertAlmostEqual(a+b*120,x+slope*(120-288),delta=3)
        template=GridTemplate(grid_x=(.1,.25,.4,.55,.7,.85),
                              grid_y=(.2,.3,.4),row_map={0:"06-07"},column_map={1:"國際線"})
        cells=crop_mapped_cells(image,template,grid_lines=(xs,[120,180,240]),column_models=models)
        self.assertLess(cells[0].bounds[0],100)
        polygon=cell_polygon(0,1,(xs,[120,180,240]),column_models=models)
        self.assertGreater(polygon[3][0],polygon[0][0])

    def test_crops_only_mapped_cells_and_discards_cell_borders(self):
        image = Image.new("RGB", (200, 120), "white")
        cells = crop_mapped_cells(image, self._template(), scale=5)
        self.assertEqual([(cell.row, cell.column) for cell in cells], [(0, 0), (0, 1), (1, 0), (1, 1)])
        # A 100x60 cell keeps 78% then enlarges five times.
        self.assertEqual(cells[0].image.size, (390, 230))

    def test_cell_crop_can_inset_horizontal_borders_without_clipping_vertical_glyphs(self):
        image = Image.new("RGB", (200, 120), "white")
        cells = crop_mapped_cells(
            image, self._template(), scale=5,
            inner_fraction_x=.78, inner_fraction_y=.96,
        )
        # Horizontal borders are excluded while almost the full vertical span
        # remains available for glyphs touching the top or bottom of a cell.
        self.assertEqual(cells[0].image.size, (390, 290))

    def test_analysis_window_rebases_mapped_cell_crops(self):
        template = GridTemplate(
            output_width=200, output_height=120,
            grid_x=(0.0, 0.5, 1.0), grid_y=(0.0, 0.25, 0.5, 0.75, 1.0),
            row_map={1: "06-07", 2: "07-08"}, column_map={0: "國際線"},
        )
        window, origin = analysis_time_window(Image.new("RGB", (200, 120), "white"), template)
        cells = crop_mapped_cells(window, template, origin=origin, canvas_size=(200, 120))
        self.assertEqual((window.size, origin), ((200, 60), (0, 30)))
        self.assertEqual([cell.bounds for cell in cells], [(0, 30, 100, 60), (0, 60, 100, 90)])

    def test_corrected_preview_has_only_blue_boxes_and_stops_at_last_configured_hour(self):
        image = Image.new("RGB", (200, 160), "white")
        template = GridTemplate(
            grid_x=(0.0, 0.5, 1.0),
            grid_y=(0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0),
            row_map={4: "16-17", 5: "17-18"},
            column_map={0: "國際線"},
        )
        lines = ([0, 100, 199], [0, 20, 40, 60, 80, 100, 120, 140, 159])
        rows = [(float(y), 0.0) for y in lines[1]]
        with patch(
            "backend.phase1_cell_pipeline.registered_column_models",
            return_value=[(0.0, 0.0), (100.0, 0.0), (199.0, 0.0)],
        ):
            preview = render_corrected_roster_preview(image, template, lines, rows)
        self.assertEqual(preview.size, (200, 120))
        self.assertEqual(preview.getpixel((1, 80)), (37, 99, 235))
        self.assertNotIn((245, 158, 11), preview.getdata())

    def test_corrected_preview_aligns_rows_when_a_header_rule_is_missing(self):
        image = Image.new("RGB", (100, 100), "white")
        template = GridTemplate(
            output_width=100, output_height=100,
            grid_x=(0.0, 0.5, 1.0),
            grid_y=(0.0, 0.1, 0.2, 0.6, 0.7, 0.8, 0.9, 1.0),
            row_map={5: "06-07"}, column_map={0: "國際線"},
        )
        # The detected list lacks one separator before the hourly body. Raw
        # index 5 is y=90, while semantic row 5 must be rebased to y=80.
        lines = ([0, 50, 99], [0, 10, 60, 70, 80, 90, 99])
        with patch(
            "backend.phase1_cell_pipeline.registered_column_models",
            return_value=[(0.0, 0.0), (50.0, 0.0), (99.0, 0.0)],
        ), patch(
            "backend.phase1_cell_pipeline.registered_row_models",
            side_effect=lambda _image, values: [(float(y), 0.0) for y in values],
        ):
            preview = render_corrected_roster_preview(image, template, lines)

        self.assertEqual(preview.getpixel((0, 85)), (37, 99, 235))

    def test_only_matching_two_pass_target_is_mapped_to_schedule(self):
        template = self._template()
        records = accepted_assignments([
            ClassifiedCell(0, 0, (CellLabel.JI, CellLabel.WU), (CellLabel.JI, CellLabel.WU), 0.92),
            ClassifiedCell(0, 1, (CellLabel.DING,), (CellLabel.GENG,), 0.90),
            ClassifiedCell(1, 0, (CellLabel.EMPTY,), None, 0.99),
        ], template)
        self.assertEqual(records, [{
            "person": "戊", "row": 0, "column": 0,
            "time": "05-06", "shift": "國際線", "confidence": 0.92,
        }, {
            "person": "己", "row": 0, "column": 0,
            "time": "05-06", "shift": "國際線", "confidence": 0.92,
        }])

    def test_markdown_grid_keeps_empty_other_and_disagreement_visible(self):
        table = render_markdown_grid([
            ClassifiedCell(0, 0, (CellLabel.JI, CellLabel.WU), (CellLabel.JI, CellLabel.WU), 0.9),
            ClassifiedCell(0, 1, (CellLabel.DING,), (CellLabel.GENG,), 0.9),
            ClassifiedCell(1, 0, (CellLabel.EMPTY,), None, 0.9),
            ClassifiedCell(1, 1, (CellLabel.OTHER,), None, 0.9),
        ], self._template())
        self.assertIn("| 05-06 | 戊+己 | 丁→庚 |", table)
        self.assertIn("| 06-07 | · | O |", table)
        self.assertIn("國際線 #1", table)

    def test_crop_artifacts_include_every_cell_and_contact_sheet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = Image.new("RGB", (200, 120), "white")
            cells = crop_mapped_cells(image, self._template())
            artifacts = export_crop_artifacts(
                root / "review", corrected=image, analysis_image=image,
                cells=cells, template=self._template(),
            )
            self.assertTrue(Path(artifacts["corrected_image"]).is_file())
            self.assertTrue(Path(artifacts["analysis_window"]).is_file())
            self.assertTrue(Path(artifacts["contact_sheet"]).is_file())
            self.assertTrue(Path(artifacts["index"]).is_file())
            self.assertEqual(len(list(Path(artifacts["cell_directory"]).glob("*.png"))), 4)
            with Image.open(artifacts["contact_sheet"]) as sheet:
                # Two duty columns plus a dedicated left-hand time column,
                # and a header row plus two time rows.
                self.assertEqual(sheet.size, (565, 400))

    def test_ding_and_geng_rules_distinguish_lookalikes_from_stacked_entries(self):
        for instructions in (CLASSIFIER_INSTRUCTIONS, CONFIRMATION_INSTRUCTIONS):
            self.assertIn("I/H/M/B/F/D", instructions)
            self.assertIn("乙", instructions)
            self.assertIn("丙", instructions)
            self.assertIn("stacked", instructions)

    def test_module_loads_project_env_without_overriding_existing_environment(self):
        # The module-level call is intentionally explicit so its CLI can run
        # from a directory other than the repository root.
        with patch("backend.phase1_cell_pipeline.load_dotenv") as loader:
            from backend.phase1_cell_pipeline import load_project_env
            load_project_env()
        loader.assert_called_once_with(PROJECT_ROOT / ".env", override=False)

    def test_correct_perspective_uses_template_corners(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "roster.png"
            Image.new("RGB", (100, 80), "white").save(source)
            corrected, method = correct_perspective(source, GridTemplate(
                output_width=200, output_height=120,
                table_corners=((0, 0), (99, 0), (99, 79), (0, 79)),
            ))
            self.assertEqual((corrected.size, method), ((200, 120), "perspective"))

    def test_aspect_preserving_registration_does_not_squeeze_the_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "roster.png"
            Image.new("RGB", (100, 200), "white").save(source)
            corrected, method = correct_perspective(source, GridTemplate(
                output_width=240, output_height=160,
                preserve_source_aspect_ratio=True,
                table_corners=((0, 0), (99, 0), (99, 199), (0, 199)),
            ))
            self.assertEqual((corrected.size, method), ((80, 160), "perspective"))

    def test_registered_grid_lines_restores_a_single_missing_physical_column(self):
        template = GridTemplate(
            output_width=100, output_height=100,
            grid_x=(0.0, 0.25, 0.5, 0.75, 1.0),
            grid_y=(0.0, 0.25, 0.5, 0.75, 1.0),
            row_map={1: "06-07"}, column_map={2: "國際線"},
        )
        with patch("backend.phase1_cell_pipeline.detect_grid_lines",
                   return_value=([0, 50, 75, 100], [0, 25, 50, 75, 100])):
            x_lines, y_lines = registered_grid_lines(Image.new("RGB", (100, 100), "white"), template)
        self.assertEqual(x_lines[:4], [0, 25, 50, 75])
        self.assertEqual(y_lines[:3], [0, 25, 50])

    def test_registered_grid_lines_anchors_first_duty_after_two_missing_left_rules(self):
        template = GridTemplate(
            output_width=120, output_height=100,
            grid_x=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            grid_y=(0.0, 0.25, 0.5, 0.75, 1.0),
            row_map={1: "06-07"}, column_map={2: "國際線", 3: "國際線（二）"},
        )
        image = Image.new("RGB", (120, 100), "white")
        draw = ImageDraw.Draw(image)
        for y in (0, 25, 50, 75, 99):
            draw.line((0, y, 119, y), fill="black", width=2)
        # The outer/time header rules are absent from the long-line detector;
        # x=40 is already semantic edge C02, not C00.
        with patch("backend.phase1_cell_pipeline.detect_grid_lines",
                   return_value=([40, 60, 80, 100, 119], [0, 25, 50, 75, 99])):
            x_lines, _ = registered_grid_lines(image, template)
        self.assertEqual(x_lines[2:5], [40, 60, 80])

    def test_sectional_detector_recovers_slanted_rules_missing_from_full_projection(self):
        image = Image.new("RGB", (200, 300), "white")
        draw = ImageDraw.Draw(image)
        expected = [0, 40, 80, 120, 160, 199]
        for x in (2, 40, 80, 120, 160, 197):
            draw.line((x - 4, 0, x + 4, 299), fill="black", width=2)

        recovered, supported = _sectional_template_vertical_lines(
            image, [2, 40], expected, required_count=len(expected),
        )

        self.assertEqual(recovered, [2, 40, 80, 120, 160, 197])
        self.assertTrue(all(supported))

    def test_registered_grid_lines_rejects_an_invented_first_duty_edge(self):
        template = GridTemplate(
            output_width=100, output_height=100,
            grid_x=(0.0, 0.25, 0.5, 0.75, 1.0),
            grid_y=(0.0, 0.25, 0.5, 0.75, 1.0),
            row_map={1: "06-07"}, column_map={2: "國際線"},
        )
        # C02 is missing. A visually regular interpolation must not silently
        # shift all semantic duty columns.
        with patch("backend.phase1_cell_pipeline.detect_grid_lines",
                   return_value=([0, 25, 75, 100], [0, 25, 50, 75, 100])):
            with self.assertRaisesRegex(GridAlignmentError, "第一個勤務欄"):
                registered_grid_lines(Image.new("RGB", (100, 100), "white"), template)

    def test_september12_missing_first_duty_edge_is_rejected(self):
        source = PROJECT_ROOT / "data/9:12.jpg"
        template_path = PROJECT_ROOT / "configs/phase1_roster_template.local.json"
        if not source.exists() or not template_path.exists():
            self.skipTest("Local daily-photo regression fixture is unavailable")
        template = GridTemplate.from_json(template_path)
        image, _ = correct_perspective(source, template)
        with self.assertRaisesRegex(GridAlignmentError, "第一個勤務欄"):
            registered_grid_lines(image, template)

    def test_original_image_mode_keeps_native_pixels_and_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "roster.png"
            Image.new("RGB", (100, 80), "white").save(source)
            original, method = correct_perspective(source, GridTemplate(use_original_image=True))
            self.assertEqual((original.size, method), ((100, 80), "source_original"))

    def test_original_crop_bottom_extension_keeps_upper_edge_fixed(self):
        common = dict(
            output_width=200, output_height=100,
            table_corners=((0, 0), (199, 0), (199, 99), (0, 99)),
            grid_x=(0.0, 0.5, 1.0), grid_y=(0.0, 0.5, 1.0),
            row_map={0: "06-07"}, column_map={0: "國際線"},
        )
        image = Image.new("RGB", (200, 100), "white")
        baseline = original_grid_projector(image, GridTemplate(**common))
        shifted = original_grid_projector(image, GridTemplate(
            **common, source_crop_bottom_extension_fraction=0.1,
        ))
        normal_bounds = baseline.mapped_cells()[0].bounds
        shifted_bounds = shifted.mapped_cells()[0].bounds
        self.assertEqual(shifted_bounds[1], normal_bounds[1])
        self.assertGreater(shifted_bounds[3], normal_bounds[3])

    def test_calibration_detects_grid_and_serializes_a_new_template(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "roster.png"
            image = Image.new("RGB", (300, 200), "white")
            draw = ImageDraw.Draw(image)
            for x in (20, 80, 140, 200, 260):
                draw.line((x, 20, x, 180), fill="black", width=3)
            for y in (20, 60, 100, 140, 180):
                draw.line((20, y, 260, y), fill="black", width=3)
            image.save(source)
            template, _ = calibrated_template(source, GridTemplate(output_width=300, output_height=200,
                                                                     table_corners=((0, 0), (299, 0), (299, 199), (0, 199))))
            self.assertGreaterEqual(len(template.grid_x), 5)
            self.assertGreaterEqual(len(template.grid_y), 5)

    def test_grid_alignment_rejects_a_shifted_registration_before_ocr(self):
        template = replace(self._template(), minimum_grid_alignment_score=0.9)
        expected_x = [0, 100, 200]
        expected_y = [0, 60, 120]
        with patch("backend.phase1_cell_pipeline.detect_grid_lines", return_value=(expected_x, expected_y)):
            score = grid_alignment_score(Image.new("RGB", (200, 120), "white"), template)
            self.assertEqual(score, 1.0)
            self.assertEqual(validate_grid_alignment(Image.new("RGB", (200, 120), "white"), template), 1.0)
        with patch("backend.phase1_cell_pipeline.detect_grid_lines", return_value=([80], [100])):
            with self.assertRaises(GridAlignmentError):
                validate_grid_alignment(Image.new("RGB", (200, 120), "white"), template)


# A roster-like form on a 900x1200 sheet: a wide time column, 34 duty columns,
# a title band and 40 rows.  Coordinates are the rules' centres at scale 1.
SHEET_SIZE = (900, 1200)
SHEET_X = [90.0, 130.0] + [130.0 + 20.0 * k for k in range(1, 35)]
SHEET_Y = [70.0, 120.0, 220.0] + [220.0 + 22.75 * k for k in range(1, 41)]
SHEET_CORNERS = ((SHEET_X[0], SHEET_Y[0]), (SHEET_X[-1], SHEET_Y[0]),
                 (SHEET_X[-1], SHEET_Y[-1]), (SHEET_X[0], SHEET_Y[-1]))


def ruled_sheet(long_side=1100, *, angle=0.0, quality=70, neighbour=False, red_marks=False):
    """Render the form like a re-encoded phone photo; return it and the true table corners.

    Drawn at three times the scale and reduced, so rules come out thin and
    anti-aliased as in a LINE copy.  ``neighbour`` adds another sheet's rules
    along the right edge, ``red_marks`` a red handwritten date over the
    top-left corner, ``angle`` turns the photo counter-clockwise.
    """
    zoom = 3
    width, height = SHEET_SIZE
    image = Image.new("RGB", (width * zoom, height * zoom), (236, 236, 228))
    draw = ImageDraw.Draw(image)
    for x in SHEET_X:
        draw.line((x * zoom, SHEET_Y[0] * zoom, x * zoom, SHEET_Y[-1] * zoom), fill=(25, 25, 25), width=5)
    for y in SHEET_Y:
        draw.line((SHEET_X[0] * zoom, y * zoom, SHEET_X[-1] * zoom, y * zoom), fill=(25, 25, 25), width=5)
    for row, y in enumerate(SHEET_Y[3:-1]):   # a "2" in some cells: strokes that are not rules
        for column, x in enumerate(SHEET_X[2:-1]):
            if (row * 7 + column * 3) % 4 == 0:
                glyph = [(x + 5, y + 5), (x + 13, y + 5), (x + 13, y + 11), (x + 5, y + 17), (x + 14, y + 17)]
                draw.line([(gx * zoom, gy * zoom) for gx, gy in glyph], fill=(30, 30, 30), width=4)
    if neighbour:
        draw.line((862 * zoom, 0, 862 * zoom, height * zoom), fill=(25, 25, 25), width=5)
        for y in range(40, height, 24):
            draw.line((862 * zoom, y * zoom, width * zoom, y * zoom), fill=(25, 25, 25), width=5)
    if red_marks:
        red = (235, 50, 70)
        draw.line((70 * zoom, 20 * zoom, 70 * zoom, 125 * zoom), fill=red, width=12 * zoom)
        draw.line((150 * zoom, 125 * zoom, 190 * zoom, 15 * zoom), fill=red, width=12 * zoom)
        draw.ellipse((95 * zoom, 20 * zoom, 140 * zoom, 125 * zoom), outline=red, width=12 * zoom)
    scale = long_side / max(width, height)
    image = image.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
    corners = np.asarray(SHEET_CORNERS) * scale
    if angle:
        before = np.asarray(image.size, dtype=float)
        image = image.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(120, 120, 120))
        theta = np.deg2rad(angle)
        rotation = np.asarray([[np.cos(theta), np.sin(theta)], [-np.sin(theta), np.cos(theta)]])
        corners = (corners - before / 2) @ rotation.T + np.asarray(image.size) / 2
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return Image.open(BytesIO(buffer.getvalue())).convert("RGB"), corners


def sheet_template():
    """The form's grid, normalised to its outer border, as a calibrated template."""
    width, height = SHEET_X[-1] - SHEET_X[0], SHEET_Y[-1] - SHEET_Y[0]
    return GridTemplate(
        preserve_source_aspect_ratio=True,
        grid_x=tuple((x - SHEET_X[0]) / width for x in SHEET_X),
        grid_y=tuple((y - SHEET_Y[0]) / height for y in SHEET_Y),
        row_map={row: f"{row:02d}" for row in range(3, 42)},
        column_map={column: f"c{column:02d}" for column in range(2, 35)},
    )


class TimetableCornerTests(unittest.TestCase):
    def assertCornersNear(self, found, expected, tolerance):
        self.assertIsNotNone(found)
        error = np.abs(np.asarray(found) - np.asarray(expected)).max()
        self.assertLess(error, tolerance, f"corners {np.round(found)} vs {np.round(expected)}")

    def test_finds_the_border_of_a_small_recompressed_photo(self):
        for long_side, quality in ((1100, 55), (1400, 90)):
            with self.subTest(long_side=long_side, quality=quality):
                image, corners = ruled_sheet(long_side, quality=quality)
                self.assertCornersNear(detect_timetable_corners(image), corners, long_side * 0.006)

    def test_the_same_border_at_line_and_phone_original_sizes(self):
        small, small_corners = ruled_sheet(1100)
        large, large_corners = ruled_sheet(3300)
        self.assertCornersNear(detect_timetable_corners(small), small_corners, 7)
        self.assertCornersNear(detect_timetable_corners(large), large_corners, 21)

    def test_follows_a_tilted_photo(self):
        for angle in (-2.0, 1.0, 2.0):
            with self.subTest(angle=angle):
                image, corners = ruled_sheet(1300, angle=angle)
                self.assertCornersNear(detect_timetable_corners(image), corners, 9)

    def test_a_neighbouring_sheet_does_not_pull_the_right_side(self):
        for long_side, angle in ((1100, 1.0), (1200, -1.0), (1200, 0.0), (1477, -1.0)):
            with self.subTest(long_side=long_side, angle=angle):
                image, corners = ruled_sheet(long_side, angle=angle, neighbour=True)
                self.assertCornersNear(detect_timetable_corners(image), corners, long_side * 0.007)

    def test_a_red_date_over_the_corner_does_not_hide_the_border(self):
        image, corners = ruled_sheet(1150, red_marks=True)
        self.assertCornersNear(detect_timetable_corners(image), corners, 7)

    def test_follows_a_keystoned_photo(self):
        image, corners = ruled_sheet(1300)
        width, height = image.size
        # The phone leaned back: the top of the sheet comes out 4% narrower.
        shrunk = np.float32([(0.04 * width, 0), (0.96 * width, 0), (width, height), (0, height)])
        matrix = cv2.getPerspectiveTransform(np.float32([(0, 0), (width, 0), (width, height), (0, height)]), shrunk)
        warped = cv2.warpPerspective(np.asarray(image), matrix, (width, height), borderValue=(120, 120, 120))
        expected = cv2.perspectiveTransform(np.float32([corners]), matrix)[0]
        self.assertCornersNear(detect_timetable_corners(Image.fromarray(warped)), expected, 9)

    def test_registration_takes_the_form_and_refuses_a_rectangle_without_its_grid(self):
        image, corners = ruled_sheet(1200, angle=1.0, neighbour=True)
        found, _ = _registration_corners(image, sheet_template())
        self.assertCornersNear(found, corners, 8)
        # A screenshot panel: a large clean rectangle around a few lines of text.
        panel = Image.new("RGB", (900, 1600), (200, 220, 240))
        draw = ImageDraw.Draw(panel)
        draw.rounded_rectangle((60, 200, 840, 1400), radius=30, fill="white", outline=(90, 90, 90), width=4)
        for y in range(260, 1300, 90):   # lines of text
            for x in range(100, 760, 40):
                draw.rectangle((x, y, x + 28, y + 30), outline="black", width=3)
        self.assertEqual(_registration_corners(panel, sheet_template()), (None, "perspective"))

    def test_registration_keeps_a_perfect_paper_outline_without_the_second_detector(self):
        outline = ((0.0, 0.0), (99.0, 0.0), (99.0, 79.0), (0.0, 79.0))
        with patch("backend.phase1_cell_pipeline.detect_table_corners", return_value=outline), \
                patch("backend.phase1_cell_pipeline.grid_alignment_score", return_value=1.0), \
                patch("backend.phase1_cell_pipeline.detect_timetable_corners") as timetable:
            found = _registration_corners(Image.new("RGB", (100, 80), "white"), sheet_template())
        self.assertEqual(found, (outline, "perspective"))
        timetable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
