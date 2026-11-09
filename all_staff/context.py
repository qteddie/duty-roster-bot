"""Context crops: each cell image also contains a margin of its neighbours.

Printed codes often overflow the ruled cell (``67`` whose ``7`` sits on the
right rule). A crop limited to the rule truncates the glyph, so V2 crops a
margin around every cell and remembers where the *core* cell lies. Ownership is
decided later by the segmenter: a text cluster belongs to the cell whose core
contains its centre.

The V1 crop function is patched only for the duration of ``prepare``; V1 code
and datasets are untouched.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image

CONTEXT_FILE = "context.json"
DEFAULT_CONTEXT = {"fx": 0.22, "fy": 0.12}


def load_context(dataset: Path) -> dict:
    """Context fractions for a dataset; legacy datasets have no margin."""
    path = Path(dataset) / CONTEXT_FILE
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"fx": 0.0, "fy": 0.0}


def core_box(width: int, height: int, context: dict) -> tuple[int, int, int, int]:
    """Core cell rectangle inside a context crop of ``width``×``height``."""
    fx, fy = float(context.get("fx", 0)), float(context.get("fy", 0))
    mx = round(width * fx / (1 + 2 * fx)); my = round(height * fy / (1 + 2 * fy))
    return mx, my, width - mx, height - my


def _crop_with_fill(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    left, top, right, bottom = box
    inside = (max(0, left), max(0, top), min(image.width, right), min(image.height, bottom))
    piece = image.crop(inside)
    if inside == box:
        return piece
    fill = tuple(int(v) for v in np.median(np.asarray(piece.convert("RGB")).reshape(-1, 3), 0))
    canvas = Image.new("RGB", (right - left, bottom - top), fill)
    canvas.paste(piece.convert("RGB"), (inside[0] - left, inside[1] - top))
    return canvas.convert(image.mode)


@contextmanager
def context_crops(fx: float, fy: float):
    """Temporarily make ``roster_resnet`` emit context crops."""
    import roster_resnet
    original = roster_resnet.crop_mapped_cells

    def patched(image, template, *, scale=5, origin=(0, 0), **kwargs):
        cells = original(image, template, scale=scale, origin=origin, **kwargs)
        output = []
        for cell in cells:
            left, top, right, bottom = cell.bounds
            mx, my = round((right - left) * fx), round((bottom - top) * fy)
            ox, oy = origin
            crop = _crop_with_fill(image, (left - ox - mx, top - oy - my,
                                           right - ox + mx, bottom - oy + my))
            output.append(replace(cell, image=crop.resize((crop.width * scale, crop.height * scale),
                                                          Image.Resampling.LANCZOS)))
        return output

    roster_resnet.crop_mapped_cells = patched
    try:
        yield
    finally:
        roster_resnet.crop_mapped_cells = original


def prepare_context(schedules: Path, dataset: Path, template: Path, *,
                    fx=DEFAULT_CONTEXT["fx"], fy=DEFAULT_CONTEXT["fy"],
                    only_sources: list[Path] | None = None, method: str = "tracked") -> dict:
    """Build context crops. ``method='tracked'`` (default) uses the V2 grid tracker
    with per-cell perspective warps; ``'v1'`` reuses the V1 registration."""
    photos = list(only_sources) if only_sources is not None else [
        path for path in Path(schedules).rglob("*")
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    if not photos:
        raise FileNotFoundError(f"{schedules} 沒有班表照片；請用 --schedules 指定照片資料夾")
    dataset.mkdir(parents=True, exist_ok=True)
    (dataset / CONTEXT_FILE).write_text(json.dumps({"fx": fx, "fy": fy, "method": method}) + "\n",
                                        encoding="utf-8")
    if method == "tracked":
        from .grid import ingest_tracked
        report = ingest_tracked(schedules, dataset, template, fx=fx, fy=fy, only_sources=only_sources)
        return {**report, "annotation_rows": _manifest_to_annotations(dataset)}
    from .dataset import prepare_cells
    with context_crops(fx, fy):
        return prepare_cells(schedules, dataset, template, only_sources=only_sources)


def _manifest_to_annotations(dataset: Path) -> int:
    """Create/refresh annotations.jsonl from labels.csv, keeping existing labels."""
    import csv
    from .schema import CellAnnotation, load_annotations, save_annotations
    prior = {item.sample_id: item for item in load_annotations(dataset / "annotations.jsonl")}
    items = []
    with (dataset / "labels.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            with Image.open(dataset / row["image"]) as image:
                width, height = image.size
            old = prior.get(row["sample_id"])
            if old is not None and (old.width, old.height) == (width, height):
                items.append(old); continue
            items.append(CellAnnotation(sample_id=row["sample_id"], image=row["image"],
                                        source_group=row["source_group"], row=int(row["row"]),
                                        column=int(row["column"]), width=width, height=height,
                                        reviewed=False))
    save_annotations(dataset / "annotations.jsonl", items)
    return len(items)


def _offset(old: np.ndarray, new: np.ndarray) -> tuple[int, int, float]:
    import cv2
    if old.shape[0] > new.shape[0] or old.shape[1] > new.shape[1]:
        return 0, 0, 0.0
    result = cv2.matchTemplate(new, old, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(result)
    return x, y, float(score)


def migrate_annotations(source: Path, destination: Path, *, min_score=0.8, stay_score=0.55,
                        keep_proposals=False, search=1, overwrite=True) -> dict:
    """Carry human/reviewed annotations from a plain dataset into a context dataset.

    Each old crop is located by template matching inside the new context crop of
    the same cell *and* its neighbours (``search`` cells in every direction). An
    old registration that was off by a row or column (9/12, 9/22 v2) therefore
    moves its labels to the cell that really shows them. A label is kept only if
    every box lands inside the core of the chosen cell; reviewed-empty cells are
    kept only when they match their own position. Anything else is left for
    re-review and listed in the report.
    """
    from .schema import Annotation, Box, load_annotations, save_annotations
    context = load_context(destination)
    source_context = load_context(source)
    old_items = load_annotations(source / "annotations.jsonl")
    new_items = load_annotations(destination / "annotations.jsonl")
    index = {(i.source_group, i.row, i.column): n for n, i in enumerate(new_items)}
    report = {"migrated": 0, "reviewed": 0, "boxes": 0, "moved": [], "weak_matches": [],
              "conflicts": 0, "missing": 0, "missing_images": 0}
    chosen: dict[int, tuple[float, object]] = {}
    for old in old_items:
        annotations = [a for a in old.annotations if keep_proposals or a.source == "human" or old.reviewed]
        if not old.reviewed and not annotations:
            continue
        if (old.source_group, old.row, old.column) not in index:
            report["missing"] += 1; continue
        if not (source / old.image).is_file():
            report["missing_images"] += 1; continue
        with Image.open(source / old.image) as opened:
            template = np.asarray(opened.convert("L"))
        ox = oy = 0
        if source_context.get("fx") or source_context.get("fy"):
            # a context crop is as large as the new one: match its core only
            x1, y1, x2, y2 = core_box(template.shape[1], template.shape[0], source_context)
            template = template[y1:y2, x1:x2]; ox, oy = x1, y1
        candidates = []
        for dr in range(-search, search + 1):
            for dc in range(-search, search + 1):
                if annotations == [] and (dr or dc):
                    continue  # an empty cell carries no evidence of where it belongs
                n = index.get((old.source_group, old.row + dr, old.column + dc))
                if n is None:
                    continue
                item = new_items[n]
                with Image.open(destination / item.image) as opened:
                    dx, dy, score = _offset(template, np.asarray(opened.convert("L")))
                dx, dy = dx - ox, dy - oy  # shift for boxes in the old image's coordinates
                x1, y1, x2, y2 = core_box(item.width, item.height, context)
                if annotations:
                    inside = all(x1 <= (a.box.x1 + a.box.x2) / 2 + dx <= x2 and
                                 y1 <= (a.box.y1 + a.box.y2) / 2 + dy <= y2 for a in annotations)
                else:  # the old crop's centre must sit in this core
                    cx, cy = dx + ox + template.shape[1] / 2, dy + oy + template.shape[0] / 2
                    inside = x1 <= cx <= x2 and y1 <= cy <= y2
                if inside:
                    candidates.append((score, n, dx, dy, dr, dc))
        # stay in place unless a neighbour is clearly the better match: a slanted
        # sheet lowers every score a little (axis-aligned vs warped crop)
        home = next((c for c in candidates if c[4] == 0 and c[5] == 0 and c[0] >= stay_score), None)
        moved_best = max((c for c in candidates if (c[4] or c[5]) and c[0] >= min_score), default=None)
        best = home
        if moved_best is not None and (home is None or moved_best[0] >= home[0] + .1):
            best = moved_best
        if best is None:
            report["weak_matches"].append(old.sample_id); continue
        score, n, dx, dy, dr, dc = best
        item = new_items[n]
        moved = tuple(Annotation(Box(max(0, min(item.width - 1, a.box.x1 + dx)),
                                     max(0, min(item.height - 1, a.box.y1 + dy)),
                                     max(1, min(item.width, a.box.x2 + dx)),
                                     max(1, min(item.height, a.box.y2 + dy))),
                                 a.text, a.role, a.source, a.confidence) for a in annotations)
        if not overwrite and (item.reviewed or any(a.source == "human" for a in item.annotations)):
            continue  # already carries labels from a newer source
        if n in chosen:
            report["conflicts"] += 1
            if chosen[n][0] >= score:
                continue
        chosen[n] = (score, replace(item, reviewed=old.reviewed, annotations=moved))
        if dr or dc:
            report["moved"].append(f"{old.sample_id} → r{old.row + dr:02d}_c{old.column + dc:02d}")
    output = list(new_items)
    for n, (_, item) in chosen.items():
        output[n] = item
        report["migrated"] += 1; report["reviewed"] += item.reviewed; report["boxes"] += len(item.annotations)
    save_annotations(destination / "annotations.jsonl", output)
    report["weak_match_count"] = len(report["weak_matches"])
    report["moved_count"] = len(report["moved"])
    return report
