"""Small, local multi-label ResNet classifier for roster cell crops.

The module intentionally avoids torchvision so training and inference only
depend on PyTorch, Pillow, and NumPy.  Imports of PyTorch are lazy enough that
the existing API/bot can continue to run without the optional training stack.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

LABELS = ("丁", "戊", "己", "庚")


def _torch():
    try:
        import torch
        import torch.nn as nn
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError(
            "尚未安裝 PyTorch；請執行：python -m pip install -r requirements-train.txt"
        ) from exc
    return torch, nn


def build_resnet18(num_labels: int = 4):
    """Build a compact grayscale ResNet-18 without torchvision."""
    torch, nn = _torch()

    class BasicBlock(nn.Module):
        expansion = 1

        def __init__(self, in_channels, channels, stride=1):
            super().__init__()
            self.conv1 = nn.Conv2d(in_channels, channels, 3, stride, 1, bias=False)
            self.bn1 = nn.BatchNorm2d(channels)
            self.conv2 = nn.Conv2d(channels, channels, 3, 1, 1, bias=False)
            self.bn2 = nn.BatchNorm2d(channels)
            self.relu = nn.ReLU(inplace=True)
            self.shortcut = (
                nn.Identity() if stride == 1 and in_channels == channels else
                nn.Sequential(
                    nn.Conv2d(in_channels, channels, 1, stride, bias=False),
                    nn.BatchNorm2d(channels),
                )
            )

        def forward(self, x):
            identity = self.shortcut(x)
            x = self.relu(self.bn1(self.conv1(x)))
            x = self.bn2(self.conv2(x))
            return self.relu(x + identity)

    class ResNet18(nn.Module):
        def __init__(self):
            super().__init__()
            self.stem = nn.Sequential(
                nn.Conv2d(1, 32, 5, 2, 2, bias=False),
                nn.BatchNorm2d(32),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(3, 2, 1),
            )
            channels = (32, 64, 128, 256)
            blocks = []
            previous = 32
            for stage, channels_out in enumerate(channels):
                stride = 1 if stage == 0 else 2
                blocks.extend([
                    BasicBlock(previous, channels_out, stride),
                    BasicBlock(channels_out, channels_out),
                ])
                previous = channels_out
            self.body = nn.Sequential(*blocks)
            self.pool = nn.AdaptiveAvgPool2d(1)
            self.dropout = nn.Dropout(0.15)
            self.head = nn.Linear(256, num_labels)

        def forward(self, x):
            x = self.body(self.stem(x))
            x = self.pool(x).flatten(1)
            return self.head(self.dropout(x))

    return ResNet18()


def prepare_image(image: Image.Image, size: int = 128, *, augment: bool = False):
    """Pad a cell crop to square and return a normalized 1×H×W tensor."""
    torch, _ = _torch()
    image = image.convert("L")
    if augment:
        import random
        image = ImageEnhance.Brightness(image).enhance(random.uniform(0.82, 1.18))
        image = ImageEnhance.Contrast(image).enhance(random.uniform(0.8, 1.25))
        if random.random() < 0.30:
            image = ImageOps.autocontrast(image, cutoff=random.uniform(0, 2))
        if random.random() < 0.35:
            image = image.filter(ImageFilter.GaussianBlur(random.uniform(0.15, 0.8)))
        elif random.random() < 0.30:
            image = ImageEnhance.Sharpness(image).enhance(random.uniform(1.2, 2.0))
        if random.random() < 0.20:
            # MinFilter thickens dark ink; MaxFilter thins it. Both simulate
            # different printers, focus and exposure without changing glyphs.
            image = image.filter(random.choice((ImageFilter.MinFilter(3), ImageFilter.MaxFilter(3))))
        width, height = image.size
        scale_jitter = random.uniform(0.92, 1.08)
        translate_x = random.uniform(-0.05, 0.05) * width
        translate_y = random.uniform(-0.05, 0.05) * height
        image = image.transform(
            image.size, Image.Transform.AFFINE,
            (1 / scale_jitter, 0, -translate_x, 0, 1 / scale_jitter, -translate_y),
            resample=Image.Resampling.BICUBIC, fillcolor=255,
        )
        image = image.rotate(random.uniform(-2.0, 2.0), resample=Image.Resampling.BICUBIC,
                             fillcolor=255)
    scale = min((size - 12) / image.width, (size - 12) / image.height)
    resized = image.resize((max(1, round(image.width * scale)),
                            max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    canvas = Image.new("L", (size, size), 255)
    canvas.paste(resized, ((size - resized.width) // 2, (size - resized.height) // 2))
    array = np.asarray(canvas, dtype=np.float32) / 255.0
    # Ink is positive; this is more stable than learning a large white bias.
    return torch.from_numpy((1.0 - array)[None, :, :])


@dataclass(frozen=True)
class CellPrediction:
    probabilities: dict[str, float]
    labels: tuple[str, ...]


class RosterResNetClassifier:
    def __init__(self, checkpoint: Path | str, *, device: str | None = None):
        torch, _ = _torch()
        selected = device or ("mps" if getattr(torch.backends, "mps", None) and
                              torch.backends.mps.is_available() else
                              "cuda" if torch.cuda.is_available() else "cpu")
        payload = torch.load(checkpoint, map_location=selected, weights_only=False)
        self.device = torch.device(selected)
        self.size = int(payload.get("image_size", 128))
        self.thresholds = payload.get("thresholds", {label: 0.5 for label in LABELS})
        self.model = build_resnet18(len(LABELS)).to(self.device)
        self.model.load_state_dict(payload["model_state"])
        self.model.eval()

    def predict(self, images: Iterable[Image.Image]) -> list[CellPrediction]:
        torch, _ = _torch()
        tensors = [prepare_image(image, self.size) for image in images]
        if not tensors:
            return []
        with torch.no_grad():
            probabilities = torch.sigmoid(self.model(torch.stack(tensors).to(self.device)))
        output = []
        for values in probabilities.cpu().tolist():
            scores = dict(zip(LABELS, map(float, values)))
            output.append(CellPrediction(
                probabilities=scores,
                labels=tuple(label for label in LABELS
                             if scores[label] >= float(self.thresholds[label])),
            ))
        return output


def save_metadata(path: Path, payload: dict) -> None:
    path.with_suffix(".json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
