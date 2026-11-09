#!/usr/bin/env python3
"""An opt-in, cell-first roster reader.

This module intentionally does not replace the production reader.  It is the
first-stage experiment described in ``docs/phase1_cell_pipeline.md``: correct a
fixed roster, split it into cells, ask the vision model to classify *one cell*
at a time, and let Python map the resulting row/column to a duty.

The template is deliberately external.  A template is tied to one roster form;
when the form changes, re-calibrate it instead of changing the existing bot.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field, model_validator
from dotenv import load_dotenv


TARGET_CODES = ("丁", "戊", "己", "庚")
TARGET_CODE_ORDER = {code: index for index, code in enumerate(TARGET_CODES)}
PROJECT_ROOT = Path(__file__).resolve().parents[1]

def load_project_env() -> None:
    """Load the repository `.env` while preserving deployment environment values."""
    # This opt-in CLI is normally started from the repository root, but loading
    # an explicit path also makes it work from another working directory.
    load_dotenv(PROJECT_ROOT / ".env", override=False)


load_project_env()


class CellLabel(str, Enum):
    DING = "丁"
    WU = "戊"
    JI = "己"
    GENG = "庚"
    OTHER = "OTHER"
    EMPTY = "EMPTY"
    UNKNOWN = "UNKNOWN"


class ModelCellClassification(BaseModel):
    """One deliberately context-free judgment returned by the vision model."""

    row: int
    column: int
    # A cell can list several people. Non-target states are represented as a
    # one-item list, e.g. ["EMPTY"] or ["OTHER"].
    labels: list[CellLabel] = Field(min_length=1, max_length=4)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def valid_label_set(self) -> "ModelCellClassification":
        labels = set(self.labels)
        if len(labels) != len(self.labels):
            raise ValueError("labels 不可重複")
        targets = labels & target_labels()
        if targets and len(labels) != len(targets):
            raise ValueError("目標代號不可與 OTHER、EMPTY 或 UNKNOWN 混用")
        if not targets and len(labels) != 1:
            raise ValueError("無目標代號時只能回傳一個狀態")
        return self


class ModelClassificationBatch(BaseModel):
    cells: list[ModelCellClassification]


@dataclass(frozen=True)
class GridTemplate:
    """Coordinates are normalized to the perspective-corrected output image."""

    output_width: int = 2400
    output_height: int = 1600
    # Keep the uploaded image's native pixel geometry. This is the default for
    # the production-like local template because glyph shape matters more than
    # a perfectly rectangular page.
    use_original_image: bool = False
    # Perspective-register to a canvas with the photo's table aspect ratio,
    # avoiding a horizontal/vertical squeeze of every glyph.
    preserve_source_aspect_ratio: bool = False
    # Reject a perspective-corrected image when its detected grid no longer
    # matches this calibrated template. Zero disables the safeguard.
    minimum_grid_alignment_score: float = 0.0
    # Shift the source-photo crop downward by this fraction of its full cell
    # height after borders are removed. Scanned codes tend to sit low in a cell.
    source_crop_y_shift_fraction: float = 0.0
    # Extend only the lower edge while preserving the upper edge. This is the
    # preferred setting when the top is already correctly aligned.
    source_crop_bottom_extension_fraction: float = 0.0
    # Four source-image points: top-left, top-right, bottom-right, bottom-left.
    # Omit to use automatic table-corner detection.
    table_corners: tuple[tuple[float, float], ...] | None = None
    grid_x: tuple[float, ...] = ()
    grid_y: tuple[float, ...] = ()
    # Keys are grid interval indexes: grid_y[row]..grid_y[row + 1].
    row_map: dict[int, str] = field(default_factory=dict)
    # Keys are grid interval indexes: grid_x[column]..grid_x[column + 1].
    column_map: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, path: Path) -> "GridTemplate":
        payload = json.loads(path.read_text(encoding="utf-8"))
        corners = payload.get("table_corners")
        return cls(
            output_width=int(payload.get("output_width", 2400)),
            output_height=int(payload.get("output_height", 1600)),
            use_original_image=bool(payload.get("use_original_image", False)),
            preserve_source_aspect_ratio=bool(payload.get("preserve_source_aspect_ratio", False)),
            minimum_grid_alignment_score=float(payload.get("minimum_grid_alignment_score", 0.0)),
            source_crop_y_shift_fraction=float(payload.get("source_crop_y_shift_fraction", 0.0)),
            source_crop_bottom_extension_fraction=float(payload.get("source_crop_bottom_extension_fraction", 0.0)),
            table_corners=(tuple(tuple(map(float, point)) for point in corners)
                           if corners else None),
            grid_x=tuple(float(value) for value in payload.get("grid_x", [])),
            grid_y=tuple(float(value) for value in payload.get("grid_y", [])),
            row_map={int(key): str(value) for key, value in payload.get("row_map", {}).items()},
            column_map={int(key): str(value) for key, value in payload.get("column_map", {}).items()},
        )

    def to_json(self, path: Path) -> None:
        payload = asdict(self)
        payload["table_corners"] = (
            [list(point) for point in self.table_corners]
            if self.table_corners is not None else None
        )
        payload["grid_x"] = list(self.grid_x)
        payload["grid_y"] = list(self.grid_y)
        payload["row_map"] = {str(key): value for key, value in self.row_map.items()}
        payload["column_map"] = {str(key): value for key, value in self.column_map.items()}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class CellCrop:
    row: int
    column: int
    bounds: tuple[int, int, int, int]
    image: Image.Image


@dataclass(frozen=True)
class OriginalGridProjector:
    """Locate canonical grid cells while copying pixels from the original photo.

    The homography is used only to find the four corners of a cell. No image is
    warped: every crop remains an axis-aligned rectangle of the source photo.
    """

    image: Image.Image
    template: GridTemplate
    canonical_to_source: np.ndarray
    canonical_size: tuple[int, int] | None = None

    def _lines(self) -> tuple[list[int], list[int]]:
        width, height = self.canonical_size or (self.template.output_width, self.template.output_height)
        return (
            _pixel_lines(self.template.grid_x, width),
            _pixel_lines(self.template.grid_y, height),
        )

    def _source_bounds(self, *, row_start: int, row_end: int, column_start: int,
                       column_end: int, inner_fraction: float,
                       inner_fraction_x: float | None = None,
                       inner_fraction_y: float | None = None) -> tuple[int, int, int, int]:
        x_lines, y_lines = self._lines()
        points = [
            (x_lines[column_start], y_lines[row_start]),
            (x_lines[column_end], y_lines[row_start]),
            (x_lines[column_end], y_lines[row_end]),
            (x_lines[column_start], y_lines[row_end]),
        ]
        return self.project_canonical_quad(
            points, inner_fraction=inner_fraction,
            inner_fraction_x=inner_fraction_x, inner_fraction_y=inner_fraction_y,
        )

    def project_canonical_quad(self, points: list[tuple[int, int]], *,
                               inner_fraction: float = 1,
                               inner_fraction_x: float | None = None,
                               inner_fraction_y: float | None = None) -> tuple[int, int, int, int]:
        """Map a registered (possibly sloped) cell quadrilateral to raw pixels."""
        source_points = cv2.perspectiveTransform(
            np.asarray([points], dtype=np.float32), self.canonical_to_source,
        )[0]
        left, top = source_points.min(axis=0)
        right, bottom = source_points.max(axis=0)
        fraction_x = inner_fraction if inner_fraction_x is None else inner_fraction_x
        fraction_y = inner_fraction if inner_fraction_y is None else inner_fraction_y
        if not 0 < fraction_x <= 1 or not 0 < fraction_y <= 1:
            raise ValueError("inner_fraction 必須介於 0 與 1")
        width, height = right - left, bottom - top
        pad_x = width * (1 - fraction_x) / 2
        pad_y = height * (1 - fraction_y) / 2
        shift_y = height * self.template.source_crop_y_shift_fraction
        bottom_extension = height * self.template.source_crop_bottom_extension_fraction
        return (
            max(0, round(left + pad_x)), max(0, round(top + pad_y + shift_y)),
            min(self.image.width, round(right - pad_x)),
            min(self.image.height, round(bottom - pad_y + shift_y + bottom_extension)),
        )

    def crop(self, *, row_start: int, row_end: int, column_start: int, column_end: int,
             scale: int = 1, inner_fraction: float = 1,
             inner_fraction_x: float | None = None,
             inner_fraction_y: float | None = None) -> tuple[Image.Image, tuple[int, int, int, int]]:
        bounds = self._source_bounds(row_start=row_start, row_end=row_end,
                                    column_start=column_start, column_end=column_end,
                                    inner_fraction=inner_fraction,
                                    inner_fraction_x=inner_fraction_x,
                                    inner_fraction_y=inner_fraction_y)
        crop = self.image.crop(bounds)
        if scale != 1:
            crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
        return crop, bounds

    def analysis_window(self) -> Image.Image:
        return self.crop(
            row_start=min(self.template.row_map), row_end=max(self.template.row_map) + 1,
            column_start=0, column_end=len(self.template.grid_x) - 1,
        )[0]

    def mapped_cells(self, *, inner_fraction: float = 0.78,
                     inner_fraction_x: float | None = None,
                     inner_fraction_y: float | None = None) -> list[CellCrop]:
        cells: list[CellCrop] = []
        for row in sorted(self.template.row_map):
            for column in sorted(self.template.column_map):
                image, bounds = self.crop(row_start=row, row_end=row + 1,
                                          column_start=column, column_end=column + 1,
                                          scale=5, inner_fraction=inner_fraction,
                                          inner_fraction_x=inner_fraction_x,
                                          inner_fraction_y=inner_fraction_y)
                cells.append(CellCrop(row, column, bounds, image))
        return cells

    def context_crops(self, candidates: list[CellCrop]) -> list[CellCrop]:
        cells: list[CellCrop] = []
        for cell in candidates:
            image, bounds = self.crop(
                row_start=max(0, cell.row - 1), row_end=min(len(self.template.grid_y) - 1, cell.row + 2),
                column_start=max(0, cell.column - 1), column_end=min(len(self.template.grid_x) - 1, cell.column + 2),
                scale=6,
            )
            cells.append(CellCrop(cell.row, cell.column, bounds, image))
        return cells


@dataclass(frozen=True)
class ClassifiedCell:
    row: int
    column: int
    first_pass: tuple[CellLabel, ...]
    second_pass: tuple[CellLabel, ...] | None
    confidence: float

    @property
    def accepted_labels(self) -> tuple[CellLabel, ...]:
        """Precision-first: accept each code only when both passes saw it."""
        second = set(self.second_pass or ())
        return tuple(sorted((set(self.first_pass) & second) & target_labels(),
                            key=lambda label: TARGET_CODE_ORDER[label.value]))

    @property
    def first_targets(self) -> tuple[CellLabel, ...]:
        return tuple(sorted(set(self.first_pass) & target_labels(),
                            key=lambda label: TARGET_CODE_ORDER[label.value]))

    @property
    def second_targets(self) -> tuple[CellLabel, ...]:
        return tuple(sorted(set(self.second_pass or ()) & target_labels(),
                            key=lambda label: TARGET_CODE_ORDER[label.value]))


def target_labels() -> set[CellLabel]:
    return {CellLabel(code) for code in TARGET_CODES}


def _normalized_lines(lines: Iterable[int], length: int) -> tuple[float, ...]:
    return tuple(round(value / length, 8) for value in lines)


def _pixel_lines(lines: Iterable[float], length: int) -> list[int]:
    return [round(value * length) for value in lines]


def _line_centers(scores: np.ndarray, minimum: int, *, minimum_separation: int = 6) -> list[int]:
    positions = np.flatnonzero(scores >= minimum)
    if not len(positions):
        return []
    groups: list[list[int]] = [[int(positions[0])]]
    for position in positions[1:]:
        if position <= groups[-1][-1] + 2:
            groups[-1].append(int(position))
        else:
            groups.append([int(position)])
    centers = [round(sum(group) / len(group)) for group in groups]
    # Perspective interpolation can turn one printed border into several
    # nearby peaks.  Treat these as one grid line; otherwise a 3–10 px phantom
    # interval becomes a nonsensical "cell" in the calibrated template.
    merged: list[list[int]] = []
    for center in centers:
        if not merged or center - merged[-1][-1] > minimum_separation:
            merged.append([center])
        else:
            merged[-1].append(center)
    return [round(sum(group) / len(group)) for group in merged]


def detect_table_corners(image: Image.Image) -> tuple[tuple[float, float], ...] | None:
    """Find the largest quadrilateral table; return ``None`` on an unsure photo."""
    grayscale = np.asarray(image.convert("L"))
    edges = cv2.Canny(grayscale, 50, 150)
    edges = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    minimum_area = image.width * image.height * 0.20
    candidates: list[tuple[float, np.ndarray]] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < minimum_area:
            continue
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(polygon) == 4:
            candidates.append((area, polygon.reshape(4, 2).astype(np.float32)))
    if not candidates:
        return None
    _, points = max(candidates, key=lambda item: item[0])
    # Standard TL, TR, BR, BL ordering, independent of contour direction.
    sums = points.sum(axis=1)
    diffs = np.diff(points, axis=1).reshape(-1)
    ordered = np.array([
        points[np.argmin(sums)], points[np.argmin(diffs)],
        points[np.argmax(sums)], points[np.argmax(diffs)],
    ])
    return tuple((float(x), float(y)) for x, y in ordered)


# The timetable detector measures rules on a copy resized to this long side, so
# its kernels and tolerances are fixed fractions of the photo: a 1100 px LINE
# copy and a 4000 px phone original are measured alike.
_RULE_WORKING_SIZE = 1200
# Steepest table side searched (about 8.5°): a hand-held tilt plus keystone.
_MAXIMUM_SIDE_SLOPE = 0.15
# Largest slope difference between opposite sides (perspective convergence).
_MAXIMUM_SIDE_CONVERGENCE = 0.15
# Distance, in working pixels, within which a sample lies on a side: a wavy
# paper edge stays inside it, the next rule in (10+ px away) does not.
_SIDE_TOLERANCE = 4
# Share of scanlines whose outermost rule pixel must lie on the fitted side.
_MINIMUM_SIDE_SUPPORT = 0.4


def _form_rule_masks(image: Image.Image) -> tuple[np.ndarray, np.ndarray, float]:
    """Horizontal and vertical printed-rule masks of a working-size copy, and its scale.

    The form is printed in black; a date written in red over a corner and the
    red stamp are not.  The brightest RGB channel keeps black rules dark while
    making coloured ink as light as the paper, so neither breaks a border.
    """
    scale = _RULE_WORKING_SIZE / max(image.width, image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    value = cv2.resize(np.asarray(image.convert("RGB")), size,
                       interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR).max(axis=2)
    ink = cv2.adaptiveThreshold(value, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 31, 10)
    # A rule survives an opening longer than any glyph stroke only while it
    # stays inside the kernel's width: thicken it across first, so a thin rule
    # that leans a few degrees is not cut into pieces.
    across = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 3))
    horizontal = cv2.morphologyEx(cv2.dilate(ink, across), cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, size[0] // 30), 1)))
    vertical = cv2.morphologyEx(cv2.dilate(ink, across.T), cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, size[1] // 35))))
    return horizontal, vertical, scale


def _rule_pencil(mask: np.ndarray, minimum_length: float) -> tuple[float, float] | None:
    """Slope of the long rules as a linear function of where they lie.

    ``mask`` holds rules running along axis 1.  Seen in perspective, the rules
    of one direction meet in a vanishing point, so a rule's slope is linear in
    its position across them; a curled or wavy edge is an outlier to that
    trend.  Returns ``(a, b)``: ``slope = a + b * position``, the position being
    taken at the middle of the mask; ``None`` when there are too few rules.
    """
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
    middle = mask.shape[1] / 2
    positions, slopes, weights = [], [], []
    for index in range(1, count):
        x, y, width, height = (int(value) for value in stats[index, :4])
        if width < minimum_length:
            continue
        ys, xs = np.nonzero(labels[y:y + height, x:x + width] == index)
        slope, intercept = np.polyfit(xs + x, ys + y, 1)
        positions.append(slope * middle + intercept)
        slopes.append(slope)
        weights.append(np.sqrt(len(xs)))
    positions, slopes, weights = np.asarray(positions), np.asarray(slopes), np.asarray(weights)
    keep = np.ones(len(positions), dtype=bool)
    for _ in range(4):
        if np.count_nonzero(keep) < 5:
            return None
        b, a = np.polyfit(positions[keep], slopes[keep], 1, w=weights[keep])
        residuals = np.abs(slopes - (a + b * positions))
        keep = residuals <= max(0.002, 2.5 * float(np.median(residuals[keep])))
    return float(a), float(b)


def _fit_side(positions: np.ndarray, extremes: np.ndarray, pencil: tuple[float, float] | None,
              middle: float) -> tuple[float, float, float] | None:
    """Fit ``extreme = slope * position + intercept`` to one side of the table.

    ``extremes`` holds, per scanline, the outermost rule pixel on that side:
    most lie on the border, the rest on whatever interrupts it.  Every slope up
    to the tilt limit is tried and the offset with most samples nearby wins (a
    deterministic RANSAC).  The side keeps the inliers' position but takes the
    direction the other rules imply at that position, so the warped rows and
    columns come out level even when the paper edge itself is curled.
    Returns ``(slope, intercept, share of samples on the side)``.
    """
    if len(positions) < 20:
        return None
    span = float(positions.max() - positions.min()) or 1.0
    window = np.ones(2 * _SIDE_TOLERANCE + 1)
    best = (-1.0, 0.0, 0.0)
    for slope in np.arange(-_MAXIMUM_SIDE_SLOPE, _MAXIMUM_SIDE_SLOPE + 1e-9, 1.0 / span):
        offsets = np.round(extremes - slope * positions).astype(int)
        low = int(offsets.min())
        support = np.convolve(np.bincount(offsets - low), window, mode="same")
        peak = int(np.argmax(support))
        if support[peak] > best[0]:
            best = (float(support[peak]), float(slope), float(peak + low))
    _, slope, intercept = best
    inliers = np.abs(extremes - (slope * positions + intercept)) <= _SIDE_TOLERANCE
    if np.count_nonzero(inliers) < 20:
        return None
    if pencil is None:
        slope, intercept = np.polyfit(positions[inliers], extremes[inliers], 1)
    else:
        slope = pencil[0] + pencil[1] * (slope * middle + intercept)
        intercept = np.median(extremes[inliers] - slope * positions[inliers])
    return float(slope), float(intercept), float(np.count_nonzero(inliers)) / len(positions)


def detect_timetable_corners(image: Image.Image) -> tuple[tuple[float, float], ...] | None:
    """Fit the four borders of the main ruled grid, ignoring adjacent pages.

    A phone photo can contain a neighbouring sheet along one edge, and a LINE
    copy is only ~1100 px tall.  The largest Canny contour then describes both
    sheets, and fixed pixel sizes miss or break thin rules.  This detector keeps
    only the long printed rules of the largest connected grid (another sheet's
    rules are not connected to it) and fits each side to the outermost rule
    pixel of every scanline, so neither the neighbour nor the photo edge can
    pull a side, and a tilt or keystone is measured rather than assumed away.
    The quadrilateral is returned only when every side is well supported and
    opposite sides converge no more than perspective allows.
    """
    horizontal, vertical, scale = _form_rule_masks(image)
    height, width = horizontal.shape
    count, labels, stats, _ = cv2.connectedComponentsWithStats(cv2.bitwise_or(horizontal, vertical))
    if count < 2:
        return None
    component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    grid_width, grid_height = (int(value) for value in stats[component, 2:4])
    if grid_width < width * 0.4 or grid_height < height * 0.4:
        return None
    grid = labels == component
    vertical_rules = (vertical > 0) & grid
    horizontal_rules = (horizontal > 0) & grid

    def outermost(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Per row of ``mask`` holding a rule: the row, its first and its last rule pixel."""
        present = np.flatnonzero(mask.any(axis=1))
        first = np.argmax(mask[present], axis=1)
        last = mask.shape[1] - 1 - np.argmax(mask[present, ::-1], axis=1)
        return present.astype(float), first.astype(float), last.astype(float)

    rows, left_x, right_x = outermost(vertical_rules)
    columns, top_y, bottom_y = outermost(horizontal_rules.T)
    column_pencil = _rule_pencil(vertical_rules.T, 0.3 * grid_height)
    row_pencil = _rule_pencil(horizontal_rules, 0.3 * grid_width)
    sides = [_fit_side(rows, left_x, column_pencil, height / 2),
             _fit_side(rows, right_x, column_pencil, height / 2),
             _fit_side(columns, top_y, row_pencil, width / 2),
             _fit_side(columns, bottom_y, row_pencil, width / 2)]
    if any(side is None or side[2] < _MINIMUM_SIDE_SUPPORT for side in sides):
        return None
    left, right, top, bottom = sides
    if (abs(left[0] - right[0]) > _MAXIMUM_SIDE_CONVERGENCE
            or abs(top[0] - bottom[0]) > _MAXIMUM_SIDE_CONVERGENCE):
        return None

    def intersect(side: tuple[float, ...], horizontal_line: tuple[float, ...]) -> tuple[float, float]:
        side_slope, side_intercept = side[:2]
        line_slope, line_intercept = horizontal_line[:2]
        x = (side_slope * line_intercept + side_intercept) / (1 - side_slope * line_slope)
        return x, line_slope * x + line_intercept

    corners = np.asarray([
        intersect(left, top), intersect(right, top),
        intersect(right, bottom), intersect(left, bottom),
    ], dtype=np.float32)
    corners = (corners + 0.5) / scale - 0.5
    if (np.any(corners[:, 0] < -0.01 * image.width) or np.any(corners[:, 0] > 1.01 * image.width)
            or np.any(corners[:, 1] < -0.01 * image.height) or np.any(corners[:, 1] > 1.01 * image.height)
            or cv2.contourArea(corners) < image.width * image.height * 0.30):
        return None
    return tuple((float(x), float(y)) for x, y in corners)


def _registration_canvas_size(corners: tuple[tuple[float, float], ...],
                              template: GridTemplate) -> tuple[int, int]:
    """Choose a canonical canvas without stretching the photographed paper."""
    if not template.preserve_source_aspect_ratio:
        return template.output_width, template.output_height
    points = np.asarray(corners, dtype=np.float32)
    top = np.linalg.norm(points[1] - points[0])
    bottom = np.linalg.norm(points[2] - points[3])
    left = np.linalg.norm(points[3] - points[0])
    right = np.linalg.norm(points[2] - points[1])
    source_width = (top + bottom) / 2
    source_height = (left + right) / 2
    if source_height <= 0:
        return template.output_width, template.output_height
    return max(1, round(template.output_height * source_width / source_height)), template.output_height


def _perspective_matrix(corners: tuple[tuple[float, float], ...],
                        template: GridTemplate) -> tuple[np.ndarray, tuple[int, int]]:
    output_width, output_height = _registration_canvas_size(corners, template)
    source_points = np.asarray(corners, dtype=np.float32)
    destination_points = np.asarray([
        (0, 0), (output_width - 1, 0),
        (output_width - 1, output_height - 1),
        (0, output_height - 1),
    ], dtype=np.float32)
    return cv2.getPerspectiveTransform(source_points, destination_points), (output_width, output_height)


def _warp_table(image: Image.Image, corners: tuple[tuple[float, float], ...],
                template: GridTemplate) -> Image.Image:
    matrix, size = _perspective_matrix(corners, template)
    warped = cv2.warpPerspective(np.asarray(image), matrix, size,
                                 flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
    return Image.fromarray(warped)


def _horizontal_grid_deskew(image: Image.Image) -> tuple[Image.Image, np.ndarray]:
    """Remove the small residual y-vs-x slope left after perspective warp.

    The grid detector returns horizontal-line segments in the timetable body.
    A vertical shear keeps vertical column lines vertical while making those
    row boundaries level; unlike another full homography it does not squeeze
    glyphs or alter column pitch.
    """
    grayscale = np.asarray(image.convert("L"))
    height, width = grayscale.shape
    edges = cv2.Canny(grayscale, 50, 150)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=60,
        minLineLength=max(80, round(width * 0.28)), maxLineGap=25,
    )
    slopes: list[float] = []
    if lines is not None:
        for x1, y1, x2, y2 in lines[:, 0]:
            if x2 == x1 or not (height * 0.14 <= (y1 + y2) / 2 <= height * 0.62):
                continue
            slope = (y2 - y1) / (x2 - x1)
            if abs(slope) <= 0.05 and abs(x2 - x1) >= width * 0.28:
                slopes.append(float(slope))
    slope = float(np.median(slopes)) if len(slopes) >= 4 else 0.0
    if abs(slope) < 0.002:
        return image, np.eye(3, dtype=np.float64)
    slope = float(np.clip(slope, -0.04, 0.04))
    # Keep every output coordinate positive: y' = y - slope*x + offset.
    offset = max(0.0, slope * width)
    affine = np.asarray([[1, 0, 0], [-slope, 1, offset]], dtype=np.float32)
    deskewed = cv2.warpAffine(np.asarray(image), affine, (width, height),
                              flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
    affine_3x3 = np.vstack((affine, (0, 0, 1))).astype(np.float64)
    return Image.fromarray(deskewed), affine_3x3


# A detected boundary is trusted only when at least this share of the template's
# mapped rules line up after the warp.  Roster photos score from about 0.6 (a
# paper outline) to 1.0; the rectangle of a screenshot or chat bubble about 0.
_MINIMUM_REGISTRATION_SCORE = 0.4


def _registration_corners(image: Image.Image, template: GridTemplate) -> tuple[tuple[tuple[float, float], ...] | None, str]:
    """Choose the table boundary whose registered grid best matches template.

    Both the paper outline and the line-fitted timetable boundary are tried: a
    photographed sheet can have a weak or occluded paper edge while its long
    rules remain unambiguous, and falling through to a fixed-size resize would
    visibly squeeze the roster.  With a calibrated grid, the better-scoring
    boundary wins, and neither is used when too few template rules line up:
    any picture can contain a large quadrilateral, only a roster has its grid.
    """
    if template.table_corners is not None:
        # Keep the public correction-method value stable for callers that use
        # manually calibrated corners.  The grid-registration suffix is only
        # needed when automatic boundary selection chose the grid-specific
        # detector over the generic contour detector.
        return template.table_corners, "perspective"
    outer = detect_table_corners(image)
    if not (template.grid_x and template.grid_y and (template.row_map or template.column_map)):
        if outer is not None:
            return outer, "perspective"
        timetable = detect_timetable_corners(image)
        return (timetable, "perspective_grid_registration") if timetable is not None else (None, "perspective")
    scored = []
    if outer is not None:
        scored.append((grid_alignment_score(_warp_table(image, outer, template), template), outer, "perspective"))
    # A tie keeps the paper outline, so a perfect one needs no second detector.
    if not scored or scored[0][0] < 1.0:
        timetable = detect_timetable_corners(image)
        if timetable is not None:
            scored.append((grid_alignment_score(_warp_table(image, timetable, template), template),
                           timetable, "perspective_grid_registration"))
    score, corners, method = max(scored, key=lambda item: item[0], default=(0.0, None, "perspective"))
    if score < _MINIMUM_REGISTRATION_SCORE:
        return None, "perspective"
    return corners, method


def canonical_grid_registration(image: Image.Image, template: GridTemplate) -> tuple[Image.Image, str]:
    """Register an image for geometry checks without choosing OCR pixels.

    ``use_original_image`` deliberately keeps the native photograph for OCR:
    phone pixels are not squeezed into the fixed canvas.  We still need one
    canonical image to prove that the detected table boundary matches the
    calibrated grid before billing a model request.
    """
    corners, method = _registration_corners(image, template)
    if corners is None:
        resized = cv2.resize(np.asarray(image), (template.output_width, template.output_height),
                             interpolation=cv2.INTER_CUBIC)
        return Image.fromarray(resized), "resize_fallback"
    deskewed, deskew_matrix = _horizontal_grid_deskew(_warp_table(image, corners, template))
    return deskewed, (method + "_row_deskew" if not np.allclose(deskew_matrix, np.eye(3)) else method)


def correct_perspective(source: Path, template: GridTemplate) -> tuple[Image.Image, str]:
    """EXIF-normalize and warp the roster to the template canvas.

    Automatic detection is intentionally conservative.  If it cannot prove a
    table quadrilateral, it preserves the whole image instead of inventing a
    crop; the caller can then put manually measured corners in the template.
    """
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
    if template.use_original_image:
        return image, "source_original"
    return canonical_grid_registration(image, template)


def original_grid_projector(image: Image.Image, template: GridTemplate) -> OriginalGridProjector:
    """Build raw-photo grid geometry from the same corners used for calibration."""
    _require_grid(template)
    corners, _ = _registration_corners(image, template)
    if corners is None:
        raise ValueError("原圖模式找不到表格四角；請在 template 設定 table_corners")
    source_to_canonical, canonical_size = _perspective_matrix(corners, template)
    _, deskew_matrix = _horizontal_grid_deskew(_warp_table(image, corners, template))
    source_to_registered = deskew_matrix @ source_to_canonical
    return OriginalGridProjector(image, template, np.linalg.inv(source_to_registered), canonical_size)


def detect_grid_lines(image: Image.Image) -> tuple[list[int], list[int]]:
    """Detect long form lines after perspective correction, without erasing glyphs."""
    grayscale = np.asarray(image.convert("L"))
    ink = cv2.adaptiveThreshold(grayscale, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 51, 12)
    height, width = ink.shape
    vertical = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, height // 18))),
    )
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, width // 32), 1)),
    )
    separation = max(6, round(min(width, height) * 0.0075))
    x_lines = _line_centers(np.count_nonzero(vertical, axis=0), max(40, height // 8),
                            minimum_separation=separation)
    y_lines = _line_centers(np.count_nonzero(horizontal, axis=1), max(40, width // 5),
                            minimum_separation=separation)
    return x_lines, y_lines


class GridAlignmentError(ValueError):
    """The uploaded roster could not be registered to the calibrated grid."""


def grid_alignment_score(image: Image.Image, template: GridTemplate) -> float:
    """Score how well the detected long grid lines match the fixed template.

    Only the boundaries surrounding mapped cells participate. Cell contents are
    ignored, so a different day's numbers and handwritten codes cannot change
    this geometric registration check.
    """
    _require_grid(template)
    detected_x, detected_y = detect_grid_lines(image)
    if not detected_x or not detected_y:
        return 0.0
    expected_x = _pixel_lines(template.grid_x, image.width)
    expected_y = _pixel_lines(template.grid_y, image.height)
    mapped_x = sorted({edge for column in template.column_map for edge in (column, column + 1)})
    mapped_y = sorted({edge for row in template.row_map for edge in (row, row + 1)})
    tolerance = max(8, round(min(image.width, image.height) * 0.020))
    distances = [
        min(abs(expected_x[index] - value) for value in detected_x)
        for index in mapped_x
    ] + [
        min(abs(expected_y[index] - value) for value in detected_y)
        for index in mapped_y
    ]
    return sum(distance <= tolerance for distance in distances) / len(distances)


def validate_grid_alignment(image: Image.Image, template: GridTemplate) -> float:
    """Return the alignment score or reject the upload before OCR is billed."""
    score = grid_alignment_score(image, template)
    if template.minimum_grid_alignment_score and score < template.minimum_grid_alignment_score:
        # A curled sheet can score poorly in a full-height projection even
        # though sectional, template-anchored rules establish every required
        # cell. Treat that stronger geometric registration as the fallback
        # proof instead of preventing the classifier from running.
        try:
            registered_grid_lines(image, template)
        except GridAlignmentError as exc:
            raise GridAlignmentError(
                "班表格線定位失敗，無法建立可供模型辨識的格子。"
            ) from exc
        score = template.minimum_grid_alignment_score
    return score


def _complete_regular_grid_lines(lines: list[int], expected: list[int], *, required_count: int) -> list[int]:
    """Restore an isolated missing line in a regular body-grid sequence.

    A 9/17-style photo can lose one faint vertical line to thresholding.  The
    following detected lines must then not be assigned to the wrong physical
    column.  Only gaps that are very close to an integer number of the
    template's *local* regular pitch are interpolated; large header/body gaps
    are intentionally left alone.
    """
    if len(lines) < 2 or required_count < 2:
        return lines
    expected_deltas = np.diff(expected[:required_count])
    regular = expected_deltas[expected_deltas > 0]
    if len(regular) == 0:
        return lines
    pitch = float(np.median(regular))
    completed = [lines[0]]
    for left, right in zip(lines, lines[1:]):
        gap = right - left
        multiples = max(1, round(gap / pitch))
        # Do not invent lines in a deliberately wide physical column.
        if multiples > 1 and abs(gap - multiples * pitch) <= pitch * 0.28:
            completed.extend(round(left + gap * step / multiples) for step in range(1, multiples))
        completed.append(right)
    return completed


def _horizontal_table_bounds(image: Image.Image) -> tuple[int, int] | None:
    """Estimate the table's outer x extent from its many horizontal rules."""
    grayscale = np.asarray(image.convert("L"))
    height, width = grayscale.shape
    ink = cv2.adaptiveThreshold(
        grayscale, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 51, 12,
    )
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, width // 32), 1)),
    )
    support = np.count_nonzero(horizontal, axis=0)
    candidates = np.flatnonzero(support >= max(8, round(height * .015)))
    if len(candidates) < 2:
        return None
    return int(candidates[0]), int(candidates[-1])


def _align_first_duty_edge(
    image: Image.Image,
    lines: list[int],
    expected: list[int],
    *,
    first_duty_edge: int,
) -> tuple[list[int], int]:
    """Anchor semantic columns when cropped/merged left rules are invisible.

    The hourly body has a merged time/location area before the first duty
    column. Its internal header divider may not be a long vertical rule, while
    the first actual duty edge is. Locate that edge relative to the table's
    horizontal left boundary instead of assuming detected line zero is
    semantic line zero.
    """
    if first_duty_edge <= 0 or len(lines) < 2 or len(expected) <= first_duty_edge:
        return lines, 0
    bounds = _horizontal_table_bounds(image)
    if bounds is None:
        table_left = expected[0]
    else:
        table_left = bounds[0]
    deltas = np.diff(lines)
    regular = deltas[(deltas > 0) & (deltas < np.median(deltas) * 1.7)]
    if len(regular) == 0:
        return lines, 0
    pitch = float(np.median(regular))
    predicted_anchor = table_left + first_duty_edge * pitch
    anchor_index = min(
        range(len(lines)), key=lambda index: abs(lines[index] - predicted_anchor),
    )
    if abs(lines[anchor_index] - predicted_anchor) > pitch * .65:
        return lines, 0
    if anchor_index == first_duty_edge:
        return lines, 0
    anchor = lines[anchor_index]
    prefix = [
        round(table_left + (anchor - table_left) * index / first_duty_edge)
        for index in range(first_duty_edge)
    ]
    return [*prefix, *lines[anchor_index:]], anchor_index - first_duty_edge


def _sectional_template_vertical_lines(
    image: Image.Image,
    detected: list[int],
    expected: list[int],
    *,
    required_count: int,
) -> tuple[list[int], list[bool]]:
    """Recover curved/slanted column rules from several timetable bands.

    A single full-height projection loses rules on a curled sheet because the
    same rule moves sideways as it travels down the photo.  The registered
    template supplies the column identity; short horizontal bands supply local
    observations.  Missing observations retain the template prediction so one
    faint rule cannot abort inference.
    """
    targets = np.asarray(expected[:required_count], dtype=float)
    if len(targets) < 2:
        return [round(value) for value in targets], [False] * len(targets)
    pitch = float(np.median(np.diff(targets)))
    tolerance = max(5.0, pitch * .42)

    # Estimate the day's small horizontal scale/translation from unambiguous
    # full-height rules. Refit after rejecting glyph strokes and duplicate use.
    pairs: list[tuple[float, float]] = []
    used: set[int] = set()
    for target in targets:
        candidates = [(abs(value - target), index, value)
                      for index, value in enumerate(detected) if index not in used]
        if not candidates:
            continue
        distance, index, value = min(candidates)
        if distance <= tolerance:
            pairs.append((target, float(value)))
            used.add(index)
    scale, offset = 1.0, 0.0
    if len(pairs) >= 4:
        source, observed = map(np.asarray, zip(*pairs))
        scale, offset = map(float, np.polyfit(source, observed, 1))
        residual = np.abs(observed - (scale * source + offset))
        keep = residual <= max(4.0, pitch * .18)
        if int(np.count_nonzero(keep)) >= 4:
            scale, offset = map(float, np.polyfit(source[keep], observed[keep], 1))
    predicted = scale * targets + offset

    gray = np.asarray(image.convert("L"))
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 31, 10)
    height, width = ink.shape
    top, bottom = round(height * .18), round(height * .66)
    reference = height * .48
    band_height = max(50, round((bottom - top) / 5))
    observations: list[list[tuple[float, float]]] = [[] for _ in predicted]
    for band_top in range(top, bottom, band_height):
        band_bottom = min(bottom, band_top + band_height)
        band = ink[band_top:band_bottom]
        vertical = cv2.morphologyEx(
            band, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(18, band.shape[0] // 3))),
        )
        peaks = _line_centers(
            np.count_nonzero(vertical, axis=0), max(12, band.shape[0] // 5),
            minimum_separation=max(4, round(pitch * .18)),
        )
        center_y = (band_top + band_bottom) / 2
        for index, target in enumerate(predicted):
            nearby = [value for value in peaks if abs(value - target) <= tolerance]
            if nearby:
                observations[index].append((center_y, float(min(nearby, key=lambda value: abs(value-target)))))

    recovered: list[int] = []
    supported: list[bool] = []
    for target, samples in zip(predicted, observations):
        if len(samples) >= 2:
            ys, xs = map(np.asarray, zip(*samples))
            slope, intercept = map(float, np.polyfit(ys, xs, 1))
            value = intercept + slope * reference
            # A character stroke may recur in two bands. Never let local
            # evidence pull a known template boundary by half a cell.
            if abs(value - target) > tolerance:
                value = target
            recovered.append(round(value))
            supported.append(True)
        else:
            recovered.append(round(target))
            supported.append(False)

    # Preserve strict ordering even where two noisy local fits converge.
    minimum_gap = max(3, round(pitch * .45))
    for index in range(1, len(recovered)):
        recovered[index] = max(recovered[index], recovered[index - 1] + minimum_gap)
    recovered = [min(width - 1, max(0, value)) for value in recovered]
    return recovered, supported


def align_semantic_row_lines(detected: list[int], expected: list[int], *,
                             first_body_row: int) -> tuple[list[int], int]:
    """Align hourly row indexes when a photo omits a header separator.

    The tall column-heading area creates the largest early vertical gap. Its
    lower edge is a stable semantic anchor even when one of the short header
    rules above it is missing. Return aligned lines plus the detected index
    offset for diagnostics.
    """
    search_count = min(first_body_row + 3, len(detected) - 1, len(expected) - 1)
    if search_count < 2:
        return detected, 0
    detected_anchor = max(range(search_count),
                          key=lambda index: detected[index + 1] - detected[index]) + 1
    expected_anchor = max(range(search_count),
                          key=lambda index: expected[index + 1] - expected[index]) + 1
    offset = detected_anchor - expected_anchor
    if offset == 0:
        return detected, 0
    aligned = detected.copy()
    for index in range(first_body_row, len(aligned)):
        source_index = index + offset
        if 0 <= source_index < len(detected):
            aligned[index] = detected[source_index]
    return aligned, offset


def registered_grid_lines(image: Image.Image, template: GridTemplate) -> tuple[list[int], list[int]]:
    """Return the photo's real grid boundaries keyed by physical grid index.

    ``row_map`` and ``column_map`` remain semantic physical indexes (so C17
    never becomes the ninth selected column).  The pixel edges come from this
    day's registered image rather than from 9/16's fixed pixel pitch.
    """
    _require_grid(template)
    detected_x, detected_y = detect_grid_lines(image)
    required_x = max(edge for column in template.column_map for edge in (column, column + 1)) + 1
    required_y = max(template.row_map) + 2
    expected_x = _pixel_lines(template.grid_x, image.width)
    if len(detected_y) < required_y:
        raise GridAlignmentError("找不到足夠的班表格線，請重新拍攝完整主表格。")
    completed_x = _complete_regular_grid_lines(detected_x, expected_x, required_count=required_x)
    recovered_support: list[bool] | None = None
    if len(completed_x) < required_x:
        completed_x, recovered_support = _sectional_template_vertical_lines(
            image, detected_x, expected_x, required_count=required_x,
        )
    # The divider immediately before the first duty column is the semantic
    # anchor for every column index.  It is safe to interpolate the invisible
    # divider inside the merged time/location heading (C01), but not C02
    # itself: inventing C02 shifts every duty one cell while still producing a
    # visually regular grid (the 9/12 regression).
    if template.column_map:
        first_duty_edge = min(template.column_map)
        completed_x, _ = _align_first_duty_edge(
            image, completed_x, expected_x, first_duty_edge=first_duty_edge,
        )
        if len(completed_x) < required_x:
            completed_x, recovered_support = _sectional_template_vertical_lines(
                image, completed_x, expected_x, required_count=required_x,
            )
        local_pitch = float(np.median(np.diff(completed_x[:required_x])))
        anchor = completed_x[first_duty_edge]
        sectional_support_is_strong = (
            recovered_support is not None
            and sum(recovered_support) >= max(3, required_x // 3)
        )
        nearest_detected = min(
            (abs(anchor - detected) for detected in detected_x), default=float("inf")
        )
        if not sectional_support_is_strong and nearest_detected > local_pitch * 0.25:
            raise GridAlignmentError(
                "找不到第一個勤務欄的實際垂直格線，無法安全判定欄位索引。"
            )
    return completed_x, detected_y


def registered_row_models(image: Image.Image, y_lines: list[int]) -> list[tuple[float, float]]:
    """Fit each row line from three independent horizontal projections.

    A full-width Hough segment can be pulled toward a neighbouring rule when
    the left side is skewed.  Measuring the left, middle, and right bands
    independently keeps the 06–09 and 16–18 row boundaries tied to the line
    visible under the target cells.
    """
    grayscale = np.asarray(image.convert("L"))
    height, width = grayscale.shape
    ink = cv2.adaptiveThreshold(grayscale, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 51, 12)
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, width // 32), 1)),
    )
    bands = ((0, width // 2), (width // 4, width * 3 // 4), (width // 2, width))
    band_centers = [(left + right) / 2 for left, right in bands]
    band_lines = [
        _line_centers(
            np.count_nonzero(horizontal[:, left:right], axis=1),
            max(20, (right - left) // 5),
            minimum_separation=max(6, round(min(width, height) * 0.0075)),
        )
        for left, right in bands
    ]
    models: list[tuple[float, float]] = []
    for y in y_lines:
        samples = [
            (x, min(lines, key=lambda candidate: abs(candidate - y)))
            for x, lines in zip(band_centers, band_lines)
            if lines and abs(min(lines, key=lambda candidate: abs(candidate - y)) - y) <= 14
        ]
        if len(samples) >= 2:
            x_values, y_values = zip(*samples)
            slope, intercept = np.polyfit(x_values, y_values, 1)
            models.append((float(intercept), float(slope)))
        else:
            models.append((float(y), 0.0))
    return models


def registered_column_models(image: Image.Image, x_lines: list[int]) -> list[tuple[float, float]]:
    """Fit x = intercept + slope*y from long rules in the timetable body.

    Match segments at the middle of the body, where the full-image projection
    is anchored. Exclude the unrelated name-table grid below the hourly rows.
    """
    gray = np.asarray(image.convert("L"))
    height, width = gray.shape
    top, bottom = round(height * .18), round(height * .65)
    edges = cv2.Canny(gray[top:bottom], 50, 150)
    segments = cv2.HoughLinesP(edges, 1, np.pi / 1800, threshold=35,
                               minLineLength=max(40, (bottom-top)//2), maxLineGap=35)
    reference = height * .48
    candidates = []
    if segments is not None:
        for x1, y1, x2, y2 in segments[:, 0]:
            if abs(y2-y1) < 5 * abs(x2-x1):
                continue
            slope = float(x2-x1) / float(y2-y1)
            intercept = float(x1) - slope * (float(y1)+top)
            candidates.append((intercept, slope, abs(float(y2-y1))))
    models = []
    for index, x in enumerate(x_lines):
        pitch = min([abs(x-other) for other in x_lines if other != x], default=width*.03)
        matches = [(a,b,w) for a,b,w in candidates if abs(a+b*reference-x) < pitch*.48]
        if matches:
            # Longest supporting rule is the seed for the local-band refinement.
            a,b,_ = max(matches, key=lambda item: item[2])
            models.append((a,b))
        else:
            models.append((float(x), 0.0))
    # Local bands retain slightly curved/faint rules that a single long Hough
    # segment misses. Follow from the body centre outwards to avoid hopping to
    # a neighbouring column when the top has moved by half a cell width.
    ink = cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV,31,10)
    bands = []
    for y in range(top+40,bottom-39,60):
        band = ink[y-40:y+40]
        vertical = cv2.morphologyEx(band,cv2.MORPH_OPEN,
                      cv2.getStructuringElement(cv2.MORPH_RECT,(1,25)))
        peaks = _line_centers(np.count_nonzero(vertical,axis=0),30,minimum_separation=5)
        bands.append((y,peaks))
    for index,(a,b) in enumerate(models):
        pitch = min([abs(x_lines[index]-x) for x in x_lines if x!=x_lines[index]],default=30)
        samples=[]
        for y,peaks in sorted(bands,key=lambda item:abs(item[0]-reference)):
            predicted=a+b*y
            if peaks:
                x=min(peaks,key=lambda v:abs(v-predicted))
                if abs(x-predicted)<pitch*.48:
                    samples.append((y,x))
                    if len(samples)>=3:
                        b,a=map(float,np.polyfit(*zip(*samples),1))
        if len(samples)>=3:
            models[index]=(a,b)
    # A handwritten slash can form a long segment inside one cell. Real
    # adjacent form rules have similar slopes; reject that isolated outlier.
    slopes = [b for a,b in models]
    for index,(a,b) in enumerate(models):
        neighbours = slopes[max(0,index-2):index] + slopes[index+1:index+3]
        if neighbours and abs(b-float(np.median(neighbours))) > .025:
            slope = float(np.median(neighbours))
            offsets=[]
            for y,peaks in bands:
                predicted=x_lines[index]+slope*(y-reference)
                if peaks:
                    x=min(peaks,key=lambda value:abs(value-predicted))
                    if abs(x-predicted)<8:
                        offsets.append(x-slope*y)
            models[index]=(float(np.median(offsets)) if offsets else x_lines[index]-slope*reference,slope)
    # A glyph stroke can survive both the line detector and slope checks.
    # For a locally regular pair of columns, recheck an off-centre divider
    # against actual ink near the midpoint of its two reliable neighbours.
    snapshot = list(models)
    pitch = float(np.median(np.diff(x_lines)))
    for index in range(2, len(models)-1):
        left_a,left_b = snapshot[index-1]
        right_a,right_b = snapshot[index+1]
        a,b = snapshot[index]
        expected_a,expected_b = (left_a+right_a)/2, (left_b+right_b)/2
        span = (right_a+right_b*reference)-(left_a+left_b*reference)
        if abs(span-2*pitch) > pitch*.20:
            continue  # Deliberately wide/narrow columns must not be regularized.
        if max(abs((a-expected_a)+(b-expected_b)*y) for y in (top,bottom)) <= pitch*.25:
            continue
        samples = []
        for y,peaks in bands:
            predicted = expected_a+expected_b*y
            if peaks:
                x = min(peaks,key=lambda value:abs(value-predicted))
                if abs(x-predicted) <= pitch*.20:
                    samples.append((y,x))
        if len(samples) >= 4:
            new_b,new_a = map(float,np.polyfit(*zip(*samples),1))
            if max(abs((new_a-expected_a)+(new_b-expected_b)*y)
                   for y in (top,bottom)) <= pitch*.20:
                models[index] = (new_a,new_b)
    # The hourly time axis merges the first two header subcolumns. Its
    # internal header divider is absent in the body: preserve it by interpolation.
    if len(models) > 28:
        models[1] = tuple((left+right)/2 for left,right in zip(models[0],models[2]))
    crossings = [
        (index, y)
        for y in (top, bottom)
        for index, ((a, b), (c, d)) in enumerate(zip(models, models[1:]))
        if a + b * y >= c + d * y
    ]
    if crossings:
        # Independent local fits can cross on a curled page when one band is
        # pulled toward a glyph. Keep the template-registered centres and use
        # the robust common page slant. Parallel boundaries cannot exchange
        # column identity, so inference can continue without shifting duties.
        shared_slope = float(np.median([b for _, b in models]))
        shared_slope = max(-.12, min(.12, shared_slope))
        models = [
            (float(x) - shared_slope * reference, shared_slope)
            for x in x_lines
        ]
    return models


def cell_polygon(row: int, column: int, grid_lines: tuple[list[int], list[int]],
                 row_models=None, column_models=None) -> list[tuple[float, float]]:
    """Intersect the fitted horizontal and vertical rules at all four corners."""
    xs, ys = grid_lines
    points = []
    for r,c in ((row,column),(row,column+1),(row+1,column+1),(row+1,column)):
        a,b = column_models[c] if column_models is not None else (xs[c],0.0)
        d,e = row_models[r] if row_models is not None else (ys[r],0.0)
        x = (a+b*d)/(1-b*e)
        points.append((x,d+e*x))
    return points


def _row_y(row_models: list[tuple[float, float]] | None, index: int, x: float,
           fallback: list[int]) -> int:
    if row_models is None:
        return fallback[index]
    intercept, slope = row_models[index]
    return round(intercept + slope * x)


def calibrated_template(source: Path, template: GridTemplate | None = None) -> tuple[GridTemplate, str]:
    """Return a detected-grid template which a human can label once."""
    base = template or GridTemplate()
    corrected, method = correct_perspective(source, base)
    x_lines, y_lines = detect_grid_lines(corrected)
    if len(x_lines) < 2 or len(y_lines) < 2:
        raise ValueError("找不到足夠的表格線；請在 template 的 table_corners 手動校正後重試")
    return GridTemplate(
        output_width=base.output_width,
        output_height=base.output_height,
        use_original_image=base.use_original_image,
        preserve_source_aspect_ratio=base.preserve_source_aspect_ratio,
        minimum_grid_alignment_score=base.minimum_grid_alignment_score,
        source_crop_y_shift_fraction=base.source_crop_y_shift_fraction,
        source_crop_bottom_extension_fraction=base.source_crop_bottom_extension_fraction,
        table_corners=base.table_corners,
        grid_x=_normalized_lines(x_lines, corrected.width),
        grid_y=_normalized_lines(y_lines, corrected.height),
        row_map=base.row_map,
        column_map=base.column_map,
    ), method


def _require_grid(template: GridTemplate) -> None:
    if len(template.grid_x) < 2 or len(template.grid_y) < 2:
        raise ValueError("template 必須先包含至少兩條 grid_x 與 grid_y；先執行 calibrate")
    if any(right <= left for left, right in zip(template.grid_x, template.grid_x[1:])):
        raise ValueError("template 的 grid_x 必須遞增")
    if any(bottom <= top for top, bottom in zip(template.grid_y, template.grid_y[1:])):
        raise ValueError("template 的 grid_y 必須遞增")


def analysis_time_window(image: Image.Image, template: GridTemplate, *,
                         y_lines: list[int] | None = None,
                         row_models: list[tuple[float, float]] | None = None) -> tuple[Image.Image, tuple[int, int]]:
    """Crop to the configured hourly rows before any cell image is made.

    Template coordinates remain relative to the full corrected canvas, so the
    returned origin accompanies the cropped image. This excludes the name list
    and unneeded hours from the 06:00–18:00 experiment.
    """
    _require_grid(template)
    if not template.row_map:
        raise ValueError("template 必須至少設定一個 row_map 才能裁切時段")
    y_lines = y_lines or _pixel_lines(template.grid_y, image.height)
    first_row, last_row = min(template.row_map), max(template.row_map)
    if first_row < 0 or last_row + 1 >= len(y_lines):
        raise ValueError("row_map 超出 grid")
    # A sloped left-side row boundary can sit above the projection's centre y.
    # Include both image edges so the first/last cells are never clipped before
    # their local row model is applied in ``crop_mapped_cells``.
    top = min(_row_y(row_models, first_row, x, y_lines) for x in (0, image.width - 1))
    bottom = max(_row_y(row_models, last_row + 1, x, y_lines) for x in (0, image.width - 1))
    return image.crop((0, top, image.width, bottom)), (0, top)


def crop_mapped_cells(image: Image.Image, template: GridTemplate, *, scale: int = 5,
                      inner_fraction: float = 0.78, origin: tuple[int, int] = (0, 0),
                      inner_fraction_x: float | None = None,
                      inner_fraction_y: float | None = None,
                      canvas_size: tuple[int, int] | None = None,
                      grid_lines: tuple[list[int], list[int]] | None = None,
                      row_models: list[tuple[float, float]] | None = None,
                      column_models: list[tuple[float, float]] | None = None) -> list[CellCrop]:
    """Crop only mapped body cells and discard their borders rather than lines.

    Keeping the character strokes intact is especially important for ``丁``.
    """
    _require_grid(template)
    fraction_x = inner_fraction if inner_fraction_x is None else inner_fraction_x
    fraction_y = inner_fraction if inner_fraction_y is None else inner_fraction_y
    if not 0 < fraction_x <= 1 or not 0 < fraction_y <= 1:
        raise ValueError("inner_fraction 必須介於 0 與 1")
    canvas_width, canvas_height = canvas_size or image.size
    x_lines, y_lines = grid_lines or (
        _pixel_lines(template.grid_x, canvas_width),
        _pixel_lines(template.grid_y, canvas_height),
    )
    cells: list[CellCrop] = []
    for row in sorted(template.row_map):
        for column in sorted(template.column_map):
            if row < 0 or column < 0 or row + 1 >= len(y_lines) or column + 1 >= len(x_lines):
                raise ValueError(f"row/column mapping 超出 grid: row={row}, column={column}")
            polygon = cell_polygon(row, column, (x_lines, y_lines), row_models, column_models)
            left = round((polygon[0][0] + polygon[3][0]) / 2)
            right = round((polygon[1][0] + polygon[2][0]) / 2)
            top = round((polygon[0][1] + polygon[1][1]) / 2)
            bottom = round((polygon[2][1] + polygon[3][1]) / 2)
            width, height = right - left, bottom - top
            if width < 2 or height < 2:
                raise GridAlignmentError("勤務格尺寸異常，無法安全裁切。")
            pad_x = round(width * (1 - fraction_x) / 2)
            pad_y = round(height * (1 - fraction_y) / 2)
            origin_x, origin_y = origin
            interior = image.crop((left - origin_x + pad_x, top - origin_y + pad_y,
                                   right - origin_x - pad_x, bottom - origin_y - pad_y))
            enlarged = interior.resize((interior.width * scale, interior.height * scale), Image.Resampling.LANCZOS)
            cells.append(CellCrop(row, column, (left, top, right, bottom), enlarged))
    return cells


def _review_font(size: int) -> ImageFont.ImageFont:
    """Use a CJK-capable font for review artifacts when the host provides one."""
    for font_path in (
        "/System/Library/Fonts/PingFang.ttc",  # macOS
        "/System/Library/Fonts/STHeiti Light.ttc",  # older macOS
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux
    ):
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_centered_text(draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int], text: str,
                        *, font: ImageFont.ImageFont, fill: str = "black", bold: bool = False) -> None:
    """Draw text centred in a table cell without depending on Pillow anchors."""
    left, top, right, bottom = bounds
    stroke_width = 1 if bold else 0
    box = draw.multiline_textbbox((0, 0), text, font=font, align="center", spacing=2,
                                  stroke_width=stroke_width)
    text_width, text_height = box[2] - box[0], box[3] - box[1]
    draw.multiline_text(
        ((left + right - text_width) / 2, (top + bottom - text_height) / 2),
        text,
        font=font,
        fill=fill,
        align="center",
        spacing=2,
        stroke_width=stroke_width,
        stroke_fill=fill,
    )


def render_grid_debug(image: Image.Image, template: GridTemplate,
                       grid_lines: tuple[list[int], list[int]],
                       row_models: list[tuple[float, float]] | None = None) -> Image.Image:
    """Render the detected physical grid and mapped target cells for review."""
    x_lines, y_lines = grid_lines
    column_models = registered_column_models(image, x_lines)
    debug = image.copy()
    draw = ImageDraw.Draw(debug)
    for a,b in column_models:
        draw.line((a, 0, a+b*(debug.height-1), debug.height - 1), fill=(245, 158, 11), width=1)
    for y in y_lines:
        draw.line((0, y, debug.width - 1, y), fill=(245, 158, 11), width=1)
    font = _review_font(max(10, round(min(debug.size) * 0.010)))
    for column in sorted(template.column_map):
        left, right = x_lines[column], x_lines[column + 1]
        draw.text(((left + right) / 2, max(0, y_lines[min(template.row_map)] - font.size - 2)),
                  f"C{column:02d}", fill=(37, 99, 235), font=font, anchor="mt")
    for row in sorted(template.row_map):
        for column in sorted(template.column_map):
            left, right = x_lines[column], x_lines[column + 1]
            draw.polygon(
                cell_polygon(row, column, grid_lines, row_models, column_models),
                outline=(37, 99, 235), width=2,
            )
    return debug


def render_corrected_roster_preview(
    image: Image.Image,
    template: GridTemplate,
    grid_lines: tuple[list[int], list[int]],
    row_models: list[tuple[float, float]] | None = None,
) -> Image.Image:
    """Render the user-facing, flattened roster through 19:00.

    Unlike :func:`render_grid_debug`, this image deliberately omits the amber
    detector lines.  Blue outlines identify only the cells that are actually
    read by the roster classifier.  The full table width and header are kept,
    while rows below the 19:00 boundary are removed.
    """
    x_lines, detected_y = grid_lines
    # Use the same semantic-row anchor as the recognition crop.  A daily photo
    # can omit one short header separator; indexing the raw detected lines in
    # that case draws every blue box one hour too low even though inference
    # itself used the correctly aligned cells.
    expected_y = _pixel_lines(template.grid_y, image.height)
    semantic_y, _ = align_semantic_row_lines(
        detected_y, expected_y, first_body_row=min(template.row_map),
    )
    display_row_end = min(
        max(template.row_map) + 1, len(expected_y) - 1, len(semantic_y) - 1,
    )
    y_lines = semantic_y
    try:
        column_models = registered_column_models(image, x_lines)
    except GridAlignmentError:
        # This is a visual preview, not the OCR crop geometry.  On a warped
        # photo, an isolated glyph can confuse the per-line slope fitter even
        # though the registered x positions are sound; straight blue boxes are
        # preferable to rejecting an otherwise useful flattened preview.
        column_models = [(float(x), 0.0) for x in x_lines]
    row_models = row_models or registered_row_models(image, y_lines)
    bottom = min(
        image.height,
        round(max(
            _row_y(row_models, display_row_end, x, y_lines)
            for x in (0, image.width - 1)
        )),
    )
    preview = image.crop((0, 0, image.width, bottom))
    draw = ImageDraw.Draw(preview)
    line_width = max(2, round(min(preview.width, preview.height) * 0.002))
    blue = (37, 99, 235)
    for row in sorted(template.row_map):
        for column in sorted(template.column_map):
            points = [
                (max(0, min(preview.width - 1, x)), max(0, min(preview.height - 1, y)))
                for x, y in cell_polygon(
                    row, column, grid_lines, row_models, column_models
                )
            ]
            draw.line([*points, points[0]], fill=blue, width=line_width, joint="curve")
    return preview


def _export_grid_debug(destination: Path, image: Image.Image, template: GridTemplate,
                       grid_lines: tuple[list[int], list[int]],
                       row_models: list[tuple[float, float]] | None = None) -> Path:
    debug = render_grid_debug(image, template, grid_lines, row_models)
    path = destination / "01-grid-debug.jpg"
    debug.save(path, format="JPEG", quality=92, optimize=True)
    return path


def export_crop_artifacts(destination: Path, *, corrected: Image.Image, analysis_image: Image.Image,
                          cells: list[CellCrop], template: GridTemplate,
                          grid_lines: tuple[list[int], list[int]] | None = None,
                          row_models: list[tuple[float, float]] | None = None,
                          compact: bool = False) -> dict[str, str]:
    """Save every intermediate image for human review without overwriting files."""
    if destination.exists():
        raise FileExistsError(f"裁切輸出資料夾已存在，拒絕覆寫：{destination}")
    destination.mkdir(parents=True)
    corrected_path = destination / (
        ("00-source-original.jpg" if template.use_original_image else "00-perspective-corrected.jpg")
        if compact else
        ("00-source-original.png" if template.use_original_image else "00-perspective-corrected.png")
    )
    window_path = destination / ("01-analysis-window-06-18.jpg" if compact else
                                 "01-analysis-window-06-18.png")
    cells_dir = destination / "cells"
    if compact:
        corrected.convert("RGB").save(corrected_path, format="JPEG", quality=88)
        analysis_image.convert("RGB").save(window_path, format="JPEG", quality=88)
    else:
        cells_dir.mkdir()
        corrected.save(corrected_path, format="PNG", optimize=True)
        analysis_image.save(window_path, format="PNG", optimize=True)
    grid_debug_path = (_export_grid_debug(destination, corrected, template, grid_lines, row_models)
                       if grid_lines is not None else None)

    # Keep the contact sheet's geometry identical to the Markdown table: one
    # time row per horizontal line, a duty header above each crop, and a time
    # label at the far left. This makes visual comparison unambiguous.
    tile_width, tile_height = 220, 150
    time_width, header_height = 125, 100
    mapped_rows = sorted(template.row_map)
    mapped_columns = sorted(template.column_map)
    headers = _markdown_column_headers(template)
    sheet = Image.new(
        "RGB",
        (time_width + len(mapped_columns) * tile_width,
         header_height + len(mapped_rows) * tile_height),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    header_font = _review_font(24)
    time_font = _review_font(30)
    grid_color = "#777777"
    _draw_centered_text(draw, (0, 0, time_width, header_height), "時段", font=header_font, bold=True)
    for position, column in enumerate(mapped_columns):
        left = time_width + position * tile_width
        _draw_centered_text(
            draw,
            (left, 0, left + tile_width, header_height),
            f"{headers[column]}\nc{column:02d}",
            font=header_font,
            bold=True,
        )
    cells_by_index = {(cell.row, cell.column): cell for cell in cells}
    index_lines = ["| File | Time | Column | Duty | Source bounds |", "| --- | --- | --- | --- | --- |"]
    for row_position, row in enumerate(mapped_rows):
        top = header_height + row_position * tile_height
        _draw_centered_text(draw, (0, top, time_width, top + tile_height),
                            template.row_map[row], font=time_font, bold=True)
        for column_position, column in enumerate(mapped_columns):
            cell = cells_by_index[(row, column)]
            filename = f"r{cell.row:02d}_c{cell.column:02d}.png"
            if not compact:
                cell.image.save(cells_dir / filename, format="PNG", optimize=True)
            left = time_width + column_position * tile_width
            thumbnail = cell.image.copy()
            thumbnail.thumbnail((tile_width - 12, tile_height - 12), Image.Resampling.LANCZOS)
            sheet.paste(thumbnail, (left + (tile_width - thumbnail.width) // 2,
                                   top + (tile_height - thumbnail.height) // 2))
            source_left, source_top, source_right, source_bottom = cell.bounds
            cell_reference = filename if compact else f"[cells/{filename}](cells/{filename})"
            index_lines.append(
                f"| {cell_reference} | {template.row_map[cell.row]} | "
                f"{cell.column} | {template.column_map[cell.column]} | "
                f"{source_left}, {source_top}, {source_right}, {source_bottom} |"
            )

    for x in [0, time_width, *(time_width + index * tile_width for index in range(1, len(mapped_columns) + 1))]:
        draw.line((x, 0, x, sheet.height - 1), fill=grid_color, width=1)
    for y in [0, header_height, *(header_height + index * tile_height for index in range(1, len(mapped_rows) + 1))]:
        draw.line((0, y, sheet.width - 1, y), fill=grid_color, width=1)

    # The individual enlarged PNGs remain the actual model inputs; the sheet
    # is solely a labelled, Markdown-shaped visual index.
    contact_sheet_path = destination / "02-cell-contact-sheet.jpg"
    index_path = destination / "cells.md"
    sheet.save(contact_sheet_path, format="JPEG", quality=92, optimize=True)
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    artifacts = {
        "directory": str(destination),
        "corrected_image": str(corrected_path),
        "analysis_window": str(window_path),
        "contact_sheet": str(contact_sheet_path),
        "index": str(index_path),
    }
    if not compact:
        artifacts["cell_directory"] = str(cells_dir)
    if grid_debug_path is not None:
        artifacts["grid_debug"] = str(grid_debug_path)
    return artifacts


def _image_data_url(image: Image.Image) -> str:
    import io
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


CLASSIFIER_INSTRUCTIONS = """
You are a context-free Chinese glyph classifier. Each supplied image is exactly
one roster cell after its border was removed. For every requested (row, column),
return every target glyph visibly present in that one cell. ``labels`` may be a
non-empty list containing any combination of 丁, 戊, 己, 庚; for example, a cell
with 丁 and 戊 must return ["丁", "戊"]. If no target glyph is present, return
exactly one of OTHER, EMPTY, or UNKNOWN. Do not use neighboring cells, roster
rules, expected hours, or sequence patterns. Do not infer a missing character.

False positives are worse than UNKNOWN. A target must be a complete, standalone
Chinese glyph, not a visual fragment or a merely similar character. In
particular: never read the Latin letters I/H/M/B/F/D, Arabic numerals, or a
digit/letter's serif as 丁. 丁 has a distinct top horizontal stroke connected to
a central vertical stroke. Do not confuse 庚 with similar Chinese characters
such as 乙 or 丙; return 庚 only when its full traditional glyph is visible.
Cells often contain two stacked entries. Inspect both entries independently:
digits or Latin characters elsewhere in a cell do not invalidate a genuine 丁
or 庚 in the other entry.
""".strip()

CONFIRMATION_INSTRUCTIONS = """
You are a precision-first Chinese glyph classifier. Each supplied image shows a
target roster cell plus a small surrounding context; classify the *center cell
only*. Return every target glyph visibly in the center cell as a list containing
any combination of 丁, 戊, 己, 庚. If none is present, return exactly one of
OTHER, EMPTY, or UNKNOWN. Never borrow a glyph from a neighboring cell and
never infer from roster patterns.

Treat 丁 and 庚 as strict exact-glyph checks. Do not label Latin I/H/M/B/F/D,
Arabic numerals, serifs, 乙, 丙, or other lookalikes as either code. A real 丁
or 庚 can appear above or below a number in the same cell, so judge each stacked
entry independently. When uncertain, use UNKNOWN rather than adding 丁 or 庚.
""".strip()


def classify_cells(cells: list[CellCrop], *, model: str, client: OpenAI | None = None,
                   batch_size: int = 48, confirmation: bool = False) -> dict[tuple[int, int], ModelCellClassification]:
    """Classify image crops in bounded batches, filling missing model replies with UNKNOWN."""
    if batch_size < 1:
        raise ValueError("batch_size 必須大於 0")
    if not cells:
        return {}
    if client is None and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("尚未設定 OPENAI_API_KEY")
    api = client or OpenAI(timeout=180, max_retries=0)
    instructions = CONFIRMATION_INSTRUCTIONS if confirmation else CLASSIFIER_INSTRUCTIONS
    output: dict[tuple[int, int], ModelCellClassification] = {}
    for start in range(0, len(cells), batch_size):
        batch = cells[start:start + batch_size]
        requested = [(cell.row, cell.column) for cell in batch]
        content: list[dict] = [{
            "type": "input_text",
            "text": "Classify only these cells: " + json.dumps(requested, ensure_ascii=False),
        }]
        for cell in batch:
            content.append({"type": "input_text", "text": f"cell row={cell.row}, column={cell.column}"})
            content.append({"type": "input_image", "image_url": _image_data_url(cell.image), "detail": "original"})
        response = api.responses.parse(
            model=model,
            instructions=instructions,
            input=[{"role": "user", "content": content}],
            text_format=ModelClassificationBatch,
        )
        parsed = response.output_parsed
        returned = {(item.row, item.column): item for item in parsed.cells} if parsed else {}
        for row, column in requested:
            output[(row, column)] = returned.get(
                (row, column),
                ModelCellClassification(row=row, column=column, labels=[CellLabel.UNKNOWN], confidence=0),
            )
    return output


def context_crops(image: Image.Image, template: GridTemplate, candidates: list[CellCrop], *, scale: int = 6,
                  origin: tuple[int, int] = (0, 0), canvas_size: tuple[int, int] | None = None,
                  grid_lines: tuple[list[int], list[int]] | None = None,
                  row_models=None, column_models=None) -> list[CellCrop]:
    """Make second-pass crops containing immediate neighbors but centered on the target."""
    canvas_width, canvas_height = canvas_size or image.size
    x_lines, y_lines = grid_lines or (
        _pixel_lines(template.grid_x, canvas_width),
        _pixel_lines(template.grid_y, canvas_height),
    )
    contexts: list[CellCrop] = []
    for cell in candidates:
        left_index = max(0, cell.column - 1)
        right_index = min(len(x_lines) - 1, cell.column + 2)
        top_index = max(0, cell.row - 1)
        bottom_index = min(len(y_lines) - 1, cell.row + 2)
        origin_x, origin_y = origin
        corners = [point for r in (top_index, bottom_index-1)
                   for c in (left_index, right_index-1)
                   for point in cell_polygon(r,c,(x_lines,y_lines),row_models,column_models)]
        context = image.crop((
            max(0, round(min(x for x,y in corners))-origin_x),
            max(0, round(min(y for x,y in corners))-origin_y),
            min(image.width, round(max(x for x,y in corners))-origin_x),
            min(image.height, round(max(y for x,y in corners))-origin_y),
        ))
        contexts.append(CellCrop(cell.row, cell.column, cell.bounds,
                                 context.resize((context.width * scale, context.height * scale), Image.Resampling.LANCZOS)))
    return contexts


def accepted_assignments(classifications: Iterable[ClassifiedCell], template: GridTemplate) -> list[dict[str, str | int]]:
    """Map confirmed cell indexes to human-facing facts without filling any gaps."""
    records: list[dict[str, str | int]] = []
    for item in sorted(classifications, key=lambda value: (value.row, value.column)):
        for label in item.accepted_labels:
            records.append({
                "person": label.value,
                "row": item.row,
                "column": item.column,
                "time": template.row_map[item.row],
                "shift": template.column_map[item.column],
                "confidence": round(item.confidence, 3),
            })
    return records


def _markdown_column_headers(template: GridTemplate) -> dict[int, str]:
    """Give duplicate duty columns stable, human-readable Markdown headings."""
    seen: dict[str, int] = {}
    headers: dict[int, str] = {}
    for column in sorted(template.column_map):
        shift = template.column_map[column]
        seen[shift] = seen.get(shift, 0) + 1
        headers[column] = f"{shift} #{seen[shift]}"
    return headers


def _markdown_cell(item: ClassifiedCell | None) -> str:
    if item is None:
        return "?"
    if item.first_targets or item.second_targets:
        first = "+".join(label.value for label in item.first_targets) or "?"
        second = "+".join(label.value for label in item.second_targets) or "?"
        if item.first_targets == item.second_targets:
            return first
        return f"{first}→{second}"
    if CellLabel.EMPTY in item.first_pass:
        return "·"
    if CellLabel.OTHER in item.first_pass:
        return "O"
    return "?"


def render_markdown_grid(classifications: Iterable[ClassifiedCell], template: GridTemplate) -> str:
    """Render every classified cell as a reviewable Markdown duty table.

    ``丁``/``戊``/``己``/``庚`` (or ``丁+戊``) means both passes agreed. ``X→Y``
    exposes a disagreement instead of silently accepting it; ``O`` means
    non-target text and ``·`` means the first pass found the cell empty.
    """
    results = {(item.row, item.column): item for item in classifications}
    columns = sorted(template.column_map)
    headers = _markdown_column_headers(template)
    lines = [
        "| 時段 | " + " | ".join(headers[column] for column in columns) + " |",
        "| --- | " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in sorted(template.row_map):
        values = [_markdown_cell(results.get((row, column))) for column in columns]
        lines.append("| " + template.row_map[row] + " | " + " | ".join(values) + " |")
    lines.extend([
        "",
        "圖例：`丁／戊／己／庚` 或 `丁+戊`＝兩輪都看見的代號；`X→Y`＝複查不一致；`O`＝其他文字；`·`＝空白；`?`＝不明或缺少回覆。",
    ])
    return "\n".join(lines)


def run_phase1(source: Path, template: GridTemplate, *, model: str = "gpt-5.6-luna",
               client: OpenAI | None = None, batch_size: int = 48,
               include_markdown: bool = False, crops_dir: Path | None = None) -> dict:
    """Run both classification passes and return observed assignments only."""
    corrected, correction_method = correct_perspective(source, template)
    raw_projector = original_grid_projector(corrected, template) if template.use_original_image else None
    column_models = None
    grid_lines: tuple[list[int], list[int]] | None = None
    row_models: list[tuple[float, float]] | None = None
    if raw_projector is not None:
        canonical, correction_method = canonical_grid_registration(corrected, template)
        alignment_score = validate_grid_alignment(canonical, template)
        analysis_image, origin = raw_projector.analysis_window(), (0, 0)
        cells = raw_projector.mapped_cells()
    else:
        alignment_score = validate_grid_alignment(corrected, template)
        grid_lines = registered_grid_lines(corrected, template)
        row_models = registered_row_models(corrected, grid_lines[1])
        column_models = registered_column_models(corrected, grid_lines[0])
        analysis_image, origin = analysis_time_window(corrected, template, y_lines=grid_lines[1],
                                                      row_models=row_models)
        cells = crop_mapped_cells(analysis_image, template, origin=origin,
                                  canvas_size=corrected.size, grid_lines=grid_lines,
                                  row_models=row_models, column_models=column_models)
    artifacts = (export_crop_artifacts(crops_dir, corrected=corrected, analysis_image=analysis_image,
                                       cells=cells, template=template, grid_lines=grid_lines,
                                       row_models=row_models)
                 if crops_dir is not None else None)
    first = classify_cells(cells, model=model, client=client, batch_size=batch_size)
    candidates = [
        cell for cell in cells
        if set(first[(cell.row, cell.column)].labels) & target_labels()
    ]
    second_crops = (raw_projector.context_crops(candidates) if raw_projector is not None
                    else context_crops(analysis_image, template, candidates, origin=origin,
                                       canvas_size=corrected.size, grid_lines=grid_lines,
                                       row_models=row_models, column_models=column_models))
    second = classify_cells(second_crops, model=model, client=client,
                            batch_size=batch_size, confirmation=True)
    classified = [
        ClassifiedCell(
            row=cell.row,
            column=cell.column,
            first_pass=tuple(first[(cell.row, cell.column)].labels),
            second_pass=(tuple(second[(cell.row, cell.column)].labels)
                         if (cell.row, cell.column) in second else None),
            confidence=min(
                first[(cell.row, cell.column)].confidence,
                second[(cell.row, cell.column)].confidence,
            ) if (cell.row, cell.column) in second else first[(cell.row, cell.column)].confidence,
        )
        for cell in cells
    ]
    result = {
        "pipeline": "phase1_cell_classifier",
        "correction_method": correction_method,
        "grid_alignment_score": alignment_score,
        "analysis_origin": {"x": origin[0], "y": origin[1]},
        "cell_count": len(cells),
        "assignments": accepted_assignments(classified, template),
        "unconfirmed_candidates": [
            {"row": item.row, "column": item.column,
             "first_pass": [label.value for label in item.first_targets],
             "second_pass": [label.value for label in item.second_targets]}
            for item in classified
            if item.first_targets or item.second_targets
            if set(item.first_targets) != set(item.accepted_labels)
            or set(item.second_targets) != set(item.accepted_labels)
        ],
    }
    markdown_grid = render_markdown_grid(classified, template)
    if include_markdown:
        result["markdown_grid"] = markdown_grid
    if artifacts is not None:
        artifact_directory = Path(artifacts["directory"])
        markdown_path = artifact_directory / "recognition.md"
        json_path = artifact_directory / "recognition.json"
        markdown_path.write_text(markdown_grid + "\n", encoding="utf-8")
        artifacts["markdown_result"] = str(markdown_path)
        artifacts["json_result"] = str(json_path)
        result["artifacts"] = artifacts
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _cli() -> int:
    parser = argparse.ArgumentParser(description="第一階段：固定格班表字元分類器（不影響現有 Bot）")
    commands = parser.add_subparsers(dest="command", required=True)
    calibrate = commands.add_parser("calibrate", help="偵測校正後的格線，產生可人工標注 template")
    calibrate.add_argument("image", type=Path)
    calibrate.add_argument("--output", type=Path, required=True, help="必須是新檔案，避免覆寫設定")
    calibrate.add_argument("--seed-template", type=Path)
    analyze = commands.add_parser("analyze", help="以已標注 template 執行兩輪逐格辨識")
    analyze.add_argument("image", type=Path)
    analyze.add_argument("--template", type=Path, required=True)
    analyze.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"))
    analyze.add_argument("--batch-size", type=int, default=48)
    analyze.add_argument("--dry-run", action="store_true",
                         help="只校正與切格，不呼叫 OpenAI 或產生費用")
    analyze.add_argument("--markdown", action="store_true",
                         help="辨識後只輸出供人工核對的 Markdown 表格")
    analyze.add_argument("--crops-dir", type=Path,
                         help="將校正圖、時段裁切圖、每格 PNG 與 contact sheet 輸出到新的資料夾")
    args = parser.parse_args()
    if args.command == "calibrate":
        if args.output.exists():
            parser.error(f"拒絕覆寫既有 template：{args.output}")
        seed = GridTemplate.from_json(args.seed_template) if args.seed_template else None
        result, method = calibrated_template(args.image, seed)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.to_json(args.output)
        print(json.dumps({"template": str(args.output), "correction_method": method,
                          "grid_x_count": len(result.grid_x), "grid_y_count": len(result.grid_y)}, ensure_ascii=False))
        return 0
    template = GridTemplate.from_json(args.template)
    if args.dry_run:
        corrected, correction_method = correct_perspective(args.image, template)
        raw_projector = original_grid_projector(corrected, template) if template.use_original_image else None
        grid_lines: tuple[list[int], list[int]] | None = None
        row_models: list[tuple[float, float]] | None = None
        if raw_projector is not None:
            analysis_image, origin = raw_projector.analysis_window(), (0, 0)
            cells = raw_projector.mapped_cells()
        else:
            grid_lines = registered_grid_lines(corrected, template)
            row_models = registered_row_models(corrected, grid_lines[1])
            column_models = registered_column_models(corrected, grid_lines[0])
            analysis_image, origin = analysis_time_window(corrected, template, y_lines=grid_lines[1],
                                                          row_models=row_models)
            cells = crop_mapped_cells(analysis_image, template, origin=origin,
                                      canvas_size=corrected.size, grid_lines=grid_lines,
                                      row_models=row_models, column_models=column_models)
        artifacts = (export_crop_artifacts(args.crops_dir, corrected=corrected, analysis_image=analysis_image,
                                           cells=cells, template=template, grid_lines=grid_lines,
                                           row_models=row_models)
                     if args.crops_dir is not None else None)
        print(json.dumps({
            "pipeline": "phase1_cell_classifier",
            "dry_run": True,
            "correction_method": correction_method,
            "analysis_image_size": {"width": analysis_image.width, "height": analysis_image.height},
            "cell_count": len(cells),
            "rows": template.row_map,
            "columns": template.column_map,
            "artifacts": artifacts,
        }, ensure_ascii=False, indent=2))
        return 0
    result = run_phase1(args.image, template, model=args.model, batch_size=args.batch_size,
                        include_markdown=args.markdown, crops_dir=args.crops_dir)
    if args.markdown:
        print(result["markdown_grid"])
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
