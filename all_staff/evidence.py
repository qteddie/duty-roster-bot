"""Per-person evidence photo: the corrected roster with that person's cells outlined in blue.

Same idea as the V1 four-person evidence image: keep the perspective-corrected
roster at full width, outline the cells the person was recognised in, and crop
away the rows below their last duty. The cell outlines follow the tracked grid
(``geometry/<group>.json`` written by ``grid.ingest_tracked``), so they stay on
the printed rules even when the photo is slightly bent.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageOps

BLUE = (37, 99, 235)
DIVIDER = (203, 213, 225)   # between the panels of a wrapped work crop
PHONE = (9, 16)             # width : height of an upright phone screen
ORIGINAL_EDGE, PREVIEW_EDGE = 2400, 1000


def geometry_payload(registration, rows, columns) -> dict:
    """Cell, work-header and time-column quads in corrected-image pixels."""
    rows, columns = tuple(rows), tuple(columns)
    cells = {f"{row},{column}": [[round(float(x), 1), round(float(y), 1)] for x, y in registration.quad(row, column)]
             for row in rows for column in columns}
    quad = lambda left, top, right, bottom: [list(registration.corner(c, r)) for c, r in
                                           ((left, top), (right, top), (right, bottom), (left, bottom))]
    return {"size": list(registration.image.size), "cells": cells,
            "headers": {str(c): quad(c, 2, c + 1, 4) for c in columns},
            "time_header": quad(0, 2, 2, 4),
            "times": {str(r): quad(0, r, 2, r + 1) for r in rows}}


def save_geometry(registration, rows: range | tuple, columns, destination: Path) -> None:
    """Store the cell quads (corrected-image pixels) for evidence rendering."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(geometry_payload(registration, rows, columns)), encoding="utf-8")


def render_slot(corrected: Image.Image, geometry: dict, row: int, columns) -> Image.Image | None:
    """Original work headers + exactly one hour; straighten each tracked column separately.

    The time column is copied to the left of either area. No intervening hours,
    neighbouring area, A1 columns or name table are included.
    """
    import numpy as np
    columns = tuple(columns)
    if not columns or any(f"{row},{c}" not in geometry.get("cells", {}) or
                          str(c) not in geometry.get("headers", {}) for c in columns):
        return None
    if str(row) not in geometry.get("times", {}) or not geometry.get("time_header"):
        return None
    headers = [geometry["time_header"], *(geometry["headers"][str(c)] for c in columns)]
    quads = [geometry["times"][str(row)], *(geometry["cells"][f"{row},{c}"] for c in columns)]
    widths = [max(1, round(_size(q)[0])) for q in quads]
    header_height = max(1, round(max(_size(q)[1] for q in headers)))
    row_height = max(1, round(max(_size(q)[1] for q in quads)))
    source = np.asarray(corrected.convert("RGB"))
    header = _straighten(source, headers, widths, header_height)
    hour = _straighten(source, quads, widths, row_height)
    padding, gap = 4, 8
    image = Image.new("RGB", (sum(widths) + padding * 2, header_height + row_height + gap + padding * 2), "white")
    image.paste(header, (padding, padding))
    y = padding + header_height + gap
    image.paste(hour, (padding, y))
    ImageDraw.Draw(image).rectangle((padding - 1, y - 1, image.width - padding, y + row_height),
                                    outline=BLUE, width=2)
    return image


def render_work(corrected: Image.Image, geometry: dict, columns, rows) -> Image.Image | None:
    """Original work headers + every listed hour of one work, with the time column on the left.

    As in ``render_slot`` each column is straightened separately, here one hour strip at a time; the
    strips are stacked under the headers and the work's cells are outlined in blue. Most works are one
    column wide, and a whole day of one column is a sliver LINE shows too small to read: the hours then
    wrap into two or three side-by-side panels, each with its own headers, whichever shape is closest
    to an upright phone (``PHONE``), where a tapped image opens.
    """
    import numpy as np
    columns, rows = tuple(columns), tuple(rows)
    cells, headers, times = (geometry.get(key) or {} for key in ("cells", "headers", "times"))
    if not columns or not rows or not geometry.get("time_header") or \
            any(str(c) not in headers for c in columns) or any(str(r) not in times for r in rows) or \
            any(f"{r},{c}" not in cells for r in rows for c in columns):
        return None
    header_quads = [geometry["time_header"], *(headers[str(c)] for c in columns)]
    hour_quads = [[times[str(r)], *(cells[f"{r},{c}"] for c in columns)] for r in rows]
    widths = [max(1, round(max(_size(quads[i])[0] for quads in hour_quads))) for i in range(len(header_quads))]
    header_height = max(1, round(max(_size(q)[1] for q in header_quads)))
    source = np.asarray(corrected.convert("RGB"))
    header = _straighten(source, header_quads, widths, header_height)
    hours = [_straighten(source, quads, widths, max(1, round(max(_size(q)[1] for q in quads))))
             for quads in hour_quads]
    padding, gap, divider, panel_width = 4, 8, 24, sum(widths)

    def layout(count):
        per_panel = -(-len(hours) // count)
        panels = [hours[start:start + per_panel] for start in range(0, len(hours), per_panel)]
        return panels, (padding * 2 + panel_width * len(panels) + divider * (len(panels) - 1),
                        padding * 2 + header_height + gap + max(sum(h.height for h in p) for p in panels))
    panels, size = min((layout(count) for count in range(1, min(3, len(hours)) + 1)),
                       key=lambda option: abs(math.log(option[1][1] / option[1][0] * PHONE[0] / PHONE[1])))
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for index, panel in enumerate(panels):
        left = padding + index * (panel_width + divider)
        if index:
            draw.line((left - divider // 2, padding, left - divider // 2, size[1] - padding), fill=DIVIDER, width=2)
        image.paste(header, (left, padding))
        top = bottom = padding + header_height + gap
        for hour in panel:
            image.paste(hour, (left, bottom))
            bottom += hour.height
        draw.rectangle((left + widths[0] - 1, top - 1, left + panel_width, bottom), outline=BLUE, width=2)
    return image


def _size(quad) -> tuple[float, float]:
    """Mean width and height of a [tl, tr, br, bl] quad."""
    import numpy as np
    tl, tr, br, bl = np.asarray(quad, dtype=float)
    return ((np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2,
            (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2)


def _straighten(source, quads, widths, height) -> Image.Image:
    """Warp each quad to an upright ``width × height`` tile and set the tiles side by side."""
    import cv2
    import numpy as np
    tiles = []
    for quad, width in zip(quads, widths):
        target = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
        matrix = cv2.getPerspectiveTransform(np.float32(quad), target)
        tiles.append(cv2.warpPerspective(source, matrix, (width, height),
                                        flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                                        borderValue=(255, 255, 255)))
    return Image.fromarray(np.concatenate(tiles, axis=1))


def render_staff_table(photo: Path, template) -> Image.Image | None:
    """Crop all seven original name/code pairs, including the printed header.

    Locate using verified printed codes; use the source photo at higher resolution
    instead of the low-resolution duty canvas. No OCR names are painted into it.
    """
    from .name_table import locate, warp_page
    page = warp_page(photo, template, color=True)
    if page is None:
        return None
    _, report = locate(page.convert("L"))
    boxes = [r["table_bounds"] for r in report if r.get("table_bounds")]
    if not boxes:
        return None
    margin = max(2, round(page.width * .003))
    left = max(0, min(b[0] for b in boxes) - round(page.width * .025))  # vertical table title
    top = max(0, min(b[1] for b in boxes) - margin)
    right = min(page.width, max(b[2] for b in boxes) + margin)
    bottom = min(page.height, max(b[3] for b in boxes) + margin)
    return page.crop((left, top, right, bottom))


def render_person(corrected: Image.Image, geometry: dict, cells: list[tuple[int, int]], *,
                  pad_rows: int = 1) -> Image.Image | None:
    """Outline ``cells`` [(row, column)] on the corrected roster; crop below the last one."""
    known = [(row, column) for row, column in cells if f"{row},{column}" in geometry["cells"]]
    if not known:
        return None
    quads = [geometry["cells"][f"{row},{column}"] for row, column in known]
    image = corrected.convert("RGB").copy()
    width, height = image.size
    line = max(3, round(min(width, height) * .004))
    draw = ImageDraw.Draw(image)
    for quad in quads:
        points = [tuple(p) for p in quad]
        draw.line([*points, points[0]], fill=BLUE, width=line, joint="curve")
    # crop: full width, from the top down to `pad_rows` rows below the lowest outlined cell
    last_row = max(row for row, _ in known)
    bottom = max(p[1] for q in quads for p in q)
    below = [geometry["cells"].get(f"{last_row + k},{c}") for k in range(1, pad_rows + 1) for _, c in known]
    below = [q for q in below if q]
    if below:
        bottom = max(bottom, max(p[1] for q in below for p in q))
    bottom = min(height, int(round(bottom + line * 4)))
    return image.crop((0, 0, width, bottom))


def _fit(image: Image.Image, edge: int) -> Image.Image:
    scale = min(1.0, edge / max(image.size))
    if scale >= 1:
        return image
    return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)


def write_person_images(image: Image.Image, original: Path, preview: Path) -> None:
    original.parent.mkdir(parents=True, exist_ok=True)
    for path, edge, quality in ((preview, PREVIEW_EDGE, 72), (original, ORIGINAL_EDGE, 85)):
        temporary = path.with_name(path.stem + f".{uuid4().hex}.tmp.jpg")   # atomic for concurrent queries
        _fit(image, edge).save(temporary, "JPEG", quality=quality, optimize=True)
        temporary.replace(path)


def write_roster_images(photo: Path, original: Path, preview: Path) -> None:
    """Keep the complete uploaded page legible, within LINE's image-message size limits."""
    original.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(photo) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    for path, limit, variants in (
        (original, 10_000_000, ((5000, 90), (5000, 80), (4000, 80), (3000, 75), (2400, 70))),
        (preview, 1_000_000, ((1000, 72), (900, 65), (700, 60), (500, 55))),
    ):
        temporary = path.with_name(path.stem + f".{uuid4().hex}.tmp.jpg")
        try:
            for edge, quality in variants:
                _fit(image, edge).save(temporary, "JPEG", quality=quality, optimize=True)
                if temporary.stat().st_size <= limit:
                    temporary.replace(path)
                    break
            else:
                raise ValueError(f"班表照片無法壓到 {limit} bytes 以下")
        finally:
            temporary.unlink(missing_ok=True)
