"""Synthetic training lines for the printed roster.

The roster is typeset with a Times-like serif for digits/letters and a Ming
(明體) face for 干支. Rendering those codes on real blank-cell backgrounds and
degrading them like a phone photo (tiny glyphs, blur, JPEG, then the same 5×
LANCZOS upscale as ``prepare``) gives balanced data for every allowed code,
including codes that have never been labelled.

Fonts are discovered on Windows (also via WSL ``/mnt/c``), macOS and Linux.
Override with ``ALL_STAFF_LATIN_FONTS`` / ``ALL_STAFF_CJK_FONTS``
(os.pathsep-separated paths).
"""
from __future__ import annotations

import io
import os
import random
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .schema import ALLOWED_CODES, CHINESE_CODES

LATIN_CANDIDATES = (
    "C:/Windows/Fonts/times.ttf", "/mnt/c/Windows/Fonts/times.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/Library/Fonts/Times New Roman.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/System/Library/Fonts/Times.ttc",
)
CJK_CANDIDATES = (
    "C:/Windows/Fonts/mingliu.ttc", "/mnt/c/Windows/Fonts/mingliu.ttc",  # 新細明體
    "C:/Windows/Fonts/kaiu.ttf", "/mnt/c/Windows/Fonts/kaiu.ttf",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Light.ttc",
    "/usr/share/fonts/noto-cjk/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)
NEGATIVE_TEXT = ("備", "—", "-", "·", "丶", "／")
PAIRABLE = tuple("ABCDEFGHIJKLMN") + CHINESE_CODES
NUMBERS = tuple(code for code in ALLOWED_CODES if code.isdigit())


def _paths(variable: str, candidates: tuple[str, ...]) -> list[str]:
    override = [p for p in os.environ.get(variable, "").split(os.pathsep) if p]
    return [p for p in (override or candidates) if Path(p).is_file()]


@lru_cache(maxsize=None)
def font_paths() -> tuple[list[str], list[str]]:
    latin, cjk = _paths("ALL_STAFF_LATIN_FONTS", LATIN_CANDIDATES), _paths("ALL_STAFF_CJK_FONTS", CJK_CANDIDATES)
    if not latin or not cjk:
        raise RuntimeError("找不到合成用字型：請安裝 Times/Liberation Serif 與明體/Noto Serif CJK，"
                           "或設定 ALL_STAFF_LATIN_FONTS／ALL_STAFF_CJK_FONTS")
    return latin, cjk


@lru_cache(maxsize=256)
def _font(path: str, size: int, index: int = 0):
    try:
        return ImageFont.truetype(path, size, index=index)
    except OSError:
        return ImageFont.truetype(path, size)


def _pick_font(rng: random.Random, cjk: bool, size: int):
    latin, chinese = font_paths()
    path = rng.choice(chinese if cjk else latin)
    # Noto CJK collections: index 2 is TC; mingliu.ttc index 0 is 細明體
    index = 2 if "NotoSerifCJK" in path else 0
    return _font(path, size, index)


def random_line(rng: random.Random) -> tuple[str, str]:
    """(rendered text, CTC label). Mirrors what one roster text line contains."""
    roll = rng.random()
    if roll < .07:
        return rng.choice(NEGATIVE_TEXT), ""
    if roll < .25:
        count = 2 if rng.random() < .85 else 3
        codes = [rng.choice(PAIRABLE) for _ in range(count)]
        return "".join(codes), "".join(codes)
    code = rng.choice(ALLOWED_CODES)
    if code.isdigit() and rng.random() < .15:
        return "#" + code, code
    return code, code


def render(text: str, rng: random.Random, backgrounds: list[np.ndarray]) -> Image.Image:
    """Render one line as it looks after perspective crop + 5× upscale."""
    scale = 4  # supersampling before the photo-resolution downscale
    glyph = rng.uniform(8.5, 13.5)  # glyph height in original photo pixels
    parts = []  # (text, font) runs so Latin and CJK use their own faces
    for char in text:
        cjk = not char.isascii()
        size = int(glyph * scale * (1.05 if cjk else 1.38))
        parts.append((char, _pick_font(rng, cjk, size) if not parts or parts[-1][2] != cjk else parts[-1][1], cjk))
    tracking = rng.uniform(-.02, .12) * glyph * scale
    boxes = [font.getbbox(char) for char, font, _ in parts]
    widths = [b[2] - b[0] for b in boxes]
    width = int(sum(widths) + tracking * (len(parts) - 1))
    top = min(b[1] for b in boxes); bottom = max(b[3] for b in boxes)
    height = bottom - top
    pad_x = int(height * rng.uniform(.08, .3)); pad_y = int(height * rng.uniform(.08, .25))
    canvas_w, canvas_h = width + 2 * pad_x, height + 2 * pad_y
    background = rng.choice(backgrounds)
    paper = float(np.median(background)) + rng.uniform(-12, 8)
    gradient = np.linspace(0, rng.uniform(-10, 10), canvas_w)[None, :]
    noise = np.random.default_rng(rng.getrandbits(32)).normal(0, rng.uniform(1, 4), (canvas_h, canvas_w))
    patch = Image.fromarray(np.clip(paper + gradient + noise, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(patch)
    ink = rng.randint(0, 50)
    stroke = rng.choice((0, 1, 1, 2, 2))
    cursor = pad_x
    for (char, font, cjk), box, w in zip(parts, boxes, widths):
        draw.text((cursor - box[0], pad_y - top + rng.uniform(-.04, .04) * height), char, fill=ink, font=font,
                  stroke_width=min(stroke, 1) if cjk else stroke, stroke_fill=ink)
        cursor += w + tracking
    if rng.random() < .4:  # rule remnants at the crop edge
        t = int(rng.uniform(.5, 1.8) * scale); shade = rng.randint(0, 60)
        side = rng.choice("lrtb")
        box = {"l": (0, 0, t, canvas_h), "r": (canvas_w - t, 0, canvas_w, canvas_h),
               "t": (0, 0, canvas_w, t), "b": (0, canvas_h - t, canvas_w, canvas_h)}[side]
        draw.rectangle(box, fill=shade)
    if rng.random() < .15:  # sliver of a neighbour glyph
        fx = rng.choice((0, canvas_w - int(height * .15)))
        draw.rectangle((fx, pad_y, fx + int(height * rng.uniform(.04, .12)), pad_y + height), fill=ink)
    patch = patch.rotate(rng.uniform(-2.5, 2.5), resample=Image.Resampling.BILINEAR,
                         fillcolor=int(paper))
    small = patch.resize((max(3, round(canvas_w / scale)), max(3, round(canvas_h / scale))),
                         Image.Resampling.BOX)
    if rng.random() < .5:
        small = small.filter(ImageFilter.GaussianBlur(rng.uniform(.2, .7)))
    if rng.random() < .7:
        buffer = io.BytesIO(); small.save(buffer, "JPEG", quality=rng.randint(40, 90))
        small = Image.open(buffer).convert("L")
    return small.resize((small.width * 5, small.height * 5), Image.Resampling.LANCZOS)


def blank_backgrounds(dataset: Path, limit=400) -> list[np.ndarray]:
    """Crops of reviewed blank cells (train split) used as paper texture."""
    from .context import core_box, load_context
    from .schema import load_annotations
    context = load_context(dataset)
    output = []
    for item in load_annotations(dataset / "annotations.jsonl", reviewed_only=True):
        if item.annotations or item.split != "train":
            continue
        with Image.open(dataset / item.image) as image:
            x1, y1, x2, y2 = core_box(image.width, image.height, context)
            inner = image.convert("L").crop((x1 + 12, y1 + 12, x2 - 12, y2 - 12))
        output.append(np.asarray(inner))
        if len(output) >= limit:
            break
    if not output:
        output.append(np.full((120, 120), 205, np.uint8))
    return output
