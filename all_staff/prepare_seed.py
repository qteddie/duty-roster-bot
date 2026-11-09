"""Back up annotations and keep only the first reviewed seed cells for training."""
from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .schema import load_annotations, save_annotations


def prepare_seed(dataset: Path, keep: int = 1620) -> dict:
    if keep < 1:
        raise ValueError("keep must be positive")
    manifest = dataset / "annotations.jsonl"
    items = load_annotations(manifest)
    reviewed = sorted((item for item in items if item.reviewed),
                      key=lambda item: (item.row, item.column, item.source_group))
    if len(reviewed) < keep:
        raise ValueError(f"Only {len(reviewed)} reviewed cells; requested {keep}")
    selected = {item.sample_id for item in reviewed[:keep]}
    low_confidence = {item.sample_id for item in items if any(
        ann.role == "unknown" or (ann.confidence is not None and ann.confidence < .8)
        for ann in item.annotations)}
    retained = selected - low_confidence
    updated = [replace(item, reviewed=item.sample_id in retained) for item in items]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = dataset / "backups" / f"before-seed-{stamp}.jsonl"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest, backup)
    snapshot = dataset / "training_snapshots" / f"seed-{stamp}"
    snapshot.mkdir(parents=True)
    (snapshot / "images").symlink_to((dataset / "images").resolve(), target_is_directory=True)
    save_annotations(snapshot / "annotations.jsonl", [item for item in updated if item.reviewed])
    save_annotations(manifest, updated)
    report = {"backup": str(backup.resolve()), "training_snapshot": str(snapshot.resolve()),
              "previous_reviewed": len(reviewed), "selected_first": keep,
              "low_confidence_excluded_from_seed": len(selected & low_confidence),
              "reviewed": len(retained), "pending": len(items) - len(retained),
              "reset_reviewed": len(reviewed) - len(retained),
              "splits": dict(Counter(item.split for item in updated if item.reviewed)),
              "code_counts": dict(Counter(ann.text for item in updated if item.reviewed
                                          for ann in item.annotations if ann.role == "personnel_code"))}
    (snapshot / "seed_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--keep", type=int, default=1620)
    args = parser.parse_args()
    print(json.dumps(prepare_seed(args.dataset, args.keep), ensure_ascii=False, indent=2))
