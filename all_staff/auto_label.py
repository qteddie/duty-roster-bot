from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from .schema import (ALLOWED_CODE_SET, CHINESE_CODES, Annotation, Box, CellAnnotation, load_annotations,
                     repair_annotation_dimensions, save_annotations)


def candidate_boxes(image: Image.Image) -> list[Box]:
    """Find compact ink groups after removing residual cell borders."""
    gray = np.asarray(image.convert("L"))
    height, width = gray.shape
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, width//2), 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(18, height//2))))
    cleaned = cv2.bitwise_and(ink, cv2.bitwise_not(cv2.bitwise_or(horizontal, vertical)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(cleaned, 8)
    minimum = max(8, round(width*height*.0007))
    kept = np.zeros_like(cleaned)
    for index, (x, y, w, h, area) in enumerate(stats[1:], 1):
        if area >= minimum and w < width*.8 and h < height*.8:
            kept[labels == index] = 255
    # Join strokes within one printed code, including the two digits of 10–84.
    joined = cv2.dilate(kept, cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(3, round(width*.055)), max(3, round(height*.025)))), iterations=1)
    contours, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w*h < width*height*.006 or h < height*.08:
            continue
        if (x < width*.08 or x+w > width*.92) and w < width*.14 and h > height*.35:
            continue
        pad_x, pad_y = max(2, round(w*.12)), max(2, round(h*.10))
        boxes.append(Box(max(0, x-pad_x), max(0, y-pad_y), min(width, x+w+pad_x),
                         min(height, y+h+pad_y)))
    boxes = sorted(boxes, key=lambda box: (box.y1, box.x1))
    merged: list[Box] = []
    for box in boxes:
        match = None
        for index, prior in enumerate(merged):
            overlap = max(0, min(box.y2, prior.y2)-max(box.y1, prior.y1))
            vertical_ratio = overlap/max(1, min(box.y2-box.y1, prior.y2-prior.y1))
            gap = max(0, max(box.x1, prior.x1)-min(box.x2, prior.x2))
            if vertical_ratio >= .55 and gap <= max(6, round(width*.10)):
                match = index; break
        if match is None:
            merged.append(box)
        else:
            prior = merged[match]
            merged[match] = Box(min(prior.x1, box.x1), min(prior.y1, box.y1),
                                max(prior.x2, box.x2), max(prior.y2, box.y2))
    return sorted(merged, key=lambda box: (box.y1, box.x1))


def _tesseract(image: Image.Image, language: str, whitelist: str) -> tuple[str, float]:
    crop = ImageOps.expand(image.convert("L"), border=16, fill=255)
    scale = max(2, round(180/max(1, crop.height)))
    crop = crop.resize((crop.width*scale, crop.height*scale), Image.Resampling.LANCZOS)
    buffer = io.BytesIO(); crop.save(buffer, "PNG")
    command = ["tesseract", "stdin", "stdout", "-l", language, "--psm", "13", "tsv",
               "-c", f"tessedit_char_whitelist={whitelist}"]
    result = subprocess.run(command, input=buffer.getvalue(), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=15, check=False)
    candidates = []
    for row in csv.DictReader(result.stdout.decode("utf-8", "ignore").splitlines(), delimiter="\t"):
        text = (row.get("text") or "").strip().upper().replace(" ", "")
        try: confidence = max(0.0, float(row.get("conf", -1))/100)
        except ValueError: confidence = 0.0
        if text: candidates.append((text, confidence))
    return max(candidates, key=lambda item: item[1], default=("", 0.0))


def recognize_candidate(image: Image.Image) -> tuple[str, float]:
    latin = _tesseract(image, "eng", "0123456789ABCDEFGHIJKLMN")
    if latin[0] in ALLOWED_CODE_SET and latin[1] >= .75:
        return latin
    chinese = _tesseract(image, "chi_tra", "".join(CHINESE_CODES))
    valid = [item for item in (latin, chinese) if item[0] in ALLOWED_CODE_SET]
    return max(valid, key=lambda item: item[1], default=("", max(latin[1], chinese[1])))


def propose_cell(dataset: Path, item: CellAnnotation) -> CellAnnotation:
    if item.reviewed or item.annotations:
        return item
    with Image.open(dataset/item.image) as opened:
        image = opened.copy()
    boxes = candidate_boxes(image)
    annotations = []
    for box in boxes:
        text, confidence = recognize_candidate(image.crop((box.x1, box.y1, box.x2, box.y2)))
        width, height = box.x2-box.x1, box.y2-box.y1
        edge = box.x1 < image.width*.06 or box.x2 > image.width*.94
        aspect = width/max(1, height)
        # Residual vertical rules are overwhelmingly responsible for false
        # I/1/L proposals. Unknown ink at an outer edge is also a grid artifact,
        # not useful training data. Real codes remain centred in the cell crop.
        if (not text and edge) or aspect < .12:
            continue
        if edge and text in {"I", "1", "L"} and aspect < .45:
            continue
        annotations.append(Annotation(box, text, "personnel_code" if text else "unknown",
                                      "tesseract", confidence))
    # A truly empty cell is safe to close automatically; cells containing ink
    # stay unreviewed even when OCR cannot read it.
    return replace(item, annotations=tuple(annotations), reviewed=not annotations)


def auto_label(dataset: Path, *, workers: int | None = None, limit: int | None = None,
               replace_proposals: bool = False) -> dict:
    if not shutil.which("tesseract"):
        raise RuntimeError("找不到 tesseract；請先安裝 Tesseract 5")
    repair = repair_annotation_dimensions(dataset)
    path = dataset/"annotations.jsonl"
    items = load_annotations(path)
    if replace_proposals:
        items = [replace(item, reviewed=False, annotations=())
                 if item.annotations and all(ann.source == "tesseract" for ann in item.annotations)
                 else item for item in items]
    indexes = [index for index, item in enumerate(items) if not item.reviewed and not item.annotations]
    if limit is not None: indexes = indexes[:limit]
    workers = workers or min(8, max(1, (os.cpu_count() or 2)//2))
    def run(index): return index, propose_cell(dataset, items[index])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for completed, (index, result) in enumerate(pool.map(run, indexes), 1):
            items[index] = result
            if completed % 100 == 0:
                save_annotations(path, items)
                print(f"auto-label {completed}/{len(indexes)}", flush=True)
    save_annotations(path, items)
    proposed = [item for item in items if not item.reviewed and item.annotations]
    return {"processed": len(indexes), "replaced_proposals": replace_proposals,
            "dimension_repair": repair,
            "auto_empty_reviewed": sum(item.reviewed and not item.annotations for item in items),
            "proposed_cells": len(proposed), "uncertain_cells": sum(
                any(ann.role == "unknown" or (ann.confidence or 0) < .8 for ann in item.annotations)
                for item in proposed), "workers": workers}


def model_auto_label(dataset: Path, detector: Path | None, recognizer: Path, roster: Path, *,
                     replace_proposals: bool = True, limit: int | None = None) -> dict:
    """Replace generic OCR suggestions with a trained V2 model (line or legacy)."""
    from .context import load_context
    from .inference import load_engine
    repair = repair_annotation_dimensions(dataset)
    path = dataset/"annotations.jsonl"; items = load_annotations(path)
    if replace_proposals:
        items = [replace(item, reviewed=False, annotations=())
                 if (not item.reviewed and item.annotations and
                     all(ann.source != "human" for ann in item.annotations)) else item
                 for item in items]
    indexes = [index for index, item in enumerate(items)
               if not item.reviewed and not item.annotations]
    if limit is not None: indexes = indexes[:limit]
    engine = load_engine(recognizer, detector=detector, roster=roster,
                         context=load_context(dataset))
    for completed, index in enumerate(indexes, 1):
        item = items[index]
        with Image.open(dataset/item.image) as opened:
            predictions = engine.predict_cell(opened.copy(), minimum_confidence=.80)
        annotations = tuple(Annotation(
            prediction.box, prediction.code,
            "personnel_code" if prediction.code in ALLOWED_CODE_SET else "unknown",
            "model", prediction.confidence,
        ) for prediction in predictions)
        items[index] = replace(item, annotations=annotations, reviewed=False)
        if completed % 100 == 0:
            save_annotations(path, items)
            print(f"model-label {completed}/{len(indexes)}", flush=True)
    save_annotations(path, items)
    proposed = [item for item in items if not item.reviewed and item.annotations]
    return {"processed": len(indexes), "dimension_repair": repair,
            "proposed_cells": len(proposed), "model_proposals": sum(
                any(ann.source == "model" for ann in item.annotations) for item in proposed),
            "uncertain_cells": sum(any(
                ann.role == "unknown" or (ann.confidence or 0) < .8
                for ann in item.annotations) for item in proposed)}
