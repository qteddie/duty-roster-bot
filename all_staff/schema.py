from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

HEAVENLY_STEMS = tuple("甲乙丙丁戊己庚辛壬癸")
EARTHLY_BRANCHES = tuple("子丑寅卯辰巳午未申酉")
CHINESE_CODES = HEAVENLY_STEMS + EARTHLY_BRANCHES
ENGLISH_CODES = tuple("ABCDEFGHIJKLMN")
FIXED_CODES = CHINESE_CODES + ENGLISH_CODES   # one person each, every day; numeric codes change daily
ALLOWED_CODES = tuple(map(str, range(1, 85))) + ENGLISH_CODES + CHINESE_CODES
ALLOWED_CODE_SET = frozenset(ALLOWED_CODES)
CHARSET = tuple("0123456789ABCDEFGHIJKLMN") + CHINESE_CODES


@dataclass(frozen=True)
class Box:
    x1: int
    y1: int
    x2: int
    y2: int

    def validate(self, width: int, height: int) -> None:
        if not (0 <= self.x1 < self.x2 <= width and 0 <= self.y1 < self.y2 <= height):
            raise ValueError(f"invalid box {self} for {width}x{height}")


@dataclass(frozen=True)
class Annotation:
    box: Box
    text: str
    role: str = "personnel_code"
    source: str = "human"
    confidence: float | None = None

    def validate(self, width: int, height: int) -> None:
        self.box.validate(width, height)
        if self.role not in {"personnel_code", "other", "unknown", "partial"}:
            raise ValueError(f"unknown annotation role: {self.role}")
        if self.role == "personnel_code" and self.text not in ALLOWED_CODE_SET:
            raise ValueError(f"unsupported personnel code: {self.text}")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class CellAnnotation:
    sample_id: str
    image: str
    source_group: str
    row: int
    column: int
    width: int
    height: int
    reviewed: bool
    annotations: tuple[Annotation, ...] = ()

    @property
    def split(self) -> str:
        bucket = int(hashlib.sha256(self.source_group.encode()).hexdigest()[:8], 16) % 100
        return "train" if bucket < 70 else "val" if bucket < 85 else "test"

    def validate(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("image dimensions must be positive")
        for item in self.annotations:
            item.validate(self.width, self.height)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["split"] = self.split
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "CellAnnotation":
        items = tuple(Annotation(Box(**item["box"]), item.get("text", ""),
                                 item.get("role", "personnel_code"),
                                 item.get("source", "human"), item.get("confidence"))
                      for item in payload.get("annotations", []))
        result = cls(
            sample_id=str(payload["sample_id"]), image=str(payload["image"]),
            source_group=str(payload["source_group"]), row=int(payload["row"]),
            column=int(payload["column"]), width=int(payload["width"]),
            height=int(payload["height"]), reviewed=bool(payload.get("reviewed", False)),
            annotations=items,
        )
        result.validate()
        return result


def load_annotations(path: Path, *, reviewed_only: bool = False) -> list[CellAnnotation]:
    if not path.is_file():
        return []
    output = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = CellAnnotation.from_dict(json.loads(line))
        except Exception as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
        if not reviewed_only or item.reviewed:
            output.append(item)
    return output


def save_annotations(path: Path, items: list[CellAnnotation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n"
                                 for item in sorted(items, key=lambda value: value.sample_id)),
                         encoding="utf-8")
    temporary.replace(path)


def repair_annotation_dimensions(dataset: Path) -> dict:
    """Reconcile manifest dimensions after a cell is re-cropped.

    Auto-label boxes use the current image pixels. Older manifests may retain
    dimensions from a previous grid registration for the same sample id.
    """
    path = dataset/"annotations.jsonl"
    if not path.is_file(): return {"updated": 0, "clamped_boxes": 0}
    updated = clamped = 0; payloads = []
    from PIL import Image
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        payload = json.loads(line)
        with Image.open(dataset/payload["image"]) as image:
            width, height = image.size
        if (payload.get("width"), payload.get("height")) != (width, height):
            payload["width"], payload["height"] = width, height; updated += 1
        repaired = []
        for item in payload.get("annotations", []):
            box = item["box"]
            box["x1"] = max(0, min(width-1, int(box["x1"])))
            box["y1"] = max(0, min(height-1, int(box["y1"])))
            old = (box["x2"], box["y2"])
            box["x2"] = max(box["x1"]+1, min(width, int(box["x2"])))
            box["y2"] = max(box["y1"]+1, min(height, int(box["y2"])))
            clamped += old != (box["x2"], box["y2"]); repaired.append(item)
        payload["annotations"] = repaired; payloads.append(payload)
    temporary = path.with_suffix(path.suffix+".repair.tmp")
    temporary.write_text("".join(json.dumps(item, ensure_ascii=False)+"\n" for item in payloads),
                         encoding="utf-8")
    temporary.replace(path)
    return {"updated": updated, "clamped_boxes": clamped}
