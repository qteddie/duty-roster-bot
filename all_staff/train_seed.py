"""Train and verify seed OCR models without changing annotation data."""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="人工種子全員 OCR：CPU 辨識器 → 偵測器 → 驗證")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recognizer-epochs", type=int, default=40)
    parser.add_argument("--detector-epochs", type=int, default=30)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--detector-device", choices=("cpu", "auto"), default="cpu")
    args = parser.parse_args()
    if not (args.dataset / "annotations.jsonl").is_file():
        parser.error("dataset 必須包含 annotations.jsonl")
    if any(value < 1 for value in (args.recognizer_epochs, args.detector_epochs, args.threads)):
        parser.error("epochs 與 threads 必須大於零")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output 已有檔案，請使用新的資料夾，避免覆寫模型")
    import torch
    import all_staff.train as training
    from .verify_seed_run import verify
    torch.set_num_threads(args.threads)
    original_device = training._device
    try:
        training._device = lambda torch: torch.device("cpu")
        print("[1/3] CPU 辨識器訓練（不修改標註）", flush=True)
        training.train_recognizer(args.dataset, args.output / "recognizer_cpu.pt",
                                  epochs=args.recognizer_epochs)
        training._device = original_device if args.detector_device == "auto" else lambda torch: torch.device("cpu")
        print("[2/3] 框位置偵測器訓練", flush=True)
        training.train_detector(args.dataset, args.output / "detector.pt",
                                epochs=args.detector_epochs)
        print("[3/3] 模型重新載入與獨立測試", flush=True)
        report = verify(args.dataset, args.output)
        training._atomic_json(args.output / "verification.json", report)
        print(f"完成：{args.output.resolve() / 'verification.json'}", flush=True)
    except KeyboardInterrupt:
        print("已停止；已儲存的 checkpoint 保留。", flush=True)
        for name in ("recognizer_cpu.json", "detector.json"):
            path = args.output / name
            if path.is_file():
                import json
                report = json.loads(path.read_text())
                if report.get("status") == "running":
                    report["status"] = "stopped"
                    training._atomic_json(path, report)
        raise SystemExit(130)
    finally:
        training._device = original_device


if __name__ == "__main__":
    main()
