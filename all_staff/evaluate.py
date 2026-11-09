from __future__ import annotations

from collections import Counter
from pathlib import Path

from PIL import Image

from .inference import AllStaffOCR
from .schema import Box, load_annotations


def _iou(left: Box, right: Box) -> float:
    width = max(0, min(left.x2, right.x2) - max(left.x1, right.x1))
    height = max(0, min(left.y2, right.y2) - max(left.y1, right.y1))
    intersection = width * height
    union = ((left.x2-left.x1)*(left.y2-left.y1) +
             (right.x2-right.x1)*(right.y2-right.y1) - intersection)
    return intersection / max(1, union)


def evaluate(dataset: Path, engine: AllStaffOCR, *, split="test", iou_threshold=.5) -> dict:
    splits = set(split.split(","))
    items = [item for item in load_annotations(dataset/"annotations.jsonl", reviewed_only=True)
             if item.split in splits and all(ann.role == "personnel_code" for ann in item.annotations)]
    if not items:
        raise ValueError(f"沒有已複核的 {split} 資料")
    true_positive = false_positive = false_negative = 0
    cell_tp = cell_fp = cell_fn = cells_exact = 0
    errors = []
    by_code: dict[str, dict[str, int]] = {}
    for item in items:
        truths = [ann for ann in item.annotations if ann.role == "personnel_code"]
        with Image.open(dataset/item.image) as image:
            predictions = engine.predict_cell(image.copy(), minimum_confidence=0)
        # Cell-level: which people are on duty in this cell (what the bot needs)
        truth_codes = Counter(ann.text for ann in truths)
        predicted_codes = Counter(p.code for p in predictions)
        overlap = sum((truth_codes & predicted_codes).values())
        cell_tp += overlap; cell_fp += sum(predicted_codes.values()) - overlap
        cell_fn += sum(truth_codes.values()) - overlap
        cells_exact += truth_codes == predicted_codes
        if truth_codes != predicted_codes and len(errors) < 60:
            errors.append({"sample_id": item.sample_id, "truth": sorted(truth_codes.elements()),
                           "predicted": sorted(predicted_codes.elements())})
        unmatched = set(range(len(truths)))
        for prediction in predictions:
            candidates = [(index, _iou(prediction.box, truths[index].box)) for index in unmatched
                          if prediction.code == truths[index].text]
            match = max(candidates, key=lambda pair: pair[1], default=None)
            if match and match[1] >= iou_threshold:
                unmatched.remove(match[0]); true_positive += 1
                by_code.setdefault(prediction.code, {"tp": 0, "fp": 0, "fn": 0})["tp"] += 1
            else:
                false_positive += 1
                by_code.setdefault(prediction.code or "<blank>", {"tp": 0, "fp": 0, "fn": 0})["fp"] += 1
        for index in unmatched:
            false_negative += 1
            by_code.setdefault(truths[index].text, {"tp": 0, "fp": 0, "fn": 0})["fn"] += 1
    precision = true_positive/max(1, true_positive+false_positive)
    recall = true_positive/max(1, true_positive+false_negative)
    for counts in by_code.values():
        counts["precision"] = counts["tp"]/max(1, counts["tp"]+counts["fp"])
        counts["recall"] = counts["tp"]/max(1, counts["tp"]+counts["fn"])
    cell_precision = cell_tp/max(1, cell_tp+cell_fp); cell_recall = cell_tp/max(1, cell_tp+cell_fn)
    return {"split": split, "cells": len(items), "iou_threshold": iou_threshold,
            "cell_level": {"precision": cell_precision, "recall": cell_recall,
                           "f1": 2*cell_precision*cell_recall/max(1e-12, cell_precision+cell_recall),
                           "cells_exact": cells_exact/len(items), "tp": cell_tp, "fp": cell_fp,
                           "fn": cell_fn, "errors": errors},
            "true_positive": true_positive, "false_positive": false_positive,
            "false_negative": false_negative, "precision": precision, "recall": recall,
            "f1": 2*precision*recall/max(1e-12, precision+recall), "by_code": by_code}
