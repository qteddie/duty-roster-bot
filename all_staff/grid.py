"""V2 grid registration: template-guided line tracking + per-cell perspective crops.

The V1 registration detects rules globally and maps them to columns by *index*,
so one missing or extra rule shifts every following column (9/27 v2: c10–c11
and c30–c32), and cells are cut as axis-aligned rectangles, so a slanted or
curled sheet leaves text straddling the crop edge.

Here every template rule keeps its identity:

1. the photo is perspective-corrected with the existing V1 step;
2. long vertical / horizontal rules are isolated with morphology;
3. in narrow bands the whole template comb (with its irregular column widths,
   which makes the fit unambiguous) is aligned to the rule profile by a local
   scale + offset;
4. each rule is snapped to the nearest real rule inside that band and fitted as
   a smooth curve ``x(y)`` / ``y(x)`` (quadratic, outlier-trimmed);
5. every cell is the quad between its four curves, expanded by the context
   margin and warped to an upright rectangle.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

SCALE = 5  # same 5× upscale as the V1 crops the recogniser was trained on


@dataclass
class Registration:
    image: Image.Image           # perspective-corrected canvas
    x_curves: list[np.ndarray]   # per template column rule: poly coeffs of x(y)
    y_curves: list[np.ndarray]   # per template row rule: poly coeffs of y(x)
    x_support: list[int]         # snapped observations per rule
    y_support: list[int]
    method: str

    def x_at(self, index: int, y: float) -> float:
        return float(np.polyval(self.x_curves[index], y))

    def y_at(self, index: int, x: float) -> float:
        return float(np.polyval(self.y_curves[index], x))

    def corner(self, column_rule: int, row_rule: int) -> tuple[float, float]:
        y = self.y_at(row_rule, self.x_at(column_rule, 0.0))
        for _ in range(4):
            x = self.x_at(column_rule, y); y = self.y_at(row_rule, x)
        return x, y

    def quad(self, row: int, column: int) -> np.ndarray:
        return np.array([self.corner(column, row), self.corner(column + 1, row),
                         self.corner(column + 1, row + 1), self.corner(column, row + 1)],
                        dtype=np.float64)


def rule_masks(gray: np.ndarray, pitch_x: float, pitch_y: float) -> tuple[np.ndarray, np.ndarray]:
    ink = cv2.adaptiveThreshold(gray, 1, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 10)
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(15, int(pitch_y * .7)))))
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(15, int(pitch_x * .75)), 1)))
    return vertical.astype(np.float32), horizontal.astype(np.float32)


def _smooth(profile: np.ndarray, sigma=1.2) -> np.ndarray:
    radius = int(3 * sigma) + 1
    kernel = np.exp(-.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    return np.convolve(profile, kernel / kernel.sum(), mode="same")


def _snap(profile: np.ndarray, predicted: np.ndarray, window: np.ndarray, minimum: float) -> list[float | None]:
    output = []
    for x, w in zip(predicted, window):
        a, b = int(max(0, np.floor(x - w))), int(min(len(profile) - 1, np.ceil(x + w)))
        if b <= a:
            output.append(None); continue
        k = a + int(np.argmax(profile[a:b + 1]))
        if profile[k] < minimum:
            output.append(None); continue
        # sub-pixel peak (parabola through the maximum and its neighbours)
        if 0 < k < len(profile) - 1:
            l, c, r = profile[k - 1], profile[k], profile[k + 1]
            denom = l - 2 * c + r
            k = k + (.5 * (l - r) / denom if denom else 0)
        output.append(float(k))
    return output


def _robust_fit(t: list[float], v: list[float], degree: int, tolerance: float) -> np.ndarray | None:
    t, v = np.asarray(t, float), np.asarray(v, float)
    keep = np.ones(len(t), bool)
    for _ in range(3):
        if keep.sum() < degree + 2:
            return None
        coeffs = np.polyfit(t[keep], v[keep], degree)
        residual = np.abs(np.polyval(coeffs, t) - v)
        new_keep = residual <= max(tolerance, 2.5 * np.median(residual[keep]))
        if (new_keep == keep).all():
            break
        keep = new_keep
    return np.polyfit(t[keep], v[keep], degree) if keep.sum() >= degree + 2 else None


def _peaks(profile: np.ndarray, minimum: float) -> list[tuple[float, float, float]]:
    """(position, height, width) of local maxima above ``minimum``."""
    output = []
    above = profile >= minimum
    k = 0
    while k < len(profile):
        if not above[k]:
            k += 1; continue
        start = k
        while k < len(profile) and above[k]:
            k += 1
        segment = profile[start:k]
        top = start + int(np.argmax(segment))
        output.append((float(top), float(segment.max()), float(k - start)))
    return output


def _walk(peaks: list[float], start_index: int, start_pos: float, gaps: list[float],
          direction: int, count: int) -> tuple[dict[int, float], dict[int, bool]]:
    """From one anchored rule, step to neighbouring rules one expected gap at a time,
    snapping to the nearest real peak (±35 % of the gap) or keeping the prediction."""
    positions, observed = {start_index: start_pos}, {start_index: True}
    index, pos = start_index, start_pos
    while 0 <= index + direction < count:
        gap = gaps[min(index, index + direction)]
        predicted = pos + direction * gap
        near = [p for p in peaks if abs(p - predicted) <= .35 * gap]
        index += direction
        if near:
            pos = min(near, key=lambda p: abs(p - predicted)); observed[index] = True
        else:
            pos = predicted; observed[index] = False
        positions[index] = pos
    return positions, observed


def _anchor_columns(profile: np.ndarray, expected: np.ndarray, first_rule: int,
                    reliable_until: int, pitch: float) -> tuple[np.ndarray, str]:
    """Rule positions in one band, identified by physical order.

    Anchor: the first duty rule (left edge of c02) is the right-most rule near
    its template position that has no other rule within 1.3 pitches to its
    left — i.e. it follows the wide time column (or the photo edge when the
    time column is cut off). From there every column is one physical cell.
    Template spacing is used up to ``reliable_until``; beyond it (the V1
    calibration merged a bold rule there) columns are taken as regular.
    """
    peaks = [p for p, _, _ in _peaks(profile, .3)]
    target = float(expected[first_rule])
    window = [(k, p) for k, p in enumerate(peaks) if target - 2.5 * pitch <= p <= target + 1.8 * pitch]
    # 1st choice: the rule right after the ~2-pitch-wide time column (left-most such gap)
    after_time = [p for k, p in window if k > 0 and 1.5 * pitch <= p - peaks[k - 1] <= 2.6 * pitch]
    # 2nd choice: the time column is cut off by the photo edge: first rule of the band
    first = [p for k, p in window if k == 0 and p <= target + .7 * pitch]
    if after_time:
        start = min(after_time)
    elif first:
        start = first[0]
    else:
        raise ValueError("找不到時段欄右側的第一條勤務格線")
    left_rules = [p for p in peaks if p < start]
    border = left_rules[-1] if left_rules and start - left_rules[-1] <= 2.7 * pitch else None
    gaps = [pitch] * (len(expected) - 1)
    for k in range(first_rule, reliable_until):
        gaps[k] = float(expected[k + 1] - expected[k])
    positions = {first_rule: start}
    index, pos = first_rule, start
    while index + 1 < len(expected):
        gap = gaps[index]; predicted = pos + gap; index += 1
        near = [p for p in peaks if abs(p - predicted) <= .35 * gap]
        pos = min(near, key=lambda p: abs(p - predicted)) if near else predicted
        positions[index] = pos
    width = (expected[first_rule] - expected[0])
    border = border if border is not None else start - width
    for k in range(0, first_rule):
        positions[k] = border + (start - border) * (expected[k] - expected[0]) / width
    return np.array([positions[i] for i in range(len(expected))], float), "time_column"


def _anchor_rows(profile: np.ndarray, expected: np.ndarray, first_row: int,
                 pitch: float) -> tuple[np.ndarray, str]:
    """Row rules anchored at the narrow 代號 row just above the first hour."""
    peaks = [p for p, _, _ in _peaks(profile, .3)]
    target = float(expected[first_row])
    header_gap = expected[first_row] - expected[first_row - 1]  # 代號 row height
    candidates = []
    for a, b in zip(peaks, peaks[1:]):
        if .35 * pitch <= b - a <= .85 * pitch and abs(b - target) <= 2.5 * pitch:
            above = [p for p in peaks if p < a]
            # the header above the 代號 row is tall and has no horizontal rules
            if not above or a - above[-1] >= 1.5 * pitch:
                candidates.append((abs(b - target), b))
    anchor = min(candidates)[1] if candidates else min(peaks, key=lambda p: abs(p - target))
    gaps = list(np.diff(expected))
    down, _ = _walk(peaks, first_row, anchor, gaps, +1, len(expected))
    up, _ = _walk(peaks, first_row, anchor, gaps, -1, len(expected))
    result = np.array([up.get(i, down.get(i)) for i in range(len(expected))], float)
    return result, "code_row" if candidates else "nearest"


def _track(mask: np.ndarray, initial: np.ndarray, *, axis: str, band_ranges: list[tuple[int, int]],
           anchor_band: int, active: tuple[int, int]) -> tuple[list[np.ndarray], list[int]]:
    """Follow every rule from the anchor band outwards; fit a smooth curve per rule."""
    lo, hi = active
    positions0 = initial[lo:hi + 1]
    gaps = np.diff(positions0)
    pitch = float(np.median(gaps))
    window = .3 * np.minimum(np.r_[gaps[0], gaps], np.r_[gaps, gaps[-1]])
    window = np.maximum(window, 1.5)
    profiles = [_smooth((mask[a:b, :] if axis == "x" else mask[:, a:b]).mean(0 if axis == "x" else 1))
                for a, b in band_ranges]
    observations: list[list[tuple[float, float]]] = [[] for _ in positions0]
    placed: dict[int, np.ndarray] = {}
    order = sorted(range(len(band_ranges)), key=lambda k: abs(k - anchor_band))
    grid = None
    for k in order:
        if placed:
            reference = placed[min(placed, key=lambda j: abs(j - k))]
            shifts = np.arange(-.35 * pitch, .35 * pitch + .5, 1.0)
            idx = np.arange(len(profiles[k]))
            scores = [np.interp(np.clip(reference + d, 0, len(idx) - 1), idx, profiles[k]).sum()
                      - .3 * abs(d) / pitch for d in shifts]
            predicted = reference + shifts[int(np.argmax(scores))]
        else:
            predicted = positions0
        snapped = _snap(profiles[k], predicted, window, minimum=.3)
        current = np.array([v if v is not None else p for v, p in zip(snapped, predicted)])
        placed[k] = current
        a, b = band_ranges[k]
        for index, value in enumerate(snapped):
            if value is not None:
                observations[index].append(((a + b) / 2, value))
    fits: list[np.ndarray | None] = []
    for points in observations:
        coeffs = None
        if len(points) >= 4:
            degree = 2 if len(points) >= 8 else 1
            coeffs = _robust_fit([p[0] for p in points], [p[1] for p in points], degree, 2.5)
            if coeffs is not None and len(coeffs) == 2:
                coeffs = np.r_[0.0, coeffs]
        fits.append(coeffs)
    span = [min(a for a, _ in band_ranges), max(b for _, b in band_ranges)]
    samples = np.linspace(span[0], span[1], 7)
    for _ in range(2):  # fitted rules must stay ordered and apart
        for index in range(len(fits)):
            if fits[index] is None:
                continue
            here = np.polyval(fits[index], samples)
            for other in (index - 1, index + 1):
                if 0 <= other < len(fits) and fits[other] is not None:
                    gap = (np.polyval(fits[other], samples) - here) * np.sign(other - index)
                    if (gap < .45 * abs(positions0[other] - positions0[index])).any():
                        weaker = index if len(observations[index]) <= len(observations[other]) else other
                        fits[weaker] = None
    good = [i for i, f in enumerate(fits) if f is not None]
    if len(good) < 2:
        raise ValueError("找不到足夠的格線")
    curves = []
    for index, coeffs in enumerate(fits):
        if coeffs is None:
            left = max((g for g in good if g < index), default=None)
            right = min((g for g in good if g > index), default=None)
            if left is None or right is None:  # extrapolate with the neighbouring spacing
                a, b = (good[0], good[1]) if left is None else (good[-2], good[-1])
                ratio = (positions0[index] - positions0[a]) / (positions0[b] - positions0[a])
                coeffs = fits[a] + ratio * (fits[b] - fits[a])
            else:
                ratio = (positions0[index] - positions0[left]) / (positions0[right] - positions0[left])
                coeffs = fits[left] + ratio * (fits[right] - fits[left])
        curves.append(coeffs)
    full: list[np.ndarray] = []
    for index in range(len(initial)):
        if lo <= index <= hi:
            full.append(curves[index - lo])
        else:
            anchor = lo if index < lo else hi
            full.append(curves[anchor - lo] + np.r_[0.0, 0.0, initial[index] - initial[anchor]])
    support = [len(observations[i - lo]) if lo <= i <= hi else 0 for i in range(len(initial))]
    return full, support


def register(corrected: Image.Image, grid_x: list[float], grid_y: list[float], *,
             rows: tuple[int, int], columns: tuple[int, int], reliable_until: int = 27) -> Registration:
    """Register rules on a perspective-corrected canvas (template gives rough positions)."""
    gray = np.asarray(corrected.convert("L"))
    height, width = gray.shape
    ex = np.asarray(grid_x, float) * width; ey = np.asarray(grid_y, float) * height
    first_row, last_row = rows; first_col, last_col = columns
    pitch_x = float(np.median(np.diff(ex[first_col:reliable_until])))
    # Only the template's rules up to ``reliable_until`` are trusted; the right-hand ones were hand-made
    # (a merged bold rule, then a few very wide intervals) and are never used. Every rule beyond is placed
    # at the regular pitch, so the table may extend past the template's last rule (C33–C36 do).
    ex = np.r_[ex[:reliable_until + 1],
               ex[reliable_until] + pitch_x * np.arange(1, max(len(ex), last_col + 2) - reliable_until)]
    pitch_y = float(np.median(np.diff(ey[first_row:last_row + 2])))
    vertical, horizontal = rule_masks(gray, pitch_x, pitch_y)
    y_bands = [(int(ey[r] + pitch_y * .15), int(ey[r + 1] - pitch_y * .15))
               for r in range(first_row, last_row + 1)]
    # identify columns in a few central bands and keep the most common answer
    middle = len(y_bands) // 2
    trials = []
    for k in (middle, middle - 3, middle + 3, middle - 6, middle + 6):
        a, b = y_bands[k]
        try:
            trials.append((k, _anchor_columns(_smooth(vertical[a:b].mean(0)), ex, first_col, reliable_until, pitch_x)[0]))
        except ValueError:
            continue
    if not trials:
        raise ValueError("找不到時段欄右側的第一條勤務格線，無法確認欄位")
    reference = np.median([t[1] for t in trials], axis=0)
    anchor_k, x_initial = min(trials, key=lambda t: np.abs(t[1] - reference).max())
    x_active = (max(0, first_col - 2), min(len(ex) - 1, last_col + 1))
    x_curves, x_support = _track(vertical, x_initial, axis="x", band_ranges=y_bands,
                                 anchor_band=anchor_k, active=x_active)
    mid = float(np.mean(ey[first_row:last_row + 2]))
    x_mid = np.array([np.polyval(c, mid) for c in x_curves])
    x_bands = [(int(x_mid[c] + pitch_x * .1), int(x_mid[min(c + 2, last_col + 1)] - pitch_x * .1))
               for c in range(first_col, last_col + 1, 2)]
    middle = len(x_bands) // 2
    trials = []
    for k in (middle, middle - 2, middle + 2, middle - 4, middle + 4):
        a, b = x_bands[k]
        trials.append((k, _anchor_rows(_smooth(horizontal[:, a:b].mean(1)), ey, first_row, pitch_y)[0]))
    reference = np.median([t[1] for t in trials], axis=0)
    anchor_k, y_initial = min(trials, key=lambda t: np.abs(t[1][first_row:last_row + 2] -
                                                          reference[first_row:last_row + 2]).max())
    y_active = (max(0, first_row - 1), min(len(ey) - 1, last_row + 1))
    y_curves, y_support = _track(horizontal, y_initial, axis="y", band_ranges=x_bands,
                                 anchor_band=anchor_k, active=y_active)
    return Registration(corrected, x_curves, y_curves, x_support, y_support, "v2_tracked")


def context_crop(reg: Registration, row: int, column: int, fx: float, fy: float,
                 scale: int = SCALE) -> tuple[Image.Image, np.ndarray]:
    """Upright context crop of one cell (core quad + margins), and its core quad."""
    tl, tr, br, bl = reg.quad(row, column)
    def point(u, v):
        return (1 - u) * (1 - v) * tl + u * (1 - v) * tr + u * v * br + (1 - u) * v * bl
    width = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    height = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    out_w = max(8, int(round(width * (1 + 2 * fx) * scale)))
    out_h = max(8, int(round(height * (1 + 2 * fy) * scale)))
    source = np.float32([point(-fx, -fy), point(1 + fx, -fy), point(1 + fx, 1 + fy), point(-fx, 1 + fy)])
    target = np.float32([[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]])
    matrix = cv2.getPerspectiveTransform(source, target)
    rgb = np.asarray(reg.image.convert("RGB"))
    warped = cv2.warpPerspective(rgb, matrix, (out_w, out_h), flags=cv2.INTER_LANCZOS4,
                                 borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(warped), np.array([tl, tr, br, bl])


def draw_debug(reg: Registration, rows: tuple[int, int], columns: tuple[int, int],
               overlays=None) -> Image.Image:
    from PIL import ImageDraw
    image = reg.image.convert("RGB").copy(); draw = ImageDraw.Draw(image)
    height, width = image.height, image.width
    top = reg.y_at(rows[0], width / 2) - 10; bottom = reg.y_at(rows[1] + 1, width / 2) + 10
    left = reg.x_at(columns[0], height / 2) - 10; right = reg.x_at(columns[1] + 1, height / 2) + 10
    for index, coeffs in enumerate(reg.x_curves):
        ys = np.linspace(top, bottom, 40)
        if not columns[0] <= index <= columns[1] + 1:
            continue
        colour = (37, 99, 235) if reg.x_support[index] >= 4 else (220, 38, 38)
        draw.line([(float(np.polyval(coeffs, y)), float(y)) for y in ys], fill=colour, width=2)
    for index, coeffs in enumerate(reg.y_curves):
        xs = np.linspace(left, right, 40)
        if not rows[0] <= index <= rows[1] + 1:
            continue
        colour = (37, 99, 235) if reg.y_support[index] >= 4 else (220, 38, 38)
        draw.line([(float(x), float(np.polyval(coeffs, x))) for x in xs], fill=colour, width=2)
    if overlays is not None:   # boxes laid over the table: red; the cells their pointer touches: orange
        for row, column in overlays.pointer:
            draw.line([*map(tuple, reg.quad(row, column)), tuple(reg.quad(row, column)[0])],
                      fill=(234, 128, 0), width=3)
        for box in overlays.boxes:
            (r0, r1), (c0, c1) = box.rows, box.columns
            points = [reg.corner(c0, r0), reg.corner(c1 + 1, r0), reg.corner(c1 + 1, r1 + 1), reg.corner(c0, r1 + 1)]
            draw.line([*points, points[0]], fill=(220, 38, 38), width=4)
    return image


def ingest_tracked(schedules: Path, dataset: Path, template_path: Path, *, fx: float, fy: float,
                   only_sources: list[Path] | None = None) -> dict:
    """Write context crops for every photo with the tracked V2 registration.

    Output layout matches the V1 ingest (``images/``, ``labels.csv``,
    ``debug/<group>.jpg``) so annotation tooling works unchanged. Rows of photos
    that are not re-processed are kept.
    """
    import csv
    import hashlib
    import roster_resnet
    from backend.phase1_cell_pipeline import GridTemplate, correct_perspective
    from . import overlays
    from .dataset import FULL_DUTY_COLUMNS, FULL_TIME_ROWS
    template = GridTemplate.from_json(template_path)
    rows = (min(FULL_TIME_ROWS), max(FULL_TIME_ROWS))
    columns = (min(FULL_DUTY_COLUMNS), max(FULL_DUTY_COLUMNS))
    images_dir, debug_dir = dataset / "images", dataset / "debug"
    images_dir.mkdir(parents=True, exist_ok=True); debug_dir.mkdir(parents=True, exist_ok=True)
    manifest = dataset / "labels.csv"
    existing = []
    if manifest.is_file():
        with manifest.open(newline="", encoding="utf-8") as handle:
            existing = list(csv.DictReader(handle))
    extensions = {".jpg", ".jpeg", ".png", ".webp"}
    sources = (sorted(Path(p).resolve() for p in only_sources) if only_sources is not None else
               sorted(p for p in schedules.rglob("*") if p.is_file() and p.suffix.lower() in extensions))
    output_rows, processed, failures, photos, seen = [], set(), [], [], set()
    for source in sources:
        group = hashlib.sha256(source.read_bytes()).hexdigest()
        if group in seen or group[:16] in roster_resnet.EXCLUDED_SOURCE_GROUPS:
            continue
        seen.add(group)
        try:
            corrected, method = correct_perspective(source, template)
            registration = register(corrected, template.grid_x, template.grid_y, rows=rows, columns=columns)
        except Exception as exc:
            failures.append({"file": str(source), "error": str(exc)}); continue
        processed.add(group)
        duty_x = registration.x_support[columns[0]:columns[1] + 2]
        duty_y = registration.y_support[rows[0]:rows[1] + 2]
        photos.append({"file": source.name, "group": group[:16], "perspective": method,
                       "interpolated_column_rules": [columns[0] + i for i, n in enumerate(duty_x) if n < 4],
                       "interpolated_row_rules": [rows[0] + i for i, n in enumerate(duty_y) if n < 4]})
        try:
            found = overlays.detect(registration, rows=rows, columns=columns)
        except Exception as exc:   # an extra filter must never cost a whole roster
            print(f"偵測班表上的浮動視窗時出錯，略過：{type(exc).__name__}: {exc}", flush=True)
            found = overlays.Overlays()
        overlays.save(found, dataset / "overlays" / f"{group[:16]}.json")
        photos[-1]["overlays"] = found.summary()
        draw_debug(registration, rows, columns, found).save(debug_dir / f"{group[:16]}.jpg", quality=88)
        # clean corrected canvas + cell quads: used to draw per-person evidence photos
        from .evidence import save_geometry
        (dataset / "corrected").mkdir(exist_ok=True)
        registration.image.convert("RGB").save(dataset / "corrected" / f"{group[:16]}.jpg", quality=90)
        save_geometry(registration, sorted(FULL_TIME_ROWS), FULL_DUTY_COLUMNS,
                      dataset / "geometry" / f"{group[:16]}.json")
        for row in sorted(FULL_TIME_ROWS):
            for column in FULL_DUTY_COLUMNS:
                crop, _ = context_crop(registration, row, column, fx, fy)
                sample_id = f"{group[:16]}_r{row:02d}_c{column:02d}"
                destination = images_dir / f"{sample_id}.png"
                crop.save(destination, format="PNG", compress_level=1)
                output_rows.append({"sample_id": sample_id, "image": destination.relative_to(dataset).as_posix(),
                                    "source_group": group, "row": row, "column": column,
                                    "split": roster_resnet._split_for(group),
                                    "ding": "", "wu": "", "ji": "", "geng": ""})
    kept = [row for row in existing if row["source_group"] not in processed]
    temporary = manifest.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(roster_resnet.FIELDS))
        writer.writeheader()
        writer.writerows(sorted(kept + output_rows, key=lambda r: (r["source_group"], int(r["row"]), int(r["column"]))))
    temporary.replace(manifest)
    report = {"method": "v2_tracked", "source_files": len(sources), "successful_photos": len(processed),
              "samples": len(kept) + len(output_rows), "photos": photos, "failures": failures}
    import json
    (dataset / "ingest-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
    return report
