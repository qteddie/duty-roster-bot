"""One-shot verification after the seed training jobs finish (no label writes)."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def verify(snapshot: Path, models: Path) -> dict:
    import torch
    from torch.utils.data import DataLoader
    from .dataset import RecognitionDataset, recognition_collate
    from .evaluate import evaluate
    from .inference import AllStaffOCR
    from .models import build_recognizer, ctc_greedy_decode
    from .schema import CHARSET
    torch.set_num_threads(2)
    ckpt = torch.load(models / "recognizer_cpu.pt", map_location="cpu", weights_only=False)
    recognizer = build_recognizer(); recognizer.load_state_dict(ckpt["model_state"]); recognizer.eval()
    crop_results = {}
    for split in ("val", "test"):
        data = RecognitionDataset(snapshot, split)
        correct = 0
        with torch.no_grad():
            for images, targets, lengths in DataLoader(data, batch_size=32, collate_fn=recognition_collate):
                decoded = ctc_greedy_decode(recognizer(images)); offset = 0
                for (text, _), length in zip(decoded, lengths.tolist()):
                    expected = "".join(CHARSET[n-1] for n in targets[offset:offset+length].tolist())
                    offset += length; correct += text == expected
        crop_results[split] = {"correct": correct, "total": len(data), "exact_match": correct/max(1, len(data))}
    consistent = abs(crop_results["val"]["exact_match"] - ckpt["val_exact_match"]) < 1e-6
    result = {"status": "verified" if consistent else "checkpoint_mismatch",
              "checkpoint_consistent": consistent, "crop_recognition": crop_results,
              "recognizer": str((models/"recognizer_cpu.pt").resolve()),
              "detector": str((models/"detector.pt").resolve()),
              "device": "cpu", "labels_written": False}
    if consistent:
        engine = AllStaffOCR(models/"detector.pt", models/"recognizer_cpu.pt",
                             Path("configs/all_staff_roster.local.json"), device="cpu")
        result["end_to_end"] = evaluate(snapshot, engine, split="test")
    return result


if __name__ == "__main__":
    from .train import _atomic_json
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    args = parser.parse_args()
    output = args.models/"verification.json"
    try:
        deadline = time.monotonic() + 4*60*60
        while True:
            complete = all((args.models/name).is_file() and
                           json.loads((args.models/name).read_text())["status"] == "complete"
                           for name in ("recognizer_cpu.json", "detector.json"))
            if complete:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Training did not complete within four hours")
            time.sleep(15)
        report = verify(args.snapshot, args.models)
        _atomic_json(output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    except Exception as exc:
        _atomic_json(output, {"status": "failed", "error": str(exc), "labels_written": False})
        raise
