"""Classical text-line segmentation for printed roster cells.

The roster is typeset, so ink is dark on a light, smooth background and the
only other dark structures are the ruled lines. The segmenter

1. normalises the background and thresholds ink,
2. removes long horizontal/vertical rules with morphological opening,
3. groups the remaining glyph components into text lines, then into
   horizontally-adjacent clusters, and
4. keeps clusters whose centre lies inside the core cell (context crops carry
   neighbour margins; overflowing glyphs stay with the cell that owns them).

Each line is later read by the CTC recogniser as a string such as ``72``,
``IB`` or ``甲戊`` and split into personnel codes by :func:`parse_codes`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .schema import ALLOWED_CODE_SET, CHINESE_CODES, Box

TOKEN = re.compile(r"\d+|[A-N]|[" + "".join(CHINESE_CODES) + "]")


@dataclass(frozen=True)
class Line:
    box: Box
    components: tuple[Box, ...]  # glyph components, left to right
    original_text: str = ""  # retained when the numeric recheck changes a CTC reading


def digit_groups(image: Image.Image, line: Line) -> list[Box]:
    """Group overlapping strokes; split a joined wide digit pair at its ink valley.

    This provides candidate crops only. The recogniser must read them before
    they can change any personnel code.
    """
    groups = []
    for box in sorted(line.components, key=lambda b: b.x1):
        if groups and box.x1 < max(b.x2 for b in groups[-1]):
            groups[-1].append(box)
        else:
            groups.append([box])
    output = []
    for group in groups:
        box = _union(group)
        w, h = box.x2 - box.x1, box.y2 - box.y1
        if w / max(1, h) <= .85:
            output.append(box)
            continue
        ink = _ink(np.asarray(image.convert('L').crop((box.x1, box.y1, box.x2, box.y2))))
        profile = ink.sum(axis=0)
        lo, hi = round(w * .3), round(w * .7)
        if hi <= lo:
            output.append(box)
            continue
        cut = lo + int(profile[lo:hi].argmin())
        if profile[cut] > h * .25:  # a solid glyph is not two digits
            output.append(box)
            continue
        output.extend([Box(box.x1, box.y1, box.x1 + cut, box.y2),
                       Box(box.x1 + cut, box.y1, box.x2, box.y2)])
    return output


def _ink(gray: np.ndarray) -> np.ndarray:
    import cv2
    k = max(15, (min(gray.shape) // 4) | 1)
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    background = cv2.GaussianBlur(background, (0, 0), k / 4)
    ratio = gray.astype(np.float32) / np.maximum(background.astype(np.float32), 1)
    return (ratio < 0.72).astype(np.uint8)


def _remove_rules(ink: np.ndarray, core: tuple[int, int, int, int]) -> np.ndarray:
    import cv2
    cw, ch = core[2] - core[0], core[3] - core[1]
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(25, int(cw * .55)), 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(25, int(ch * .45)))))
    rules = cv2.dilate(horizontal | vertical, np.ones((3, 3), np.uint8))
    return ink & (1 - rules)


def _components(ink: np.ndarray, core) -> list[Box]:
    import cv2
    cw, ch = core[2] - core[0], core[3] - core[1]
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    min_area = max(12, int(cw * ch * 0.0012))
    boxes = []
    for x, y, w, h, area in stats[1:count]:
        if area < min_area or h > ch * 0.62 or w > cw * 0.95:
            continue
        if (w <= 3 and h > 6 * w) or (h <= 3 and w > 6 * h):  # rule remnants
            continue
        boxes.append(Box(int(x), int(y), int(x + w), int(y + h)))
    return boxes


def _merge_rows(boxes: list[Box]) -> list[list[Box]]:
    rows: list[list[Box]] = []
    for box in sorted(boxes, key=lambda b: (b.y1 + b.y2) / 2):
        for row in rows:
            top, bottom = min(b.y1 for b in row), max(b.y2 for b in row)
            overlap = min(bottom, box.y2) - max(top, box.y1)
            if overlap > 0.45 * min(bottom - top, box.y2 - box.y1):
                row.append(box); break
        else:
            rows.append([box])
    return rows


def _clusters(row: list[Box], gap: float) -> list[list[Box]]:
    row = sorted(row, key=lambda b: b.x1)
    output = [[row[0]]]
    for box in row[1:]:
        if box.x1 - max(b.x2 for b in output[-1]) > gap:
            output.append([box])
        else:
            output[-1].append(box)
    return output


def _union(boxes) -> Box:
    return Box(min(b.x1 for b in boxes), min(b.y1 for b in boxes),
               max(b.x2 for b in boxes), max(b.y2 for b in boxes))


def segment_lines(image: Image.Image, core: tuple[int, int, int, int] | None = None,
                  *, pad=0.14) -> list[Line]:
    """Return text lines owned by the core cell, top to bottom."""
    gray = np.asarray(image.convert("L"))
    core = core or (0, 0, gray.shape[1], gray.shape[0])
    ink = _remove_rules(_ink(gray), core)
    boxes = _components(ink, core)
    ch = core[3] - core[1]
    lines = []
    for row in _merge_rows(boxes):
        height = max(b.y2 for b in row) - min(b.y1 for b in row)
        if height < ch * 0.15:  # dots, dashes, speckles and rule stubs
            continue
        # drop punctuation-sized bits (``.``) that sit beside a glyph
        row = [b for b in row if (b.y2 - b.y1) > height * .35 or (b.x2 - b.x1) > height * .35]
        kept = []
        for cluster in _clusters(row, gap=max(6, height * 0.55)):
            box = _union(cluster)
            cx, cy = (box.x1 + box.x2) / 2, (box.y1 + box.y2) / 2
            if core[0] <= cx < core[2] and core[1] <= cy < core[3]:
                kept.extend(cluster)
        if not kept:
            continue
        box = _union(kept); px = py = (box.y2 - box.y1) * pad
        min_width = (box.y2 - box.y1) * .75  # thin glyphs such as 1 and I
        if box.x2 - box.x1 + 2 * px < min_width:
            px = (min_width - (box.x2 - box.x1)) / 2
        padded = Box(max(0, int(box.x1 - px)), max(0, int(box.y1 - py)),
                     min(gray.shape[1], int(box.x2 + px)), min(gray.shape[0], int(box.y2 + py)))
        lines.append(Line(padded, tuple(sorted(kept, key=lambda b: b.x1))))
    return sorted(lines, key=lambda line: line.box.y1)


def parse_codes(text: str) -> list[str]:
    """Split a recognised line into personnel codes (``IB`` → I, B; ``#72`` → 72)."""
    return TOKEN.findall(text)


def split_line(line: Line, codes: list[str]) -> list[Box]:
    """One box per code: cut the line at its widest component gaps."""
    if len(codes) <= 1:
        return [line.box] * len(codes)
    if len(line.components) < len(codes):
        return _even_split(line.box, codes)
    parts = sorted(line.components, key=lambda b: b.x1)
    merged = [[parts[0]]]
    for box in parts[1:]:  # join overlapping strokes of one glyph (e.g. 戊)
        if box.x1 < max(b.x2 for b in merged[-1]) - 1:
            merged[-1].append(box)
        else:
            merged.append([box])
    if len(merged) < len(codes):
        return _even_split(line.box, codes)
    gaps = sorted(range(1, len(merged)), key=lambda i: merged[i][0].x1 - max(
        b.x2 for b in merged[i - 1]), reverse=True)[:len(codes) - 1]
    output, start = [], 0
    for cut in sorted(gaps) + [len(merged)]:
        group = [b for chunk in merged[start:cut] for b in chunk]
        u = _union(group)
        output.append(Box(max(line.box.x1, u.x1 - 3), line.box.y1,
                          min(line.box.x2, u.x2 + 3), line.box.y2))
        start = cut
    return output


def _even_split(box: Box, codes: list[str]) -> list[Box]:
    """Touching glyphs (e.g. kerned ``KI``): divide the line by character count."""
    total = sum(len(code) for code in codes); output, x = [], box.x1
    for code in codes:
        width = (box.x2 - box.x1) * len(code) / total
        output.append(Box(int(round(x)), box.y1, int(round(x + width)), box.y2)); x += width
    return output


def valid_code(code: str) -> bool:
    return code in ALLOWED_CODE_SET
