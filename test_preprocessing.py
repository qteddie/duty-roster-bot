import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageDraw

from backend.preprocessing import (
    MIN_MODEL_CROP_WIDTH,
    ROSTER_HOURS_BOTTOM,
    _hourly_verification_crops,
    _main_roster_crop,
    _roster_grid_lines,
    _roster_crops,
    _snap_to_roster_cell,
    highlighted_roster_crop,
    preprocess_roster,
)


class PreprocessingTests(unittest.TestCase):
    def test_full_photo_preparation_preserves_the_source_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            destination = root / "cropped.png"
            image = Image.new("RGB", (800, 1000), "white")
            image.save(source)

            result = preprocess_roster(source, destination)

            self.assertEqual(result.method, "full_photo_strips")
            self.assertTrue(destination.is_file())
            with Image.open(destination) as cropped:
                self.assertEqual(cropped.size, image.size)
            self.assertEqual(len(result.zoom_image_paths), 6)
            with Image.open(result.international_crop_path) as crop:
                self.assertEqual(crop.width, MIN_MODEL_CROP_WIDTH)

    def test_hourly_verification_crops_repeat_header_for_each_row_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grid = root / "grid.png"
            Image.new("RGB", (300, 500), "white").save(grid)

            crops = _hourly_verification_crops(grid, root / "detail.png")

            self.assertEqual(len(crops), 4)
            self.assertTrue(all(path.is_file() for path in crops))

    def test_domestic_crop_rejoins_time_axis_with_domestic_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            image = Image.new("RGB", (100, 100), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 18, 99), fill="red")
            draw.rectangle((38, 0, 89, 99), fill="green")
            image.save(source)

            international, domestic = _roster_crops(source, source)

            with Image.open(international) as crop:
                self.assertEqual(crop.width, MIN_MODEL_CROP_WIDTH)
            with Image.open(domestic) as crop:
                self.assertEqual(crop.width, MIN_MODEL_CROP_WIDTH)
                self.assertGreater(crop.getpixel((45, 450))[0], 180)
                self.assertGreater(crop.getpixel((500, 450))[1], 80)

    def test_main_roster_crop_ends_at_the_17_18_region(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            Image.new("RGB", (100, 1_000), "white").save(source)

            cropped = _main_roster_crop(source, root / "roster-hours.png")

            with Image.open(cropped) as image:
                self.assertEqual(image.size, (100, round(1_000 * ROSTER_HOURS_BOTTOM)))

    def test_highlighted_roster_crop_draws_a_red_circle_around_a_cell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            destination = root / "highlighted.jpg"
            Image.new("RGB", (1_000, 1_000), "white").save(source)

            highlighted_roster_crop(source, destination, [{
                "left": 100, "top": 100, "right": 200, "bottom": 200,
            }])

            with Image.open(destination) as image:
                red_positions = [
                    x
                    for y in range(60, 240)
                    for x in range(50, 230)
                    if (red := image.getpixel((x, y))[0]) > 180
                    and image.getpixel((x, y))[1] < 100
                    and image.getpixel((x, y))[2] < 100
                ]
                red_pixels = len(red_positions)
            self.assertGreater(red_pixels, 20)
            self.assertLess(min(red_positions), 75)

    def test_grid_snapping_uses_detected_cell_edges(self):
        image = Image.new("RGB", (600, 500), "white")
        draw = ImageDraw.Draw(image)
        for x in range(100, 501, 50):
            draw.line((x, 80, x, 430), fill="black", width=2)
        for y in range(80, 431, 35):
            draw.line((100, y, 500, y), fill="black", width=2)

        vertical, horizontal = _roster_grid_lines(image)
        bounds = _snap_to_roster_cell(
            (143, 151, 189, 183),
            vertical_lines=vertical,
            horizontal_lines=horizontal,
            image_size=image.size,
        )

        self.assertEqual(bounds, (150, 150, 200, 186))

if __name__ == "__main__":
    unittest.main()
