from __future__ import annotations

import json
import time
from pathlib import Path

from .dataset import DetectionDataset, RecognitionDataset, recognition_collate
from .models import build_detector, build_recognizer
from .schema import CHARSET


def _device(torch):
    return torch.device("mps" if getattr(torch.backends, "mps", None) and
                        torch.backends.mps.is_available() else
                        "cuda" if torch.cuda.is_available() else "cpu")


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def train_detector(dataset: Path, output: Path, *, epochs=30, batch_size=16,
                   learning_rate=3e-4) -> dict:
    import torch
    from torch.utils.data import DataLoader
    device = _device(torch)
    train = DetectionDataset(dataset, "train", augment=True)
    val = DetectionDataset(dataset, "val")
    if not train or not val:
        raise ValueError("detector 需要已複核的 train 與 val 標註")
    model = build_detector().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-4)
    bce = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor([4.0], device=device))
    best, history = -1.0, []
    output.parent.mkdir(parents=True, exist_ok=True)
    for epoch in range(1, epochs + 1):
        model.train(); total = 0.0
        for images, masks in DataLoader(train, batch_size=batch_size, shuffle=True):
            images, masks = images.to(device), masks.to(device)
            optimizer.zero_grad(); logits = model(images)
            probs = logits.sigmoid()
            intersection = (probs*masks).sum((1,2,3))
            dice_loss = 1 - ((2*intersection+1) / (probs.sum((1,2,3))+masks.sum((1,2,3))+1)).mean()
            loss = bce(logits, masks) + dice_loss
            loss.backward(); optimizer.step(); total += float(loss)*len(images)
        model.eval(); intersections = unions = 0
        with torch.no_grad():
            for images, masks in DataLoader(val, batch_size=batch_size):
                predicted = model(images.to(device)).sigmoid().cpu() >= .5
                truth = masks >= .5
                intersections += int((predicted & truth).sum())
                unions += int((predicted | truth).sum())
        iou = intersections / max(1, unions)
        history.append({"epoch": epoch, "loss": total/len(train), "val_mask_iou": iou})
        if iou > best:
            best = iou
            torch.save({"kind": "all_staff_detector_v1", "model_state": model.state_dict(),
                        "input_size": (256, 128), "threshold": .5, "min_area": 32,
                        "epoch": epoch, "val_mask_iou": iou}, output)
        _atomic_json(output.with_suffix(".json"), {"status": "running", "device": str(device),
                     "best_val_mask_iou": best, "history": history, "updated_at": time.time()})
    report = {"status": "complete", "device": str(device), "best_val_mask_iou": best,
              "history": history, "completed_at": time.time()}
    _atomic_json(output.with_suffix(".json"), report)
    return report


def train_recognizer(dataset: Path, output: Path, *, epochs=40, batch_size=32,
                     learning_rate=3e-4) -> dict:
    import torch
    from torch.utils.data import DataLoader
    from .models import ctc_greedy_decode
    device = _device(torch)
    train = RecognitionDataset(dataset, "train", augment=True)
    val = RecognitionDataset(dataset, "val")
    if not train or not val:
        raise ValueError("recognizer 需要已複核且含人員代號的 train 與 val 標註")
    model = build_recognizer().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-4)
    loss_fn = torch.nn.CTCLoss(blank=0, zero_infinity=True)
    loaders = lambda data, shuffle=False: DataLoader(data, batch_size=batch_size, shuffle=shuffle,
                                                     collate_fn=recognition_collate)
    best, history = -1.0, []
    output.parent.mkdir(parents=True, exist_ok=True)
    for epoch in range(1, epochs + 1):
        model.train(); total = 0.0
        for images, targets, lengths in loaders(train, True):
            images, targets = images.to(device), targets.to(device)
            optimizer.zero_grad(); logits = model(images)
            input_lengths = torch.full((images.shape[0],), logits.shape[0], dtype=torch.long)
            loss = loss_fn(logits, targets, input_lengths, lengths)
            loss.backward(); torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step(); total += float(loss)*len(images)
        model.eval(); correct = count = 0
        with torch.no_grad():
            for images, targets, lengths in loaders(val):
                decoded = ctc_greedy_decode(model(images.to(device)).cpu())
                offset = 0
                for (text, _), length in zip(decoded, lengths.tolist()):
                    expected = "".join(CHARSET[index-1] for index in targets[offset:offset+length].tolist())
                    correct += text == expected; count += 1; offset += length
        exact = correct/max(1, count)
        history.append({"epoch": epoch, "loss": total/len(train), "val_exact_match": exact})
        if exact > best:
            best = exact
            torch.save({"kind": "all_staff_recognizer_v1", "model_state": model.state_dict(),
                        "input_size": (192, 48), "charset": CHARSET, "epoch": epoch,
                        "val_exact_match": exact}, output)
        _atomic_json(output.with_suffix(".json"), {"status": "running", "device": str(device),
                     "best_val_exact_match": best, "history": history, "updated_at": time.time()})
    report = {"status": "complete", "device": str(device), "best_val_exact_match": best,
              "history": history, "completed_at": time.time()}
    _atomic_json(output.with_suffix(".json"), report)
    return report
