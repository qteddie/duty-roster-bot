"""Static inference viewer for the V2 line model.

``python -m all_staff.cli viewer`` runs the line OCR over whole sheets and writes

    reports/all_staff_viewer/index.html   (static page, no server needed)
    reports/all_staff_viewer/data.js      (predictions, human labels, metrics)

Cell images are referenced from the dataset by relative path instead of being
copied, so the report stays small. Open ``index.html`` directly, or serve the
repository root with ``python -m http.server``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image

from .context import core_box, load_context
from .dataset import FULL_TIME_ROWS
from .schema import ALLOWED_CODES, load_annotations

PAGE = Path(__file__).with_name("viewer_page.html")


def _source_names(schedules: Path | None) -> dict[str, str]:
    """source_group → photo file name (e.g. 9_25.jpg), when the photos are available."""
    names = {}
    if schedules and schedules.is_dir():
        for path in sorted(schedules.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                names.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), path.name)
    return names


def _box(box) -> list[int]:
    return [box.x1, box.y1, box.x2, box.y2]


def column_names(root: Path) -> dict[str, str]:
    """Duty names per grid column for display.

    ``configs/all_staff_columns.json`` holds the header names of c02–c36 (read off
    the printed roster); the V1 template's ``column_map`` fills gaps and
    ``configs/all_staff_columns.local.json`` can override any of them.
    """
    names = {}
    for path in (root / "configs/phase1_roster_template.local.json",
                 root / "configs/all_staff_columns.json",
                 root / "configs/all_staff_columns.local.json"):
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            names.update({str(k): v for k, v in payload.get("column_map", payload).items()
                          if isinstance(v, str) and v and not v.startswith("欄位-")})
    return names


def cell_payload(item, image: Image.Image, engine, context: dict, url: str) -> dict:
    from .lines import parse_codes, split_line
    core = core_box(image.width, image.height, context)
    lines, predictions = [], []
    for line, text, confidence in engine.read_lines(image, core):
        codes = parse_codes(text)
        record = {"box": _box(line.box), "text": text, "conf": round(confidence, 4)}
        if line.original_text:
            record['original_text'] = line.original_text
        lines.append(record)
        for code, box in zip(codes, split_line(line, codes)):
            predictions.append({"code": code, "box": _box(box), "conf": round(confidence, 4),
                                "line": len(lines) - 1})
    cell = {"id": item.sample_id, "r": item.row, "c": item.column, "img": url,
            "w": image.width, "h": image.height, "core": list(core), "lines": lines,
            "pred": predictions}
    if item.reviewed:
        cell["truth"] = [{"code": a.text, "role": a.role, "box": _box(a.box)} for a in item.annotations]
    return cell


def sheet_payload(dataset: Path, items: list, engine, context: dict, url_for, *, name: str,
                  overview: str | None) -> dict:
    from . import overlays
    cells = sorted(items, key=lambda item: (item.row, item.column))
    found = overlays.load(dataset, cells[0].source_group)   # boxes laid over the table (notes, call-outs)
    out_cells = []
    for item in cells:
        with Image.open(dataset / item.image) as opened:
            image = opened.copy()
        cell = cell_payload(item, image, engine, context, url_for(dataset / item.image))
        if found is not None:
            found.apply(cell)   # what is read inside them is not a duty: kept in cell["ignored"]
        out_cells.append(cell)
    payload = {"group": cells[0].source_group[:16], "name": name, "split": cells[0].split,
               "rows": sorted({item.row for item in cells}),
               "columns": sorted({item.column for item in cells}),
               "cells": out_cells, "overview": overview, "warnings": grid_warnings(out_cells)}
    if found is not None and (found.boxes or found.unreliable):
        payload["overlays"] = found.to_dict()
    return payload


def grid_warnings(cells: list[dict]) -> list[str]:
    """Flag hour rows whose cell height is far from the sheet's typical height.

    A mis-registered bottom rule squeezes the last hours into the name legend,
    which then reads as random low-confidence codes.
    """
    from statistics import median
    heights = defaultdict(list)
    for cell in cells:
        heights[cell["r"]].append(cell["core"][3] - cell["core"][1])
    if not heights:
        return []
    typical = median(median(v) for v in heights.values())
    bad = [row for row, values in sorted(heights.items())
           if not .7 * typical <= median(values) <= 1.45 * typical]
    if not bad:
        return []
    times = "、".join(FULL_TIME_ROWS.get(row, f"r{row}") for row in bad)
    return [f"格線定位可能有誤：{times} 的格高與其他時段差異大，請按「原圖」確認"]


def common_payload(recognizer: Path, roster: Path | None, context: dict, root: Path) -> dict:
    roster_names = {}
    if roster and roster.is_file():
        roster_names = {k: v for k, v in json.loads(roster.read_text(encoding="utf-8")).items() if v}
    model_info = {"path": recognizer.as_posix(), "context": context}
    report = recognizer.with_suffix(".json")
    if report.is_file():
        model_info["best"] = json.loads(report.read_text(encoding="utf-8")).get("best")
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M"), "model": model_info,
            "time_rows": {str(k): v for k, v in FULL_TIME_ROWS.items()},
            "roster": roster_names, "allowed": list(ALLOWED_CODES),
            "column_names": column_names(root)}


def people(sheet: dict, data: dict, *, threshold=.75) -> list[dict]:
    """Per-person duty list (what a bot reply needs), ordered like the roster legend."""
    order = {code: n for n, code in enumerate(data["allowed"])}
    slots: dict[str, list] = defaultdict(list)
    for cell in sheet["cells"]:
        for p in cell["pred"]:
            slots[p["code"]].append({"row": cell["r"], "time": data["time_rows"].get(str(cell["r"]), ""),
                                     "column": cell["c"],
                                     "duty": data["column_names"].get(str(cell["c"]), f"c{cell['c']:02d}"),
                                     "confidence": p["conf"], "needs_review": p["conf"] < threshold})
    return [{"code": code, "name": data["roster"].get(code), "hours": len({s["row"] for s in items}),
             "needs_review": any(s["needs_review"] for s in items),
             "slots": sorted(items, key=lambda s: (s["row"], s["column"]))}
            for code, items in sorted(slots.items(), key=lambda kv: order.get(kv[0], 10**6))]


def build_viewer(dataset: Path, recognizer: Path, output: Path, *, roster: Path | None = None,
                 schedules: Path | None = None, splits: set[str] | None = None,
                 sheets: list[str] | None = None, limit_sheets: int | None = None,
                 root: Path | None = None, progress=print) -> dict:
    from .lines import LineOCR
    root = root or Path(__file__).resolve().parents[1]
    context = load_context(dataset)
    engine = LineOCR(recognizer, roster, context=context)
    items = load_annotations(dataset / "annotations.jsonl")
    groups: dict[str, list] = defaultdict(list)
    for item in items:
        groups[item.source_group].append(item)
    selected = sorted(groups)
    if sheets:
        selected = [g for g in selected if any(g.startswith(s) for s in sheets)]
    if splits:
        selected = [g for g in selected if groups[g][0].split in splits]
    if limit_sheets:
        selected = selected[:limit_sheets]
    if not selected:
        raise ValueError(f"{dataset} 沒有符合條件的班表")
    names = _source_names(schedules)
    output.mkdir(parents=True, exist_ok=True)
    relative = lambda path: Path(os.path.relpath(path, output)).as_posix()
    started = time.time()
    payload_sheets = []
    for number, group in enumerate(selected, 1):
        debug = dataset / "debug" / f"{group[:16]}.jpg"
        payload_sheets.append(sheet_payload(
            dataset, groups[group], engine, context, relative, name=names.get(group, group[:16]),
            overview=relative(debug) if debug.is_file() else None))
        progress(f"viewer {number}/{len(selected)} {names.get(group, group[:16])}")
    data = {**common_payload(recognizer, roster, context, root), "sheets": payload_sheets}
    (output / "data.js").write_text(
        "window.VIEWER_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8")
    shutil.copyfile(PAGE, output / "index.html")
    summary = _summary(payload_sheets)
    summary.update({"output": str((output / "index.html").resolve()),
                    "seconds": round(time.time() - started, 1)})
    return summary


def _summary(sheets: list[dict]) -> dict:
    """Same cell-level counting as the page (threshold-independent)."""
    tp = fp = fn = labelled = exact = 0
    codes = 0
    for sheet in sheets:
        for cell in sheet["cells"]:
            codes += len(cell["pred"])
            if "truth" not in cell or any(t["role"] != "personnel_code" for t in cell["truth"]):
                continue
            truth = Counter(t["code"] for t in cell["truth"]); pred = Counter(p["code"] for p in cell["pred"])
            overlap = sum((truth & pred).values())
            tp += overlap; fp += sum(pred.values()) - overlap; fn += sum(truth.values()) - overlap
            labelled += 1; exact += truth == pred
    return {"sheets": len(sheets), "cells": sum(len(s["cells"]) for s in sheets), "predicted_codes": codes,
            "labelled_cells": labelled, "labelled_exact": exact / max(1, labelled),
            "precision": tp / max(1, tp + fp), "recall": tp / max(1, tp + fn)}
