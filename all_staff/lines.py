"""V2 line recogniser: data, training and inference.

A text line (``72``, ``#72``, ``IB``, ``甲戊``) is read in one CTC pass and
split into personnel codes afterwards, so side-by-side and stacked codes no
longer depend on a detector separating them.

Training runs in two stages:

1. pretrain on synthetic lines covering every allowed code;
2. fine-tune on synthetic + real lines. Real lines come from both human boxes
   (union per text row, with box jitter) and the segmenter's own output on
   reviewed cells, so the model sees exactly what inference will feed it,
   including hard negatives (rule stubs, ``備``, arrows) labelled as blank.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
from PIL import Image

from .context import core_box, load_context
from .schema import ALLOWED_CODE_SET, CHARSET, Box, load_annotations
from .segment import Line, _merge_rows, _union, digit_groups, parse_codes, segment_lines, split_line

INPUT_SIZE = (128, 32)  # width, height
KIND = "all_staff_line_recognizer_v2"
DUTY_VERSION = 1
RECHECK_CODES = frozenset(("1", "11", "2", "22", "3", "33"))
INDEX = {char: number + 1 for number, char in enumerate(CHARSET)}


def line_tensor(image: Image.Image, size=INPUT_SIZE):
    """Grayscale, keep aspect ratio, fixed height, right-pad; ink = 1."""
    import torch
    width, height = size
    image = image.convert("L")
    w = max(1, min(width, round(image.width * height / max(1, image.height))))
    array = 1 - np.asarray(image.resize((w, height), Image.Resampling.BILINEAR), np.float32) / 255
    low, high = np.percentile(array, 2), array.max()
    array = np.clip((array - low) / max(.15, high - low), 0, 1)
    output = np.zeros((height, width), np.float32); output[:, :w] = array
    return torch.from_numpy(output[None])


def _iou(a: Box, b: Box) -> float:
    w = max(0, min(a.x2, b.x2) - max(a.x1, b.x1)); h = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
    inter = w * h
    return inter / max(1, (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter)


def truth_lines(item) -> list[tuple[Box, str]] | None:
    """Human boxes grouped into text rows; ``None`` if the cell has unclear roles."""
    if any(a.role != "personnel_code" for a in item.annotations):
        return None
    boxes = {a.box: a.text for a in item.annotations}
    output = []
    for row in _merge_rows(list(boxes)):
        row = sorted(row, key=lambda b: b.x1)
        output.append((_union(row), "".join(boxes[b] for b in row)))
    return output


@dataclass(frozen=True)
class LineSample:
    image: str
    box: Box
    text: str
    kind: str  # truth | segment | negative


def collect_lines(dataset: Path, splits: tuple[str, ...]) -> list[LineSample]:
    context = load_context(dataset)
    samples = []
    for item in load_annotations(dataset / "annotations.jsonl", reviewed_only=True):
        if item.split not in splits:
            continue
        truth = truth_lines(item)
        if truth is None:
            continue
        samples.extend(LineSample(item.image, box, text, "truth") for box, text in truth)
        with Image.open(dataset / item.image) as image:
            lines = segment_lines(image, core_box(image.width, image.height, context))
        for line in lines:
            match = max(truth, key=lambda t: _iou(t[0], line.box), default=None)
            if match and _iou(match[0], line.box) >= .5:
                samples.append(LineSample(item.image, line.box, match[1], "segment"))
            elif not match or _iou(match[0], line.box) < .1:
                samples.append(LineSample(item.image, line.box, "", "negative"))
    return samples


def _jitter(box: Box, width: int, height: int, rng: random.Random, amount=.1) -> Box:
    bw, bh = box.x2 - box.x1, box.y2 - box.y1
    r = lambda: rng.uniform(-amount * .6, amount)
    return Box(max(0, int(box.x1 - bw * r())), max(0, int(box.y1 - bh * r())),
               min(width, int(box.x2 + bw * r())), min(height, int(box.y2 + bh * r())))


class RealLines:
    def __init__(self, dataset: Path, samples: list[LineSample], jitter=False, seed=0):
        self.dataset, self.samples, self.jitter = dataset, samples, jitter
        self.rng, self.cache = random.Random(seed), {}

    def __len__(self): return len(self.samples)

    def __getitem__(self, index):
        import torch
        sample = self.samples[index]
        if sample.image not in self.cache:
            with Image.open(self.dataset / sample.image) as image:
                self.cache[sample.image] = image.convert("L")
        image = self.cache[sample.image]
        box = _jitter(sample.box, image.width, image.height, self.rng) if self.jitter else sample.box
        crop = image.crop((box.x1, box.y1, box.x2, box.y2))
        if self.jitter:
            from PIL import ImageEnhance
            crop = ImageEnhance.Contrast(crop).enhance(self.rng.uniform(.75, 1.25))
        return line_tensor(crop), torch.tensor([INDEX[c] for c in sample.text], dtype=torch.long)


class SynthLines:
    def __init__(self, count: int, backgrounds, seed=0):
        self.count, self.backgrounds, self.seed = count, backgrounds, seed
        self.epoch = 0

    def __len__(self): return self.count

    def __getitem__(self, index):
        import torch
        from .synth import random_line, render
        rng = random.Random(hash((self.seed, self.epoch, index)))
        text, label = random_line(rng)
        return (line_tensor(render(text, rng, self.backgrounds)),
                torch.tensor([INDEX[c] for c in label], dtype=torch.long))


def collate(batch):
    import torch
    images, targets = zip(*batch)
    return (torch.stack(images), torch.cat(targets),
            torch.tensor([len(t) for t in targets], dtype=torch.long))


def decode(log_probs) -> list[tuple[str, float]]:
    """Greedy CTC decode; confidence = lowest emitted-character probability
    (for an empty line: the mean blank probability)."""
    probabilities = log_probs.exp()
    scores, indexes = probabilities.max(2)
    output = []
    for b in range(indexes.shape[1]):
        previous, chars, confidences = 0, [], []
        for index, score in zip(indexes[:, b].tolist(), scores[:, b].tolist()):
            if index and index != previous:
                chars.append(CHARSET[index - 1]); confidences.append(score)
            previous = index
        confidence = min(confidences) if confidences else float(probabilities[:, b, 0].mean())
        output.append(("".join(chars), float(confidence)))
    return output


def _device(torch, requested=None):
    if requested:
        if requested == "mps" and not torch.backends.mps.is_available():
            raise ValueError("這台機器沒有可用的 MPS")
        return torch.device(requested)
    # MPS produced checkpoints that did not reproduce after reload (2026-09-28);
    # prefer CUDA, otherwise CPU.
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def line_accuracy(model, data, device, batch_size=128) -> dict:
    import torch
    from torch.utils.data import DataLoader
    model.eval(); correct = total = 0; errors = []
    with torch.no_grad():
        for images, targets, lengths in DataLoader(data, batch_size, collate_fn=collate):
            decoded = decode(model(images.to(device)).cpu()); offset = 0
            for (text, confidence), length in zip(decoded, lengths.tolist()):
                expected = "".join(CHARSET[i - 1] for i in targets[offset:offset + length].tolist())
                offset += length; total += 1; correct += text == expected
                if text != expected and len(errors) < 40:
                    errors.append((expected, text, round(confidence, 2)))
    return {"exact": correct / max(1, total), "total": total, "errors": errors}


STAGE_NAMES = {"pretrain": "合成預訓練", "finetune": "真實資料微調"}


class TrainingStopped(Exception):
    """Raised inside the loop when the monitor (or a ``.stop`` file) asks to stop."""


class _Progress:
    """Writes ``<output>.json`` for the monitor: status, step progress, loss curve, epochs."""

    def __init__(self, path: Path, meta: dict):
        self.path, self.stop_file = path, path.with_suffix(".stop")
        self.stop_file.unlink(missing_ok=True)
        self.state = {"status": "preparing", "detail": "準備資料", **meta, "started_at": time.time(),
                      "progress": {}, "stages": [], "loss_curve": [], "history": [], "best": None,
                      "val_errors": []}
        self.last_write, self.step_times = 0.0, []
        self.write(force=True)

    def write(self, force=False):
        now = time.time()
        if force or now - self.last_write >= 1.5:
            self.state["updated_at"] = now
            curve = self.state["loss_curve"]
            if len(curve) > 3000:  # keep the file small: thin the older half
                self.state["loss_curve"] = curve[:len(curve) // 2:2] + curve[len(curve) // 2:]
            _write(self.path, self.state); self.last_write = now

    def check_stop(self):
        if self.stop_file.exists():
            raise TrainingStopped()


def train_line_recognizer(dataset: Path, output: Path, *, synth_samples=30000,
                          pretrain_epochs=8, finetune_epochs=6, real_repeat=6,
                          batch_size=64, learning_rate=1e-3, workers=2, device=None,
                          seed=0, init: Path | None = None) -> dict:
    """Two-stage training. Progress for ``all_staff.monitor`` goes to ``<output>.json``;
    creating ``<output>.stop`` (the monitor's stop button) ends it after the current step
    and keeps the best checkpoint."""
    import torch
    from torch.utils.data import ConcatDataset, DataLoader
    from .models import build_recognizer
    from .synth import blank_backgrounds, font_paths
    output.parent.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(seed); random.seed(seed)
    device = _device(torch, device)
    config = {"synth_samples": synth_samples, "pretrain_epochs": pretrain_epochs,
              "finetune_epochs": finetune_epochs, "real_repeat": real_repeat, "batch_size": batch_size,
              "learning_rate": learning_rate, "workers": workers, "seed": seed,
              "init": str(init) if init else None}
    progress = _Progress(output.with_suffix(".json"), {
        "kind": KIND, "device": str(device), "dataset": str(dataset), "output": str(output),
        "config": config, "threads": torch.get_num_threads()})
    state = progress.state
    started = time.time()
    try:
        font_paths()  # fail early with a clear message
        context = load_context(dataset)
        state["detail"] = "讀取背景與切分真實文字行"; progress.write(force=True)
        backgrounds = blank_backgrounds(dataset)
        train_lines = collect_lines(dataset, ("train",))
        val_lines = collect_lines(dataset, ("val",))
        if not train_lines or not val_lines:
            raise ValueError("line recognizer 需要已複核的 train 與 val 資料")
        state["data"] = {"train_lines": len(train_lines), "val_lines": len(val_lines),
                         "train_kinds": {k: sum(s.kind == k for s in train_lines)
                                         for k in ("truth", "segment", "negative")}}
        val = RealLines(dataset, val_lines)
        model = build_recognizer().to(device)
        if init:
            model.load_state_dict(torch.load(init, map_location=device, weights_only=False)["model_state"])
        ctc = torch.nn.CTCLoss(blank=0, zero_infinity=True)
        stages = []
        if pretrain_epochs:
            stages.append(("pretrain", SynthLines(synth_samples, backgrounds, seed), pretrain_epochs,
                           learning_rate))
        if finetune_epochs:
            mix = ConcatDataset([SynthLines(max(2000, synth_samples // 5), backgrounds, seed + 1)] +
                                [RealLines(dataset, train_lines, jitter=True, seed=seed + k)
                                 for k in range(real_repeat)])
            stages.append(("finetune", mix, finetune_epochs, learning_rate * .3))
        per_epoch = {name: (len(data) + batch_size - 1) // batch_size for name, data, _, _ in stages}
        total_steps = sum(per_epoch[name] * epochs for name, _, epochs, _ in stages)
        offset = 0
        for name, data, epochs, _lr in stages:
            state["stages"].append({"name": name, "start": offset, "end": offset + per_epoch[name] * epochs,
                                    "epochs": epochs, "samples": len(data)})
            offset += per_epoch[name] * epochs
        best = {"val_exact": -1.0}
        global_step = 0
        validation_seconds = []

        def run(stage, data, epochs, lr):
            nonlocal global_step
            optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
            steps = epochs * per_epoch[stage]
            schedule = torch.optim.lr_scheduler.OneCycleLR(optimizer, lr, total_steps=steps, pct_start=.15)
            for epoch in range(1, epochs + 1):
                for part in getattr(data, "datasets", [data]):
                    if isinstance(part, SynthLines): part.epoch += 1
                model.train(); total = 0.0; seen = 0; t0 = time.time()
                state["status"] = "running"; state["detail"] = f"{STAGE_NAMES[stage]} 第 {epoch}/{epochs} 輪"
                loader = DataLoader(data, batch_size, shuffle=True, collate_fn=collate,
                                    num_workers=workers, persistent_workers=False)
                last = time.time()
                for step, (images, targets, lengths) in enumerate(loader, 1):
                    progress.check_stop()
                    images = images.to(device)
                    log_probs = model(images)
                    input_lengths = torch.full((images.shape[0],), log_probs.shape[0], dtype=torch.long)
                    loss = ctc(log_probs.float().cpu() if device.type == "mps" else log_probs,
                               targets.to(log_probs.device), input_lengths, lengths)
                    optimizer.zero_grad(); loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
                    optimizer.step(); schedule.step()
                    value = float(loss.detach()); total += value * len(images); seen += len(images)
                    global_step += 1
                    now = time.time(); progress.step_times = (progress.step_times + [now - last])[-50:]; last = now
                    step_time = sum(progress.step_times) / len(progress.step_times)
                    remaining_epochs = (epochs - epoch) + sum(e for n, _, e, _ in stages[
                        [s[0] for s in stages].index(stage) + 1:])
                    eta = (total_steps - global_step) * step_time + remaining_epochs * (
                        validation_seconds[-1] if validation_seconds else 0)
                    lr_now = float(schedule.get_last_lr()[0])
                    state["progress"] = {"stage": stage, "epoch": epoch, "epochs": epochs, "step": step,
                                         "steps": per_epoch[stage], "global_step": global_step,
                                         "total_steps": total_steps, "loss": value,
                                         "epoch_loss": total / max(1, seen), "lr": lr_now,
                                         "samples_per_s": len(images) / max(1e-6, step_time),
                                         "elapsed_s": now - started, "eta_s": eta}
                    state["loss_curve"].append([global_step, round(value, 5), lr_now])
                    progress.write()
                state["detail"] = f"{STAGE_NAMES[stage]} 第 {epoch}/{epochs} 輪：驗證中"; progress.write(force=True)
                v0 = time.time()
                metrics = line_accuracy(model, val, device)
                validation_seconds.append(time.time() - v0)
                record = {"stage": stage, "epoch": epoch, "global_step": global_step,
                          "loss": total / max(1, seen), "val_exact": metrics["exact"],
                          "seconds": round(time.time() - t0, 1), "lr": float(schedule.get_last_lr()[0])}
                state["history"].append(record); state["val_errors"] = metrics["errors"][:20]
                print(json.dumps(record), flush=True)
                if metrics["exact"] > best["val_exact"]:
                    best.clear(); best.update(record)
                    torch.save({"kind": KIND, "model_state": model.state_dict(), "input_size": INPUT_SIZE,
                                "charset": CHARSET, "context": context, "stage": stage, "epoch": epoch,
                                "val_exact": metrics["exact"]}, output)
                    state["best"] = dict(best)
                progress.write(force=True)

        try:
            for stage in stages:
                run(*stage)
            final_status = "complete"
        except (TrainingStopped, KeyboardInterrupt):
            final_status = "stopped"
        state["detail"] = "以 CPU 重新載入最佳模型驗證"; progress.write(force=True)
        report = {"status": final_status, "device": str(device), "best": state["best"],
                  "history": state["history"], "train_lines": len(train_lines), "val_lines": len(val_lines),
                  "minutes": round((time.time() - started) / 60, 1)}
        if output.is_file() and state["best"]:
            checkpoint = torch.load(output, map_location="cpu", weights_only=False)
            reloaded = build_recognizer(); reloaded.load_state_dict(checkpoint["model_state"])
            check = line_accuracy(reloaded, val, torch.device("cpu"))
            report.update({"reload_val_exact": check["exact"], "val_errors": check["errors"],
                           "reload_consistent": abs(check["exact"] - state["best"]["val_exact"]) < 1e-6})
            state["val_errors"] = check["errors"][:20]
        state.update({k: v for k, v in report.items() if k not in ("history", "best")})
        state["detail"] = {"complete": "完成", "stopped": "已停止（保留最佳模型）"}[final_status]
        progress.write(force=True)
        progress.stop_file.unlink(missing_ok=True)
        return report
    except Exception as exc:
        state.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}", "detail": "失敗"})
        progress.write(force=True)
        raise


def _write(path: Path, payload: dict):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class Prediction:
    box: Box
    code: str
    name: str | None
    confidence: float
    needs_review: bool
    line_text: str = ""


class LineOCR:
    """Segmenter + line recogniser. ``predict_cell`` matches ``AllStaffOCR``."""

    def __init__(self, recognizer: Path, roster: Path | None = None, *, device="cpu", context=None):
        import torch
        from .models import build_recognizer
        self.torch, self.device = torch, torch.device(device)
        checkpoint = torch.load(recognizer, map_location=self.device, weights_only=False)
        if checkpoint.get("kind") != KIND:
            raise ValueError("checkpoint 不是 V2 line recognizer")
        self.size = tuple(checkpoint["input_size"])
        self.context = context if context is not None else checkpoint.get("context", {"fx": 0, "fy": 0})
        self.model = build_recognizer(tuple(checkpoint["charset"])).to(self.device)
        self.model.load_state_dict(checkpoint["model_state"]); self.model.eval()
        self.roster = json.loads(roster.read_text(encoding="utf-8")) if roster and roster.is_file() else {}

    def read_lines(self, image: Image.Image, core=None) -> list[tuple[Line, str, float]]:
        core = core or core_box(image.width, image.height, self.context)
        lines = segment_lines(image, core)
        if not lines:
            return []
        batch = self.torch.stack([line_tensor(image.crop((l.box.x1, l.box.y1, l.box.x2, l.box.y2)), self.size)
                                  for l in lines])
        with self.torch.no_grad():
            decoded = decode(self.model(batch.to(self.device)).cpu())
        jobs, slots = [], {}
        for index, (line, (text, _)) in enumerate(zip(lines, decoded)):
            if text not in RECHECK_CODES:
                continue
            groups = digit_groups(image, line)
            if not 1 <= len(groups) <= 3:
                continue
            slots[index] = []
            for box in groups:
                height = box.y2 - box.y1
                px = max(height * .14, (height * .75 - (box.x2 - box.x1)) / 2)
                py = height * .14
                crop = image.crop((max(0, int(box.x1 - px)), max(0, int(box.y1 - py)),
                                   min(image.width, int(box.x2 + px)), min(image.height, int(box.y2 + py))))
                slots[index].append(len(jobs))
                jobs.append(line_tensor(crop, self.size))
        if jobs:
            with self.torch.no_grad():
                verified = decode(self.model(self.torch.stack(jobs).to(self.device)).cpu())
            for index, positions in slots.items():
                readings = [verified[p] for p in positions]
                if any((text and (not text.isdigit() or len(text) > 2) or score < .85)
                       for text, score in readings):
                    continue
                candidate = ''.join(text for text, _ in readings)
                if candidate not in ALLOWED_CODE_SET or not candidate.isdigit():
                    continue
                original, _ = decoded[index]
                if original != candidate:
                    lines[index] = replace(lines[index], original_text=original)
                decoded[index] = (candidate, min(score for _, score in readings))
        return [(line, text, confidence) for line, (text, confidence) in zip(lines, decoded)]

    def predict_cell(self, image: Image.Image, *, minimum_confidence=.75, core=None) -> list[Prediction]:
        output = []
        for line, text, confidence in self.read_lines(image, core):
            codes = parse_codes(text)
            leftover = text.replace("#", "") != "".join(codes)
            for code, box in zip(codes, split_line(line, codes)):
                name = self.roster.get(code)
                output.append(Prediction(box, code, name, confidence,
                                         confidence < minimum_confidence or leftover or
                                         code not in ALLOWED_CODE_SET or (bool(self.roster) and name is None),
                                         text))
        return output

    def analyze_cells(self, cells: Path) -> list[dict]:
        output = []
        for path in sorted(cells.glob("*.png")):
            with Image.open(path) as image:
                predictions = self.predict_cell(image.copy())
            output.append({"cell": path.name, "predictions": [asdict(p) for p in predictions]})
        return output
