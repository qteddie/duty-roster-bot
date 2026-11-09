"""CPU prelabel worker with compare-and-swap merging through the annotation server."""
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image

from .context import load_context
from .inference import load_engine
from .schema import ALLOWED_CODE_SET
from .train import _atomic_json


def request(url: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=120) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--server", default="http://127.0.0.1:8777")
    parser.add_argument("--recognizer", type=Path,
                        help="預設：models 內的 recognizer.pt（V2 line）或 recognizer_cpu.pt（V1）")
    args = parser.parse_args()
    import torch
    torch.set_num_threads(2)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = args.dataset/"backups"/f"before-model-label-{stamp}.jsonl"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.dataset/"annotations.jsonl", backup)
    items = request(args.server + "/api/rows?filter=review")
    eligible = [item for item in items if not any(a.get("source") == "human" for a in item["annotations"])]
    report_path = args.models/f"prelabel-{stamp}.json"
    report = {"status": "running", "eligible": len(eligible), "processed": 0, "applied": 0,
              "skipped_concurrent_edits": 0, "proposed_boxes": 0, "cells_with_predictions": 0,
              "empty_predictions": 0, "backup": str(backup.resolve()), "reviewed_written": False}
    _atomic_json(report_path, report)
    from .inference import is_line_checkpoint
    recognizer = args.recognizer or (args.models/"recognizer.pt"
                                     if is_line_checkpoint(args.models/"recognizer.pt")
                                     else args.models/"recognizer_cpu.pt")
    detector = args.models/"detector.pt"
    engine = load_engine(recognizer, detector=detector if detector.is_file() else None,
                         roster=Path("configs/all_staff_roster.local.json"), device="cpu",
                         context=load_context(args.dataset))
    batch = []
    try:
        for count, item in enumerate(eligible, 1):
            with Image.open(args.dataset/item["image"]) as image:
                predictions = engine.predict_cell(image.copy(), minimum_confidence=.8)
            annotations = [{"box": asdict(p.box), "text": p.code,
                            "role": "personnel_code" if p.code in ALLOWED_CODE_SET else "unknown",
                            "confidence": p.confidence} for p in predictions]
            batch.append({"sample_id": item["sample_id"], "expected": item, "annotations": annotations})
            report["processed"] = count; report["proposed_boxes"] += len(annotations)
            report["cells_with_predictions"] += bool(annotations)
            report["empty_predictions"] += not annotations
            if len(batch) >= 50 or count == len(eligible):
                merged = request(args.server + "/api/model-proposals", {"proposals": batch})
                report["applied"] += merged["applied"]
                report["skipped_concurrent_edits"] += merged["skipped"]
                _atomic_json(report_path, report); batch = []
                print(f"prelabel {count}/{len(eligible)} applied={report['applied']}", flush=True)
        report["status"] = "complete"; _atomic_json(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    except BaseException as exc:
        report["status"] = "failed"; report["error"] = str(exc); _atomic_json(report_path, report)
        raise


if __name__ == "__main__":
    main()
