"""Lightweight image preparation for roster vision analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps


@dataclass
class PreprocessResult:
    image_path: Path
    method: str
    zoom_image_paths: list[Path]
    international_crop_path: Path | None = None
    domestic_crop_path: Path | None = None


# These boundaries follow the current daily roster layout. Keeping them in one
# place makes calibration straightforward if the form layout changes later.
TIME_AXIS_RIGHT = 0.19
# The international view ends after 國際線備勤 and before 國內線一線.
INTERNATIONAL_RIGHT = 0.57
DOMESTIC_LEFT = 0.57
DOMESTIC_RIGHT = 0.90
# The 17-18 row ends just below the upper half of the standard daily form.
# Everything below it is the unused later rows, name table, and notes.
ROSTER_HOURS_BOTTOM = 0.49
MAX_DISCORD_CROP_HEIGHT = 2_400
MIN_MODEL_CROP_WIDTH = 900
# The vision model's cell-box coordinates on this form consistently land about
# one narrow roster column to the right.  Values are in the model's 0–1000
# coordinate system and affect only the annotation, never the extracted shift.
# A small empirical correction is retained before snapping. The final bounds
# always come from detected table lines, not from these approximate coordinates.
HIGHLIGHT_CELL_X_OFFSET = -17


def _hourly_verification_crops(
    image_path: Path, destination: Path, groups: int = 4
) -> list[Path]:
    """Create overlapping enlarged strips for a deliberate hour-by-hour review."""
    if not image_path.is_file():
        return []

    with Image.open(image_path) as original:
        image = original.convert("RGB")
        if image.height < groups * 20:
            return []

        paths: list[Path] = []
        for group in range(groups):
            overlap = int(image.height * 0.08)
            row_top = max(0, (image.height * group) // groups - overlap)
            row_bottom = min(image.height, (image.height * (group + 1)) // groups + overlap)
            strip = image.crop((0, row_top, int(image.width * 0.985), row_bottom))
            enlarged = strip.resize(
                (strip.width * 2, strip.height * 2), Image.Resampling.LANCZOS
            )
            path = destination.with_name(f"hourly-verification-{group + 1}.png")
            enlarged.save(path, format="PNG", optimize=True)
            paths.append(path)
    return paths


def _save_discord_crop(image: Image.Image, destination: Path) -> Path:
    """Save an enlarged, attachment-friendly JPEG for Discord and vision review."""
    if image.width < MIN_MODEL_CROP_WIDTH:
        scale = MIN_MODEL_CROP_WIDTH / image.width
        image = image.resize(
            (MIN_MODEL_CROP_WIDTH, round(image.height * scale)), Image.Resampling.LANCZOS
        )
    if image.height > MAX_DISCORD_CROP_HEIGHT:
        scale = MAX_DISCORD_CROP_HEIGHT / image.height
        image = image.resize(
            (round(image.width * scale), MAX_DISCORD_CROP_HEIGHT),
            Image.Resampling.LANCZOS,
        )
    image.save(destination, format="JPEG", quality=88, optimize=True, progressive=True)
    return destination


def _main_roster_crop(image_path: Path, destination: Path) -> Path:
    """Keep the roster header through the 17-18 row, excluding lower sections."""
    with Image.open(image_path) as original:
        image = original.convert("RGB")
        bottom = round(image.height * ROSTER_HOURS_BOTTOM)
        image.crop((0, 0, image.width, bottom)).save(destination, format="PNG", optimize=True)
    return destination


def _line_centers(scores: np.ndarray, *, minimum: int) -> list[int]:
    """Collapse adjacent high-scoring pixels into one detected grid line."""
    positions = np.flatnonzero(scores >= minimum)
    if not len(positions):
        return []
    groups: list[list[int]] = [[int(positions[0])]]
    for position in positions[1:]:
        if position <= groups[-1][-1] + 2:
            groups[-1].append(int(position))
        else:
            groups.append([int(position)])
    return [round(sum(group) / len(group)) for group in groups]


def _roster_grid_lines(image: Image.Image) -> tuple[list[int], list[int]]:
    """Detect the form's long vertical and horizontal grid lines with OpenCV."""
    grayscale = np.asarray(image.convert("L"))
    # Uneven phone-photo lighting makes one global threshold lose the thinner
    # lines on the left side of this form. A local threshold preserves them.
    ink = cv2.adaptiveThreshold(
        grayscale, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 51, 12,
    )
    height, width = ink.shape
    vertical = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(18, height // 20))),
    )
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(18, width // 28), 1)),
    )
    vertical_lines = _line_centers(
        np.count_nonzero(vertical, axis=0), minimum=max(30, height // 10)
    )
    horizontal_lines = _line_centers(
        np.count_nonzero(horizontal, axis=1), minimum=max(30, width // 4)
    )
    return vertical_lines, horizontal_lines


def _nearest_cell_bounds(lines: list[int], center: float, *, max_size: int) -> tuple[int, int] | None:
    """Return the grid interval whose centre is closest to ``center``."""
    candidates = [
        (left, right)
        for left, right in zip(lines, lines[1:])
        if 6 <= right - left <= max_size
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda bounds: abs((bounds[0] + bounds[1]) / 2 - center))


def _snap_to_roster_cell(
    bounds: tuple[float, float, float, float], *,
    vertical_lines: list[int], horizontal_lines: list[int], image_size: tuple[int, int],
) -> tuple[float, float, float, float]:
    """Snap an approximate model box to the nearest detected roster cell."""
    left, top, right, bottom = bounds
    width, height = image_size
    x_cell = _nearest_cell_bounds(
        vertical_lines, (left + right) / 2, max_size=max(20, width // 8)
    )
    y_cell = _nearest_cell_bounds(
        horizontal_lines, (top + bottom) / 2, max_size=max(20, height // 10)
    )
    if x_cell is not None:
        left, right = x_cell
    if y_cell is not None:
        top, bottom = y_cell
    return left, top, right, bottom


def highlighted_roster_crop(
    image_path: Path, destination: Path, cell_boxes: list[dict],
) -> Path:
    """Create a roster-hours crop with red circles around an employee's cells.

    ``cell_boxes`` use the 0–1000 coordinate system returned by the vision
    reader, relative to the complete, orientation-normalized source photo.
    """
    with Image.open(image_path) as original:
        image = original.convert("RGB")
        roster_bottom = round(image.height * ROSTER_HOURS_BOTTOM)
        marked = image.crop((0, 0, image.width, roster_bottom))
        draw = ImageDraw.Draw(marked)
        vertical_lines, horizontal_lines = _roster_grid_lines(marked)

        for box in cell_boxes:
            try:
                left = (float(box["left"]) + HIGHLIGHT_CELL_X_OFFSET) * image.width / 1000
                top = float(box["top"]) * image.height / 1000
                right = (float(box["right"]) + HIGHLIGHT_CELL_X_OFFSET) * image.width / 1000
                bottom = float(box["bottom"]) * image.height / 1000
            except (KeyError, TypeError, ValueError):
                continue

            left, right = sorted((left, right))
            top, bottom = sorted((top, bottom))
            if right <= left or bottom <= top or top >= roster_bottom:
                continue
            left, top, right, bottom = _snap_to_roster_cell(
                (left, top, right, bottom),
                vertical_lines=vertical_lines,
                horizontal_lines=horizontal_lines,
                image_size=marked.size,
            )
            padding = max(8, round(min(right - left, bottom - top) * 0.2))
            bounds = (
                max(0, round(left - padding)),
                max(0, round(top - padding)),
                min(marked.width - 1, round(right + padding)),
                min(marked.height - 1, round(bottom + padding)),
            )
            line_width = max(3, round(min(marked.width, marked.height) * 0.004))
            draw.ellipse(bounds, outline=(220, 35, 35), width=line_width)

        return _save_discord_crop(marked, destination)


def _roster_crops(image_path: Path, destination: Path) -> tuple[Path, Path]:
    """Create international and domestic roster views from the standard form.

    The domestic view joins the left time axis to the domestic duty columns so
    every row retains its hour label after the middle international columns are
    removed.
    """
    with Image.open(image_path) as original:
        image = original.convert("RGB")
        time_right = round(image.width * TIME_AXIS_RIGHT)
        international_right = round(image.width * INTERNATIONAL_RIGHT)
        domestic_left = round(image.width * DOMESTIC_LEFT)
        domestic_right = round(image.width * DOMESTIC_RIGHT)

        international = image.crop((0, 0, international_right, image.height))
        time_axis = image.crop((0, 0, time_right, image.height))
        domestic_columns = image.crop((domestic_left, 0, domestic_right, image.height))
        domestic = Image.new(
            "RGB", (time_axis.width + domestic_columns.width, image.height), "white"
        )
        domestic.paste(time_axis, (0, 0))
        domestic.paste(domestic_columns, (time_axis.width, 0))

        international_path = destination.with_name("international-roster.jpg")
        domestic_path = destination.with_name("domestic-roster.jpg")
        return (
            _save_discord_crop(international, international_path),
            _save_discord_crop(domestic, domestic_path),
        )


def preprocess_roster(source: Path, destination: Path) -> PreprocessResult:
    """Keep the complete source photo and generate enlarged hourly strips.

    The complete photo remains the source of truth for headers and grid geometry;
    strips only make small hourly cells easier for the vision model to review.
    """
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.save(destination, format="PNG", optimize=True)
    main_roster_path = _main_roster_crop(
        destination, destination.with_name("roster-hours.png")
    )
    international_crop_path, domestic_crop_path = _roster_crops(main_roster_path, destination)
    return PreprocessResult(
        # The vision reader and highlight coordinates use this same
        # orientation-normalized image, not the potentially EXIF-rotated upload.
        image_path=destination,
        method="full_photo_strips",
        zoom_image_paths=[
            international_crop_path,
            domestic_crop_path,
            *_hourly_verification_crops(international_crop_path, destination),
        ],
        international_crop_path=international_crop_path,
        domestic_crop_path=domestic_crop_path,
    )
