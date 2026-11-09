from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

from .schema import CHARSET, CellAnnotation, load_annotations

FULL_TIME_ROWS = {row: f"{hour:02d}-{hour + 1:02d}" for row, hour in enumerate(range(5, 24), 4)}
# C02..C36 are the physical duty columns of the timetable body. (Earlier V2
# excluded C28 as a "double-rule gap": the V1 calibration had split one bold
# rule into two lines. On real photos C28 is the ordinary column right after
# that bold rule and carries codes, so it is included.)
# C33..C36 (A1A3站, 內機, 內部管理, 全區帶勤) are printed on every roster, right of
# the A1 columns; V2 originally stopped at C32 and so never read them.
FIRST_DUTY_COLUMN, LAST_DUTY_COLUMN = 2, 36
FULL_DUTY_COLUMNS = tuple(range(FIRST_DUTY_COLUMN, LAST_DUTY_COLUMN + 1))
# What a saved run's cells were read with: 1 = columns c02-c32, call-out text read as duties (rosters saved
# before 2026-10-04); 2 = c02-c36, text boxes and call-outs laid over the table are ignored (overlays.py).
# ``DemoApp.ensure_grid`` reads an older run again from its saved photo.
GRID_VERSION = 2


def full_grid_template(source: Path, destination: Path) -> dict:
    """Derive a V2-only full-table template without changing the V1 template."""
    payload = __import__("json").loads(source.read_text(encoding="utf-8"))
    payload["row_map"] = {str(row): slot for row, slot in FULL_TIME_ROWS.items()}
    payload["column_map"] = {str(column): f"欄位-c{column:02d}"
                             for column in FULL_DUTY_COLUMNS}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2)+"\n",
                           encoding="utf-8")
    return {"rows": len(FULL_TIME_ROWS), "columns": len(FULL_DUTY_COLUMNS),
            "cells_per_photo": len(FULL_TIME_ROWS)*len(FULL_DUTY_COLUMNS),
            "time_range": [next(iter(FULL_TIME_ROWS.values())), list(FULL_TIME_ROWS.values())[-1]],
            "excluded_grid_intervals": [], "output": str(destination)}


def prepare_cells(schedules: Path, dataset: Path, template: Path, *,
                  only_sources: list[Path] | None = None) -> dict:
    """Reuse the production-proven registration, but write to an isolated dataset."""
    import roster_resnet
    report = roster_resnet.ingest_schedules(schedules, dataset, template,
                                             only_sources=only_sources)
    # Fitting sloped rules across the wider V2 table can occasionally make a
    # far-right row/column polygon cross even though perspective registration
    # itself is valid. Retry only that explicit geometry failure with straight
    # registered lines. This changes neither the V1 functions nor its dataset.
    retry_paths = [Path(item["file"]) for item in
                   (*report.get("failures", []), *report.get("reused_existing", []))
                   if "勤務格尺寸異常" in item.get("error", item.get("reason", ""))]
    if retry_paths:
        original_rows = roster_resnet.registered_row_models
        original_columns = roster_resnet.registered_column_models
        roster_resnet.registered_row_models = lambda image, lines: [(float(y), 0.0) for y in lines]
        roster_resnet.registered_column_models = lambda image, lines: [(float(x), 0.0) for x in lines]
        try:
            retry = roster_resnet.ingest_schedules(schedules, dataset, template,
                                                    only_sources=retry_paths)
        finally:
            roster_resnet.registered_row_models = original_rows
            roster_resnet.registered_column_models = original_columns
        report["v2_straight_grid_retries"] = retry
    manifest = dataset / "labels.csv"
    prior = {item.sample_id: item for item in load_annotations(dataset / "annotations.jsonl")}
    items = []
    with manifest.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            path = dataset / row["image"]
            with Image.open(path) as image:
                width, height = image.size
            items.append(prior.get(row["sample_id"], CellAnnotation(
                sample_id=row["sample_id"], image=row["image"],
                source_group=row["source_group"], row=int(row["row"]),
                column=int(row["column"]), width=width, height=height,
                reviewed=False,
            )))
    from .schema import save_annotations
    save_annotations(dataset / "annotations.jsonl", items)
    return {**report, "annotation_rows": len(items)}


def image_tensor(image: Image.Image, size: tuple[int, int], augment: bool = False):
    import torch
    image = image.convert("L")
    if augment:
        import random
        image = ImageEnhance.Brightness(image).enhance(random.uniform(.8, 1.2))
        image = ImageEnhance.Contrast(image).enhance(random.uniform(.8, 1.25))
        if random.random() < .25:
            image = image.filter(ImageFilter.GaussianBlur(random.uniform(.1, .7)))
    image = image.resize(size, Image.Resampling.LANCZOS)
    return torch.from_numpy((1.0 - np.asarray(image, dtype=np.float32) / 255.0)[None])


class DetectionDataset:
    def __init__(self, root: Path, split: str, size=(256, 128), augment=False):
        self.root, self.size, self.augment = root, size, augment
        self.items = [item for item in load_annotations(root / "annotations.jsonl", reviewed_only=True)
                      if item.split == split]

    def __len__(self): return len(self.items)

    def __getitem__(self, index):
        import torch
        item = self.items[index]
        with Image.open(self.root / item.image) as image:
            tensor = image_tensor(image, self.size, self.augment)
        mask = torch.zeros((1, self.size[1], self.size[0]), dtype=torch.float32)
        sx, sy = self.size[0] / item.width, self.size[1] / item.height
        for ann in item.annotations:
            if ann.role != "personnel_code": continue
            box = ann.box
            x1, x2 = round(box.x1*sx), round(box.x2*sx)
            y1, y2 = round(box.y1*sy), round(box.y2*sy)
            mask[:, y1:y2, x1:x2] = 1
        return tensor, mask


class RecognitionDataset:
    def __init__(self, root: Path, split: str, size=(192, 48), augment=False):
        self.root, self.size, self.augment = root, size, augment
        self.items = [(item, ann) for item in load_annotations(root / "annotations.jsonl", reviewed_only=True)
                      if item.split == split for ann in item.annotations
                      if ann.role == "personnel_code"]
        self.index = {char: number + 1 for number, char in enumerate(CHARSET)}

    def __len__(self): return len(self.items)

    def __getitem__(self, index):
        import torch
        item, ann = self.items[index]
        with Image.open(self.root / item.image) as image:
            crop = image.crop((ann.box.x1, ann.box.y1, ann.box.x2, ann.box.y2))
            tensor = image_tensor(crop, self.size, self.augment)
        target = torch.tensor([self.index[char] for char in ann.text], dtype=torch.long)
        return tensor, target


def recognition_collate(batch):
    import torch
    images, targets = zip(*batch)
    lengths = torch.tensor([len(value) for value in targets], dtype=torch.long)
    return torch.stack(images), torch.cat(targets), lengths
