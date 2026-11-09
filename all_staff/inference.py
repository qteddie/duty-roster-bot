from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .dataset import image_tensor
from .models import build_detector, build_recognizer, ctc_greedy_decode
from .schema import ALLOWED_CODE_SET, Box, CHARSET


@dataclass(frozen=True)
class Prediction:
    box: Box
    code: str
    name: str | None
    confidence: float
    needs_review: bool


class AllStaffOCR:
    def __init__(self, detector: Path, recognizer: Path, roster: Path, *, device=None):
        import torch
        selected = device or ("mps" if getattr(torch.backends, "mps", None) and
                              torch.backends.mps.is_available() else
                              "cuda" if torch.cuda.is_available() else "cpu")
        self.torch, self.device = torch, torch.device(selected)
        det = torch.load(detector, map_location=self.device, weights_only=False)
        rec = torch.load(recognizer, map_location=self.device, weights_only=False)
        if det.get("kind") != "all_staff_detector_v1" or rec.get("kind") != "all_staff_recognizer_v1":
            raise ValueError("checkpoint 不是 all-staff V2 模型")
        self.detector_size = tuple(det["input_size"])
        self.recognizer_size = tuple(rec["input_size"])
        self.threshold, self.min_area = float(det["threshold"]), int(det["min_area"])
        self.charset = tuple(rec.get("charset", CHARSET))
        self.detector = build_detector().to(self.device)
        self.recognizer = build_recognizer(self.charset).to(self.device)
        self.detector.load_state_dict(det["model_state"]); self.detector.eval()
        self.recognizer.load_state_dict(rec["model_state"]); self.recognizer.eval()
        self.roster = json.loads(roster.read_text(encoding="utf-8")) if roster.is_file() else {}

    def detect(self, image: Image.Image) -> list[Box]:
        with self.torch.no_grad():
            mask = self.detector(image_tensor(image, self.detector_size).unsqueeze(0).to(self.device))
            mask = (mask.sigmoid()[0, 0].cpu().numpy() >= self.threshold).astype(np.uint8)
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        sx, sy = image.width/self.detector_size[0], image.height/self.detector_size[1]
        boxes = []
        for x, y, width, height, area in stats[1:count]:
            if area < self.min_area: continue
            pad_x, pad_y = max(1, round(width*.08)), max(1, round(height*.08))
            boxes.append(Box(max(0, round((x-pad_x)*sx)), max(0, round((y-pad_y)*sy)),
                             min(image.width, round((x+width+pad_x)*sx)),
                             min(image.height, round((y+height+pad_y)*sy))))
        return sorted(boxes, key=lambda box: (box.y1, box.x1))

    def predict_cell(self, image: Image.Image, *, minimum_confidence=.75) -> list[Prediction]:
        boxes = self.detect(image)
        if not boxes: return []
        tensors = [image_tensor(image.crop((box.x1, box.y1, box.x2, box.y2)), self.recognizer_size)
                   for box in boxes]
        with self.torch.no_grad():
            decoded = ctc_greedy_decode(self.recognizer(self.torch.stack(tensors).to(self.device)).cpu(),
                                        self.charset)
        return [Prediction(box, code, self.roster.get(code), confidence,
                           confidence < minimum_confidence or code not in ALLOWED_CODE_SET or
                           code not in self.roster)
                for box, (code, confidence) in zip(boxes, decoded)]

    def analyze_cells(self, cells: Path) -> list[dict]:
        output = []
        for path in sorted(cells.glob("*.png")):
            with Image.open(path) as image:
                predictions = self.predict_cell(image.copy())
            output.append({"cell": path.name, "predictions": [asdict(item) for item in predictions]})
        return output


def is_line_checkpoint(path: Path) -> bool:
    import torch
    return Path(path).is_file() and torch.load(path, map_location="cpu", weights_only=False).get(
        "kind") == "all_staff_line_recognizer_v2"


def load_engine(recognizer: Path, *, detector: Path | None = None, roster: Path | None = None,
                device: str | None = None, context: dict | None = None):
    """Pick the engine from the recogniser checkpoint kind.

    ``all_staff_line_recognizer_v2`` → segmenter + line recogniser (no detector);
    ``all_staff_recognizer_v1`` → legacy U-Net detector + crop recogniser.
    """
    import torch
    kind = torch.load(recognizer, map_location="cpu", weights_only=False).get("kind")
    if kind == "all_staff_line_recognizer_v2":
        from .lines import LineOCR
        return LineOCR(recognizer, roster, device=device or "cpu", context=context)
    if detector is None:
        raise ValueError("V1 recognizer 需要 --detector")
    return AllStaffOCR(detector, recognizer, roster or Path("configs/all_staff_roster.local.json"),
                       device=device)
