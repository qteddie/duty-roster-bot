"""Read the 「服勤人員姓名代碼對照表」 under the duty grid: which name has which code today.

Numeric codes rotate daily, so the bot links people by name; the fixed Chinese
and English codes are supplied by the staff directory (fixed_staff_codes.json). The table's code
column is pre-printed in a fixed order (``CODE_LAYOUT``); only the names change.
The page is warped upright with the grid registration, the table's 15 row rules and
its 7 (code | name) column pairs are fitted to the line profile, and the 98 name
cells are cropped, stacked into one image and read in a single Tesseract
(``chi_tra``) call. Readings are raw OCR text; ``match_names`` snaps them to known
staff names before anything is shown to users. A located cell with no ink at all is
reported as blank (``read_name_cells``): nobody has that code today, nothing was missed.
"""
from __future__ import annotations

import difflib
from collections import Counter
import json
import os
import re
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

COLUMNS = [
    [str(n) for n in range(1, 15)],
    [str(n) for n in range(15, 29)],
    [str(n) for n in range(29, 36)] + list("甲乙丙丁戊己庚"),
    list("辛壬癸子丑寅卯午未申酉") + ["54", "55", "56"],
    [str(n) for n in range(57, 71)],
    [str(n) for n in range(71, 85)],
    list("ABCDEFGHIJKLMN"),
]
ROWS = 14
MAX_FILLED_RULES = 9   # most rows one run of undetected rules may span (a table has 14)
STRONG_RULE = .8       # a line inked over this share of the column is surely a printed rule
MIN_SUPPORT = 5        # printed codes that must read correctly, summed over the table's seven columns
# 3: full name cells plus single-line recovery for names missed by the stacked OCR
# 4: rule gaps of up to 9 rows are filled (columns used to be skipped when 5+ rules were faint)
# 5: the seven columns share one first row (a column could be laid ten rows off by three misread digits),
#    with the sheet's lean compensated column by column, and a faint stroke inside a row no longer splits it
NAMES_VERSION = 5
CJK = re.compile(r"[\u4e00-\u9fff]")
# tessdata_fast chi_tra reads these small printed names better than Debian's tesseract-ocr-chi-tra
TESSDATA = Path(__file__).resolve().parents[1] / "assets" / "tessdata"
# (tile height, binarize): each reading misses different names; matches are merged
VARIANTS = ((48, False), (64, False), (96, False), (64, True))


def warp_page(photo: Path, template, scale: float = 2.5, *, color: bool = False) -> Image.Image | None:
    """The whole sheet, perspective-corrected onto the template canvas (grayscale).

    The grid canvas is ~1600 px tall, which leaves a name only ~16 px high; ``scale``
    re-samples the original photo at a higher resolution instead.
    """
    import cv2
    from backend.phase1_cell_pipeline import _perspective_matrix, _registration_corners
    with Image.open(photo) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB" if color else "L")
    corners, _ = _registration_corners(image.convert("RGB"), template)
    if corners is None:
        return None
    matrix, (width, height) = _perspective_matrix(corners, template)
    zoom = np.diag([scale, scale, 1.0])
    page = cv2.warpPerspective(np.asarray(image), zoom @ matrix, (round(width * scale), round(height * scale)),
                               flags=cv2.INTER_AREA, borderValue=(255, 255, 255) if color else 255)
    return Image.fromarray(page)


def _fit(profile: np.ndarray, count: int, starts, pitches, offsets=(0.0,)) -> tuple[float, float]:
    """(start, pitch) whose ``count`` evenly spaced rules (plus ``offsets`` × pitch) hit the most ink."""
    best, choice = -1.0, (starts[0], pitches[0])
    for pitch in pitches:
        for start in starts:
            points = [start + k * pitch + o * pitch for k in range(count) for o in offsets]
            idx = np.round(points).astype(int)
            if idx.min() < 0 or idx.max() >= len(profile):
                continue
            score = float(profile[idx].sum())
            if score > best:
                best, choice = score, (start, pitch)
    return choice


def _snap(profile: np.ndarray, guesses: list[float], window: float) -> list[float]:
    """Move each guessed rule to the strongest ink peak within ±``window`` (keeps the guess if none)."""
    snapped = []
    for guess in guesses:
        lo, hi = max(0, round(guess - window)), min(len(profile), round(guess + window) + 1)
        if hi <= lo:
            snapped.append(guess); continue
        part = profile[lo:hi]
        snapped.append(float(lo + int(part.argmax())) if part.max() > .3 else guess)
    return snapped


def _smooth(profile: np.ndarray, reach=2) -> np.ndarray:
    return np.maximum.reduce([np.roll(profile, s) for s in range(-reach, reach + 1)])


def _row_rules(ink: np.ndarray, x1: int, x2: int, starts, pitches) -> list[float]:
    """The 15 rules below the header (header bottom … last row bottom) inside one column band."""
    profile = _smooth(ink[:, x1:x2].mean(axis=1))
    y0, py = _fit(profile, ROWS + 2, starts, pitches)
    return _snap(profile, [y0 + k * py for k in range(1, ROWS + 2)], py * .3)


def _without_stray_strokes(rules: list[float], profile: np.ndarray, pitch: float) -> list[float]:
    """Drop a faint line that sits closer than .7 pitch to a firm rule: no row of the table is that short.

    A long dash in a name, a fold or a smudge inside a row used to cut it into two halves, both too short to be
    a row, so the row itself went missing and every code below it landed one row off (the 10/2 update lost its
    middle rows in two columns this way). Faint lines at a proper distance from the firm rules stay: those are
    the printed rules that glare or a fold has made faint.
    """
    def firm(y: float) -> bool:
        return profile[round(y)] >= STRONG_RULE

    kept: list[float] = []
    for y in rules:
        while kept and y - kept[-1] < .7 * pitch and firm(y) and not firm(kept[-1]):
            kept.pop()                                # the earlier line was the faint one
        if kept and y - kept[-1] < .7 * pitch and firm(kept[-1]) and not firm(y):
            continue                                  # this one is
        kept.append(y)
    return kept


def _row_candidates(ink: np.ndarray, x1: int, x2: int, y_lo: int, y_hi: int, pitch: float) -> list[tuple[float, float]]:
    """Every row-like band (top, bottom) between horizontal rules inside one column band.

    Rules are found as ink peaks, not as an evenly spaced comb, so a table that sits a few
    rows higher or lower than expected is still found; a missing (faint) rule is filled in.
    """
    import cv2
    band = (ink[:, x1:x2] * 255).astype(np.uint8)
    # keep only long horizontal strokes: rules survive, characters do not
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, round(.35 * (x2 - x1))), 1))
    lines = cv2.morphologyEx(cv2.dilate(band, np.ones((3, 1), np.uint8)), cv2.MORPH_OPEN, kernel)
    profile = _smooth((lines > 0).mean(axis=1), 1)
    peaks = [y for y in range(max(1, y_lo), min(len(profile) - 1, y_hi))
             if profile[y] >= .5 and profile[y] >= profile[y - 1] and profile[y] >= profile[y + 1]]
    rules: list[float] = []
    for y in peaks:                       # merge a thick rule's neighbouring maxima
        if rules and y - rules[-1] < .45 * pitch:
            if profile[y] > profile[round(rules[-1])]:
                rules[-1] = float(y)
            continue
        rules.append(float(y))
    rules = _without_stray_strokes(rules, profile, pitch)
    filled: list[float] = []
    for a, b in zip(rules, rules[1:] + [None]):
        filled.append(a)
        if b is not None:
            steps = round((b - a) / pitch)
            # Glare or a fold can hide a whole run of rules (photos of 9/12 and 9/24 lost 5–7 in a row), and
            # a column with a hole used to be dropped altogether, taking its 14 names with it. Whatever is
            # filled in must still pass the printed-code check in ``locate`` before any name is read.
            if 2 <= steps <= MAX_FILLED_RULES and abs((b - a) / steps - pitch) < .2 * pitch:
                filled += [a + (b - a) * n / steps for n in range(1, steps)]
    rows = [(a, b) for a, b in zip(filled, filled[1:]) if .7 * pitch <= b - a <= 1.4 * pitch]
    if rows:   # faint bottom rules: continue at the local pitch (the code check verifies these rows too)
        step = float(np.median([b - a for a, b in rows]))
        last = rows[-1][1]
        rows += [(last + n * step, last + (n + 1) * step) for n in range(5) if last + (n + 1) * step < y_hi]
    return rows


SLOPES = np.linspace(-.08, .08, 65)   # how far a vertical rule may lean: up to 4.6° either way
RULE_COVER = .85                      # share of the table's height a line must be inked over to count as a rule


def _lean(ink: np.ndarray, x0: float, y_top: float, y_bot: float, slopes, half: int = 40) -> dict:
    """The strongest leaning vertical line within ±``half`` px of ``x0`` between ``y_top`` and ``y_bot``.

    Returns its ink coverage, its slope (px of x per px of y) and the span of columns it covers at mid-height
    (``left``…``right``), so that it sits at ``left + slope * (y - y_mid)`` on the row at ``y``.
    """
    y = np.arange(round(y_top), round(y_bot))
    y_mid = (y_top + y_bot) / 2
    lo = round(x0) - half
    xs = np.arange(lo, lo + 2 * half + 1)
    tried = []
    for slope in slopes:
        cols = xs[None, :] + np.round(slope * (y - y_mid)).astype(int)[:, None]
        inside = (cols >= 0) & (cols < ink.shape[1])
        profile = np.where(inside, ink[y[:, None], np.clip(cols, 0, ink.shape[1] - 1)], 0).mean(axis=0)
        peak = int(profile.argmax())
        tried.append((float(profile[peak]), float(slope), profile, peak))
    cover, _, profile, peak = max(tried, key=lambda t: t[0])
    # a rule is several px thick, so a range of slopes fits about equally well: take the middle of it
    slope = float(np.median([t[1] for t in tried if t[0] >= cover - .02]))
    a = b = peak
    while a > 0 and profile[a - 1] >= .5 * cover:
        a -= 1
    while b < len(profile) - 1 and profile[b + 1] >= .5 * cover:
        b += 1
    return {"cover": cover, "slope": slope, "left": lo + a, "right": lo + b, "y_mid": y_mid,
            "center": lo + (a + b) / 2}


def _vertical_rules(ink: np.ndarray, nominal: list[float], y_top: float, y_bot: float) -> list[dict | None]:
    """The table's vertical rules as leaning lines (``None`` where none can be fitted), in ``nominal`` order.

    The perspective correction leaves the rules leaning by up to 4° (most at the left of the page, steadily
    less towards the right), so one fixed x per column puts the lower cells' edges 20–35 px off: the last
    character of a name is cut in half, and a blank cell takes in the code printed beside it. A rule too
    faint to fit on its own follows the lean of the others.
    """
    fits = [_lean(ink, x, y_top, y_bot, SLOPES) for x in nominal]
    sure = [f for f in fits if f["cover"] >= RULE_COVER]
    if len(sure) >= 5:
        a, b = np.polyfit([f["center"] for f in sure], [f["slope"] for f in sure], 1)
        for i, fit in enumerate(fits):
            if fit["cover"] < RULE_COVER:
                again = _lean(ink, nominal[i], y_top, y_bot, a * nominal[i] + b + np.linspace(-.006, .006, 5))
                if again["cover"] > fit["cover"]:
                    fits[i] = again
    rules = [f if f["cover"] >= .7 else None for f in fits]
    for i in range(1, len(rules)):   # neighbouring rules are 70+ px apart; two fits on one line are both wrong
        if rules[i] and rules[i - 1] and rules[i]["center"] - rules[i - 1]["center"] < 40:
            rules[i] = rules[i - 1] = None
    return rules


def _code_matches(reading: str, code: str) -> bool:
    """Loose check that an OCR reading of a code cell shows ``code`` (digits, A–N or 甲乙…)."""
    if code.isdigit():
        digits = "".join(ch for ch in reading if ch.isdigit())
        # a cell rule read as an extra 1 ("110") is tolerated only for two-digit codes: "11" ≠ "1"
        return digits == code or (len(code) == 2 and len(digits) == 3 and code in digits)
    if code.isascii():
        return code in reading.upper()
    return code in reading


def _layouts(candidates: list, readings: dict) -> list[dict]:
    """Per column, every way of laying its printed code sequence over its row bands.

    ``{offset: (top of the first row, printed codes read correctly)}``: the band at ``offset`` is taken to be the
    sequence's first code (1, 15, 29, 辛, …); a negative offset means the table's first rows lie above the
    bands that were found, so that top is extrapolated.
    """
    layouts = []
    for k, (codes, rows) in enumerate(zip(COLUMNS, candidates)):
        layout = {}
        if not rows:                                   # no rule found in this column at all
            layouts.append(layout)
            continue
        step = float(np.median([b - a for a, b in rows]))
        for offset in range(-len(codes) + 3, len(rows) - 2):
            top = rows[offset][0] if offset >= 0 else rows[0][0] + offset * step
            layout[offset] = (top, sum(_code_matches(readings.get((k, offset + i), ""), code)
                                       for i, code in enumerate(codes) if 0 <= offset + i < len(rows)))
        layouts.append(layout)
    return layouts


def _offset_at(layout: dict, level: float | None, pitch: float) -> int | None:
    """The offset whose first row sits at ``level`` (within half a row), or ``None``."""
    if level is None or not layout:
        return None
    offset, (top, _) = min(layout.items(), key=lambda item: abs(item[1][0] - level))
    return offset if abs(top - level) <= .5 * pitch else None


def _drift(candidates: list, pitch: float) -> list[float]:
    """How far each column's rules sit below column 0's: a photographed sheet leans.

    Across the table's width the rules drift by up to most of a row (45 px against a 54 px row on the 9/30
    photo), so "the same row" is not one height. Neighbouring columns are one printed rule apart at most
    ``pitch / 2`` and usually a tenth of that, so the nearest rule of the column before names the same line:
    the median of those gaps, summed along the table, is the drift. Geometry only, no OCR involved.
    """
    drift, previous, total = [], None, 0.0
    for rows in candidates:
        tops = [a for a, _ in rows]
        if tops and previous:
            gaps = [t - min(previous, key=lambda s: abs(t - s)) for t in tops]
            gaps = [g for g in gaps if abs(g) <= .5 * pitch]
            total += float(np.median(gaps)) if gaps else 0.0
        drift.append(total)
        previous = tops or previous
    return drift


def _first_row(layouts: list[dict], pitch: float, drift: list[float] | None = None
               ) -> tuple[float | None, int, int]:
    """Height of the table's first row, as read from all columns together: (level, support, rival).

    Read on its own, a column is easily fooled: the sequence 1 … 14 fits at row 10 as soon as three cells read
    "1", "2" and "4" (two-digit codes lose a digit), and the column ends up ten rows off, taking its two
    neighbours with it. The seven columns are one table, so its first row sits at the same height in all of
    them (``level`` is the height in column 0; ``drift`` says how far lower each other column's rules sit):
    each height is scored by the codes that read correctly there, summed over the columns. A wrong height
    collects a few chance matches; the right one collects them all. ``rival`` is the best score the same
    table would get if it lay some other whole number of rows higher or lower.
    """
    drift = drift or [0.0] * len(layouts)
    levels = sorted({top - drift[k] for k, layout in enumerate(layouts) for top, _ in layout.values()})
    if not levels:
        return None, 0, 0

    def score(level: float) -> tuple[int, int]:
        total = close = 0
        for k, layout in enumerate(layouts):
            offset = _offset_at(layout, level + drift[k], pitch)
            if offset is not None:
                total += layout[offset][1]
                close += abs(layout[offset][0] - drift[k] - level) <= .25 * pitch
        return total, close                                          # ties: the level most columns sit on

    scored = {level: score(level) for level in levels}
    best = max(levels, key=lambda level: scored[level])
    chosen = [_offset_at(layout, best + drift[k], pitch) for k, layout in enumerate(layouts)]

    def shifted(rows: int) -> int:
        return sum(layout[offset + rows][1] for layout, offset in zip(layouts, chosen)
                   if offset is not None and offset + rows in layout)

    return best, scored[best][0], max((shifted(rows) for rows in range(-60, 61) if rows), default=0)


def _clear(support: int, rival: int) -> bool:
    """Is the winning first-row height clear enough to put names on it?

    Enough printed codes read correctly, and at least twice as many as at any rival height. Calibrated on the
    29 sample photos, each also blurred, shrunk, noisy and faded: a wrong height never collected more than two
    chance matches, while the right height of a sharp photo collects thirteen or more.
    """
    return support >= MIN_SUPPORT and support >= 2 * rival + 2


def _follow(rows: list[tuple[float, float]], offset: int, pitch: float) -> dict[int, tuple[float, float]]:
    """A column's row bands by position in its code sequence (0 = first code), starting at ``offset``.

    Bands are followed by the rule they share, so a band the detection missed leaves its row without a box
    instead of pushing every row below it one place up. A missed band is bridged (up to two in a row); anything
    that does not continue the table ends it.
    """
    first = max(offset, 0)
    if first >= len(rows):
        return {}
    index = first - offset
    found = {index: rows[first]}
    bottom = rows[first][1]
    for top, end in rows[first + 1:]:
        gap = (top - bottom) / pitch
        missed = round(gap)
        if missed < 0 or missed > 2 or abs(gap - missed) > .3:
            break
        index += 1 + missed
        if index >= ROWS:
            break
        found[index] = (top, end)
        bottom = end
    return found


def locate(page: Image.Image) -> tuple[dict[str, tuple[int, int, int, int]], list[dict]]:
    """Code → name-cell box on the warped page, plus per-column diagnostics.

    The printed codes in each code column are read and the expected sequence (1–14, 15–28, …)
    is slid along the detected rows. The seven (code | name) columns are one table, so they
    are aligned together (``_first_row``): the first row's height is the one at which most
    printed codes read correctly across all columns. A table whose codes do not clearly agree
    on one height is left out, so a mis-located table yields *no* names rather than names on
    the wrong codes.
    """
    width, height = page.size
    ink = (np.asarray(page) < 150).astype(np.float32)
    pitches = np.linspace(.0118 * height, .0134 * height, 17)
    # a first, page-wide row fit only to find the table band for the column fit
    ys = _row_rules(ink, round(.01 * width), round(.62 * width),
                    np.arange(round(.62 * height), round(.70 * height)), pitches)
    pitch = float(np.median(np.diff(ys)))
    columns = _smooth(ink[round(ys[0]):round(ys[-1]), :].mean(axis=0))
    # each pair: rule | code (≈1/3) | rule | name (≈2/3) | next pair
    x0, px = _fit(columns, len(COLUMNS) + 1, np.arange(0, round(.07 * width)),
                  np.linspace(.076 * width, .087 * width, 23))
    split = _fit(columns, len(COLUMNS), [x0 + px * .25 + d for d in np.arange(0, px * .2)], [px])[0] - x0
    pairs = _snap(columns, [x0 + k * px for k in range(len(COLUMNS) + 1)], px * .08)
    splits = _snap(columns, [pairs[k] + split for k in range(len(COLUMNS))], px * .08)
    y_lo, y_hi = round(ys[0] - 6 * pitch), round(ys[-1] + 6 * pitch)
    candidates = [_row_candidates(ink, round(pairs[k]), round(pairs[k + 1]), y_lo, y_hi, pitch)
                  for k in range(len(COLUMNS))]
    # read every candidate code cell in one Tesseract call
    tiles = []
    for k, rows in enumerate(candidates):
        cx1, cx2 = pairs[k], splits[k]
        inset = .12 * (cx2 - cx1)
        for r, (y1, y2) in enumerate(rows):
            tiles.append(((k, r), (round(cx1 + inset), round(y1 + .12 * (y2 - y1)),
                                   round(cx2 - inset), round(y2 - .1 * (y2 - y1)))))
    readings = _read_tiles(page, tiles, 64, False)
    layouts = _layouts(candidates, readings)
    drift = _drift(candidates, pitch)
    level, support, rival = _first_row(layouts, pitch, drift)
    # one table, one first row: a table whose printed codes cannot be read clearly enough gets no names at all
    agreed = level is not None and _clear(support, rival)
    boxes, report, anchored = {}, [], {}
    for k, (codes, rows) in enumerate(zip(COLUMNS, candidates)):
        layout = layouts[k]
        offset = _offset_at(layout, None if level is None else level + drift[k], pitch)
        hits = layout[offset][1] if offset is not None else 0
        chosen = layout[offset][0] if offset is not None else None
        runner = max((h for top, h in layout.values() if chosen is None or abs(top - chosen) > .5 * pitch), default=0)
        found = _follow(rows, offset, pitch) if agreed and offset is not None else {}
        ok = len(found) >= ROWS - 3
        if ok:
            anchored[k] = found
        report.append({"column": k, "first": codes[0], "rows": len(rows), "offset": offset, "hits": hits,
                       "runner_up": runner, "ok": ok,
                       # "codes": this column's own printed codes confirm the first row; "table": the other columns do
                       "via": ("codes" if hits >= 2 and hits - runner >= 2 else "table") if ok else None,
                       "support": support, "rival": rival})
    nominal = [pairs[0]] + [x for k in range(len(COLUMNS)) for x in (splits[k], pairs[k + 1])]
    rules = _vertical_rules(ink, nominal, ys[0], ys[-1])
    cells = {k: (rules[1 + 2 * k], rules[2 + 2 * k]) for k in range(len(COLUMNS))}
    # Every name cell of the form is about as wide as the others: a fit that disagrees is a wrong line.
    spans = {k: [_on_rule(right, y) + RIGHT_INSET - _on_rule(left, y) - LEFT_INSET for y in (ys[0], ys[-1])]
             for k, (left, right) in cells.items() if left and right}
    typical = float(np.median([w for pair in spans.values() for w in pair])) if len(spans) >= 3 else None
    for k in cells:
        if not (typical and k in spans and all(abs(w - typical) <= .15 * typical for w in spans[k])):
            cells[k] = (None, None)
    for k, rows in anchored.items():
        step = float(np.median([b - a for a, b in rows.values()]))
        top = min(a - i * step for i, (a, b) in rows.items()) - step  # keep the 代號／姓名 header
        bottom = max(b + (ROWS - i - 1) * step for i, (a, b) in rows.items())
        report[k]["table_bounds"] = [round(pairs[0]), round(top), round(pairs[-1]), round(bottom)]
        # Names can fill the whole cell. Fractional insets clipped characters,
        # particularly on the 10/3 photo; keep just a one-pixel rule inset.
        x1, x2 = splits[k] + 1, pairs[k + 1] - 1
        left, right = cells[k]
        for i, (y1, y2) in rows.items():
            y = (y1 + y2) / 2
            boxes[COLUMNS[k][i]] = (_edge(left, x1, y, LEFT_INSET, ys), round(y1 + 1),
                                    _edge(right, x2, y, RIGHT_INSET, ys), round(y2 - 1))
    return boxes, report


LEFT_INSET, RIGHT_INSET = .5, -2   # a cell starts at its left rule's left edge and ends 2 px before the right one


def _on_rule(rule: dict, y: float) -> float:
    """x of the rule's left edge at height ``y``."""
    return rule["left"] + rule["slope"] * (y - rule["y_mid"])


def _edge(rule: dict | None, nominal: float, y: float, inset: float, rows: list[float]) -> int:
    """x of a cell edge at height ``y``: on the fitted rule, unless that stays within 5 px of the fixed
    ``nominal`` position all down the table (a straight photo keeps exactly the positions it always had)."""
    if rule is None:
        return round(nominal)
    if max(abs(_on_rule(rule, rows[0]) + inset - nominal), abs(_on_rule(rule, rows[-1]) + inset - nominal)) <= 5:
        return round(nominal)
    return round(_on_rule(rule, y) + inset)


def cell_boxes(page: Image.Image) -> dict[str, tuple[int, int, int, int]]:
    """Code → name-cell box (x1, y1, x2, y2) for the columns whose printed codes were verified."""
    return locate(page)[0]


def _env() -> dict:
    env = dict(os.environ, OMP_THREAD_LIMIT="1")   # parallel readings: one thread each, or they thrash
    if (TESSDATA / "chi_tra.traineddata").is_file():
        env["TESSDATA_PREFIX"] = str(TESSDATA)
    return env


def _tesseract_tsv(image: Image.Image, *, psm: int = 6, lang: str = "chi_tra") -> list[dict]:
    buffer = BytesIO(); image.save(buffer, format="PNG")
    env = _env()
    if lang != "chi_tra":
        env.pop("TESSDATA_PREFIX", None)  # bundled data contains chi_tra only; use installed English data
    try:
        completed = subprocess.run(["tesseract", "stdin", "stdout", "-l", lang, "--psm", str(psm),
                                    "-c", "tessedit_create_tsv=1"],
                                   input=buffer.getvalue(), capture_output=True, timeout=30 if psm == 6 else 10, check=False,
                                   env=env)
    except subprocess.TimeoutExpired:
        return []
    if completed.returncode != 0:
        return []
    lines = completed.stdout.decode("utf-8", "replace").splitlines()
    head = lines[0].split("\t") if lines else []
    return [dict(zip(head, line.split("\t"))) for line in lines[1:] if line.count("\t") >= len(head) - 1]


def _otsu(values: np.ndarray) -> float:
    hist = np.bincount(values.ravel(), minlength=256).astype(float)
    total, cumulative, mean_all = hist.sum(), np.cumsum(hist), np.cumsum(hist * np.arange(256))
    weight = cumulative / total
    mean = mean_all / np.maximum(cumulative, 1)
    between = (mean_all[-1] / total * weight - mean_all / total) ** 2 / np.maximum(weight * (1 - weight), 1e-9)
    return float(np.argmax(between))


def _resize_tile(crop: Image.Image, tile_h: int, binarize: bool) -> Image.Image:
    crop = ImageOps.autocontrast(crop.resize((max(1, round(crop.width * tile_h / crop.height)), tile_h),
                                             Image.LANCZOS))
    if binarize:
        values = np.asarray(crop)
        crop = Image.fromarray(np.where(values < _otsu(values), 0, 255).astype(np.uint8))
    return crop


def _has_name_ink(crop: Image.Image) -> bool:
    """Ignore the outer rules when deciding if an otherwise blank name cell has text."""
    values = np.asarray(crop)
    h, w = values.shape
    body = values[round(.15 * h):round(.85 * h), round(.15 * w):round(.85 * w)]
    return bool(body.size and (body < 150).mean() >= .03)


def _read_name_cell(page: Image.Image, box, tile_h: int, binarize: bool) -> str:
    """Read one short name as a text line; stacked OCR sometimes omits entire lines."""
    crop = page.crop(box)
    if crop.width < 2 or crop.height < 2 or not _has_name_ink(crop):
        return ""
    crop = ImageOps.expand(_resize_tile(crop, tile_h, binarize), border=20, fill=255)
    try:
        words = _tesseract_tsv(crop, psm=7)
    except subprocess.TimeoutExpired:
        return ""  # retain the successful readings from other cells and variants
    return "".join(CJK.findall("".join(word.get("text", "") for word in words)))


def _narrow_name_box(box) -> tuple[int, int, int, int]:
    """A second framing for small, centred text whose surrounding rules upset OCR."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1 + 2, y2 - y1 + 2
    return (round(x1 - 1 + .07 * w), round(y1 - 1 + .14 * h),
            round(x2 + 1 - .05 * w), round(y2 + 1 - .1 * h))


def _read_tiles(page: Image.Image, tiles: list, tile_h: int, binarize: bool, *, lang: str = "chi_tra") -> dict:
    """Crop each (key, box), stack the crops into one image and read them in one Tesseract call."""
    gap = 40
    crops = []
    for key, box in tiles:
        crop = page.crop(box)
        if crop.width < 2 or crop.height < 2 or (np.asarray(crop) < 128).mean() < .02:   # blank cell
            continue
        crop = _resize_tile(crop, tile_h, binarize)
        crops.append((key, crop))
    if not crops:
        return {}
    width = max(c.width for _, c in crops) + 2 * gap
    mosaic = Image.new("L", (width, gap + len(crops) * (tile_h + gap)), 255)
    slots = []
    for i, (key, crop) in enumerate(crops):
        top = gap + i * (tile_h + gap)
        mosaic.paste(crop, (gap, top))
        slots.append((key, top, top + tile_h))
    texts: dict = {}
    words = _tesseract_tsv(mosaic) if lang == "chi_tra" else _tesseract_tsv(mosaic, lang=lang)
    for word in words:
        text = word.get("text", "").strip()
        if not text:
            continue
        center = int(word["top"]) + int(word["height"]) / 2
        for key, top, bottom in slots:
            if top - gap / 2 <= center < bottom + gap / 2:
                texts[key] = texts.get(key, "") + text
                break
    return texts


def read_names(page: Image.Image, tile_h: int = 64, binarize: bool = False,
               boxes: dict | None = None) -> dict[str, str]:
    """Code → raw OCR text (CJK only) of its name cell; empty cells are left out."""
    boxes = cell_boxes(page) if boxes is None else boxes
    texts = _read_tiles(page, list(boxes.items()), tile_h, binarize)
    cleaned = {code: "".join(CJK.findall(value)) for code, value in texts.items()}
    return {code: value for code, value in cleaned.items() if value}


def read_name_table(photo: Path, template, *, directory=None) -> dict[str, list[str]]:
    """Code → raw name readings, with single-line recovery for unresolved name cells (see ``read_name_cells``)."""
    return read_name_cells(photo, template, directory=directory)[0]


BLANK_DARK = .01      # a blank cell's middle: under this share darker than grey 150…
BLANK_STROKES = .005  # …and under this share 20 levels darker than its own background (its median)


def _blank(page: Image.Image, box) -> bool:
    """Is a located name cell empty on the sheet: no strokes inside its rules (nobody has that code today)?

    Conservative on purpose: calling a cell blank tells the administrator the name was not missed, so a wrong
    "blank" hides a real miss, while a wrong "not blank" only falls back to 「沒對到」. The names are printed in
    light grey, and a slightly bright, faded or blurred photo leaves them lighter than ``_has_name_ink``'s grey 150
    (on the 10/13 sheet 施志明 is only just over its line), so strokes are measured against the cell's own
    background as well: on that photo, mildly degraded, blank cells stay under 0.3% and named ones above 3%."""
    crop = page.crop(box)
    if crop.width < 2 or crop.height < 2:
        return False
    values = np.asarray(crop).astype(int)
    h, w = values.shape
    body = values[round(.15 * h):round(.85 * h), round(.15 * w):round(.85 * w)]
    if not body.size:
        return False
    return bool((body < 150).mean() < BLANK_DARK and (body < np.median(body) - 20).mean() < BLANK_STROKES)


def read_name_cells(photo: Path, template, *, directory=None) -> tuple[dict[str, list[str]], list[str]]:
    """(code → raw name readings, codes whose name cell is blank), both in table order.

    Readings get single-line recovery for unresolved name cells. A code is blank only when its cell was located
    and has no ink: the sheet leaves it empty (a code nobody has that day, which may still carry a 兼服 duty), so
    the name was not missed. A cell with ink that no reading came out of, and a cell in a column skipped because
    its printed codes could not be verified, are unknown, not blank. Returns ({}, []) when Tesseract is missing or
    the table cannot be located; the duty grid is still usable then, only names are unknown.
    """
    if not shutil.which("tesseract"):
        return {}, []
    try:
        page = warp_page(photo, template)
        if page is None:
            return {}, []
        boxes, report = locate(page)
        blank = {code for code, box in boxes.items() if _blank(page, box)}
        skipped = [f"{r['first']} 欄（對到 {r['hits']}／次佳 {r['runner_up']}）" for r in report if not r["ok"]]
        if not boxes:
            table = report[0] if report else {}
            print(f"姓名對照表：{photo.name} 的代號讀不清楚（整張表對到 {table.get('support', 0)}／次佳 "
                  f"{table.get('rival', 0)}），沒有採用任何姓名", flush=True)
        elif skipped:
            print(f"姓名對照表：{photo.name} 有欄位無法核對代號，略過：{'、'.join(skipped)}", flush=True)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(VARIANTS)) as pool:
            readings = list(pool.map(lambda v: read_names(page, *v, boxes=boxes), VARIANTS))
        raw = {code: [r.get(code, "") for r in readings] for code in boxes}
        if directory is None:
            directory = load_directory(Path(__file__).resolve().parents[1] / "configs" / "staff_names.txt")
        matched = match_names(raw, directory)
        pending = [code for code, box in boxes.items() if code not in matched and _has_name_ink(page.crop(box))]
        # Only unresolved, nonblank cells need the extra reads. Keep raw evidence;
        # the same conservative matching rules still reject ambiguous names.
        variants = ((64, False), (64, True), (96, True), (48, False))
        jobs = [(code, variant) for code in pending for variant in variants]
        with ThreadPoolExecutor(max_workers=4) as pool:
            recovered = pool.map(lambda job: _read_name_cell(page, boxes[job[0]], *job[1]), jobs)
            for (code, _), reading in zip(jobs, recovered):
                raw[code].append(reading)
        matched = match_names(raw, directory)
        narrow = {code: _narrow_name_box(boxes[code]) for code in pending if code not in matched}
        if narrow:
            with ThreadPoolExecutor(max_workers=4) as pool:
                tighter = list(pool.map(lambda v: read_names(page, *v, boxes=narrow), VARIANTS))
            for code in narrow:
                raw[code].extend(r.get(code, "") for r in tighter)
            matched = match_names(raw, directory)
            jobs = [(code, variant) for code in narrow if code not in matched for variant in VARIANTS]
            with ThreadPoolExecutor(max_workers=4) as pool:
                recovered = pool.map(lambda job: _read_name_cell(page, narrow[job[0]], *job[1]), jobs)
                for (code, _), reading in zip(jobs, recovered):
                    raw[code].append(reading)
    except Exception as exc:  # the duty grid matters more than the names
        print(f"讀取姓名對照表失敗：{type(exc).__name__}: {exc}", flush=True)
        return {}, []
    codes = [code for column in COLUMNS for code in column]
    return ({code: raw[code] for code in codes if code in raw and any(raw[code])},
            [code for code in codes if code in blank])


# ---------------------------------------------------------------- matching
def similarity(reading: str, name: str) -> float:
    """Share of characters the reading and the name have in common, in order."""
    if not reading or not name:
        return 0.0
    blocks = difflib.SequenceMatcher(None, reading, name, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / max(len(name), len(reading))


def _match_once(readings: dict[str, str], names: list[str], threshold: float, margin: float) -> dict[str, str]:
    claims: dict[str, list[tuple[float, str]]] = {}
    for code, reading in readings.items():
        scores = sorted(((similarity(reading, n), n) for n in names), reverse=True)
        if not scores or scores[0][0] < threshold:
            continue
        if len(scores) > 1 and scores[0][0] - scores[1][0] < margin:
            continue
        claims.setdefault(scores[0][1], []).append((scores[0][0], code))
    result = {}
    for name, found in claims.items():   # a name is on the table once: the closest reading wins, ties lose
        found.sort(reverse=True)
        if len(found) == 1 or found[0][0] > found[1][0]:
            result[found[0][1]] = name
    return result


def match_names(readings: dict, directory, threshold=.6, margin=.15, *, fixed_codes=None) -> dict[str, str]:
    """Code → known staff name.

    ``readings`` maps a code to one OCR string or a list of them (one per variant).
    Each variant is matched on its own — a reading snaps to the most similar directory
    name only when that name clearly beats the runner-up — and the results are merged.
    A code or name the variants disagree on is dropped: no name is better than a wrong one.
    Authoritative fixed codes take precedence over OCR, and their names cannot be
    matched to a rotating code even when a cell was misread or misaligned.
    """
    fixed = dict(fixed_codes or {})
    names = sorted({n.strip() for n in directory if n and n.strip()} - set(fixed.values()))
    if not names:
        return fixed
    as_lists = {c: (r if isinstance(r, list) else [r]) for c, r in readings.items() if c not in fixed}
    depth = max((len(r) for r in as_lists.values()), default=0)
    merged: dict[str, str] = {}
    doubtful: set[str] = set()
    for i in range(depth):
        variant = {c: r[i] for c, r in as_lists.items() if i < len(r) and r[i]}
        for code, name in _match_once(variant, names, threshold, margin).items():
            if merged.get(code, name) != name:
                doubtful.add(code)
            merged.setdefault(code, name)
    for code in doubtful:
        merged.pop(code, None)
    # Confirmed small-print confusion on the roster: 姍 → 姻. Require two
    # readings that become an exact directory name; never replace an actual
    # directory spelling, a strict match, or a conflicting pair of variants.
    aliases = {"姻": "姍"}
    aliases = {a: b for a, b in aliases.items() if not any(a in n for n in names)}
    table = str.maketrans(aliases)
    for code, values in as_lists.items():
        if code in merged or code in doubtful:
            continue
        votes = Counter(canonical for value in values
                        if (canonical := value.translate(table)) != value and canonical in names)
        candidates = [name for name, count in votes.items() if count >= 2]
        if len(candidates) == 1:
            merged[code] = candidates[0]
    owners: dict[str, list[str]] = {}
    for code, name in merged.items():
        owners.setdefault(name, []).append(code)
    return {**{codes[0]: name for name, codes in owners.items() if len(codes) == 1}, **fixed}


def load_directory(path: Path | None) -> list[str]:
    """Staff names from a text file: one per line, ``#`` starts a comment."""
    if not path or not Path(path).is_file():
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]


def load_fixed_codes(path: Path | None) -> dict[str, str]:
    """Authoritative fixed code (Chinese or English) → name mappings, independent of a roster's OCR."""
    from .schema import FIXED_CODES
    if not path or not Path(path).is_file():
        return {}
    fixed = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(fixed, dict) or any(
        code not in FIXED_CODES or not isinstance(name, str) or not re.fullmatch(r"[\u4e00-\u9fff]{2,5}", name)
        for code, name in fixed.items()
    ):
        raise ValueError("固定代號名冊格式錯誤：請使用中文代號或英文代號（A–N）與 2–5 個中文字的姓名")
    if len(set(fixed.values())) != len(fixed):
        raise ValueError("固定代號名冊中，同一姓名不能對應多個代號")
    return fixed
