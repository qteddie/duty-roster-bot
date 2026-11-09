"""Read the printed roster date (``2026年10月1日星期四``) from the page header.

The perspective-corrected canvas starts at the table, and on many photos the
header line above it is cut off. So the header is re-warped from the original
photo with the same homography, extended upwards, and read with Tesseract
(``chi_tra``: the Docker image installs ``tesseract-ocr-chi-tra``). A reading is
accepted only when it is a valid date close to the upload day; otherwise the bot
says the date is unknown and asks for it with a date picker bound to that photo:
the administrators on their request card, or without administrators the
uploader of a group photo (a private photo's sender can type 「日期 月/日」).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

WINDOW = (-7, 21)          # accepted: 7 days before … 21 days after the upload
ABOVE, BELOW = .10, .06    # strip: 10 % of the table height above its top edge, 6 % into it
WEEKDAYS = "一二三四五六日"
# A reading takes about a second on its own, but runs beside the cell and name-table OCR of the same photo. With
# 10 s and Tesseract's own threads it ran out on a busy server and the photo came back 「日期未辨識」, silently.
TIMEOUT = 20


def header_strip(photo: Path, template) -> Image.Image | None:
    """The page header (right part) warped upright from the original photo."""
    import cv2
    from backend.phase1_cell_pipeline import _perspective_matrix, _registration_corners
    with Image.open(photo) as raw:
        image = ImageOps.exif_transpose(raw).convert("L")
    corners, _ = _registration_corners(image.convert("RGB"), template)
    if corners is None:
        return None
    matrix, (width, height) = _perspective_matrix(corners, template)
    pad = round(height * ABOVE)
    shift = np.array([[1, 0, 0], [0, 1, pad], [0, 0, 1]], dtype=np.float64)
    strip = cv2.warpPerspective(np.asarray(image), shift @ matrix, (width, pad + round(height * BELOW)),
                                flags=cv2.INTER_CUBIC, borderValue=255)
    left = round(width * .45)
    return Image.fromarray(strip[:, left:round(width * .97)])


def _ocr(image: Image.Image, language: str, psm: int) -> str:
    buffer = BytesIO(); image.save(buffer, format="PNG")
    env = dict(os.environ, OMP_THREAD_LIMIT="1")   # beside the other OCR: one thread, or they thrash (name_table._env)
    completed = subprocess.run(["tesseract", "stdin", "stdout", "-l", language, "--psm", str(psm)],
                               input=buffer.getvalue(), capture_output=True, timeout=TIMEOUT, check=False, env=env)
    return completed.stdout.decode("utf-8", "replace") if completed.returncode == 0 else ""


def parse(text: str, low: date | None = None, high: date | None = None) -> list[tuple[date, bool]]:
    """(date, weekday-verified) for text like 2026年10月1日（星期四）; a weekday that was read must agree.

    With a window (``low``..``high``) a partial reading such as ``0月15日星期二`` (9 read
    as 0) still counts when exactly one day in the window has that day of month and weekday.
    """
    found = []
    compact = re.sub(r"\s+", "", text)
    for match in re.finditer(r"(20\d{2})年(\d{1,2})月(\d{1,2})日(?:星期([一二三四五六日天]))?", compact):
        try:
            day = date(*(int(match.group(i)) for i in (1, 2, 3)))
        except ValueError:
            continue
        weekday = (match.group(4) or "").replace("天", "日")
        if weekday and WEEKDAYS[day.weekday()] != weekday:
            continue
        found.append((day, bool(weekday)))
    if found or low is None or high is None:
        return found
    for match in re.finditer(r"(\d{1,2})日[星里][期旬基散朗]?([一二三四五六日天])", compact):
        number, weekday = int(match.group(1)), match.group(2).replace("天", "日")
        options = [low + timedelta(days=k) for k in range((high - low).days + 1)]
        options = [d for d in options if d.day == number and WEEKDAYS[d.weekday()] == weekday]
        if len(options) == 1:
            found.append((options[0], True))
    return found


def read_header_date(photo: Path, template, uploaded: date) -> date | None:
    """The roster's printed date, or None when nothing plausible was read."""
    if not shutil.which("tesseract"):
        return None
    try:
        strip = header_strip(photo, template)
    except Exception:
        return None
    if strip is None:
        return None
    scale = max(1.0, 2400 / strip.width)
    strip = ImageOps.autocontrast(strip.resize((round(strip.width * scale), round(strip.height * scale)),
                                               Image.LANCZOS))
    low, high = uploaded + timedelta(days=WINDOW[0]), uploaded + timedelta(days=WINDOW[1])
    readings: list[tuple[date, bool]] = []
    for psm in (6, 11):
        try:
            text = _ocr(strip, "chi_tra", psm)
        except subprocess.TimeoutExpired:   # the other layout may still read it
            print(f"讀取班表日期逾時（psm {psm}，{TIMEOUT} 秒）：{photo.name}", flush=True)
            continue
        except (OSError, subprocess.SubprocessError):
            return None
        readings += [(d, ok) for d, ok in parse(text, low, high) if low <= d <= high]
    # a reading whose weekday matches wins; an unverified one only when every reading agrees
    verified = [d for d, ok in readings if ok]
    if verified:
        return max(set(verified), key=verified.count)
    if readings and len({d for d, _ in readings}) == 1 and len(readings) > 1:
        return readings[0][0]
    return None
