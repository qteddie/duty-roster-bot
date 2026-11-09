"""Read the explicitly printed 輪休 / 請假人員 table beside the daily name table."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata

import numpy as np
from PIL import Image, ImageOps

from .name_table import VARIANTS, _has_name_ink, _read_name_cell, _read_tiles, _resize_tile, _tesseract_tsv, locate, warp_page
from .name_table import load_directory, load_fixed_codes, match_names, similarity
from .schema import ALLOWED_CODE_SET

LEAVE_VERSION = 3
KINDS = ("陪產假", "生理假", "休假", "補休", "補半", "事假", "病假", "喪假", "婚假", "公假",
         "產假", "特休", "年休", "身心調適假", "災防假", "榮譽假", "退伍", "停職", "調職", "受訓")


def locate_leave(page: Image.Image) -> dict | None:
    """Use verified name-table bounds, then follow the adjacent printed table rules.

    輪休 has three columns of codes; 請假 has code, name, kind and reason columns.
    The name/kind divider ends where the separate 代班 table begins, so that
    unrelated table is excluded rather than treating its codes as people on leave.
    """
    import cv2
    _, report = locate(page.convert("L"))
    bounds = [r["table_bounds"] for r in report if r.get("table_bounds")]
    if not bounds:
        return None
    w, h = page.size
    reference = bounds[-1]
    _, top, left, name_bottom = reference
    pitch = (name_bottom - top) / 15
    y1, y2 = max(0, round(top - pitch)), min(h, round(name_bottom + 7 * pitch))
    x1 = max(0, round(left - .015 * w))
    ink = (np.asarray(page.convert("L")) < 150).astype(np.uint8)
    band = ink[y1:y2, x1:]
    vertical = cv2.morphologyEx(cv2.dilate(band, np.ones((1, 3), np.uint8)), cv2.MORPH_OPEN,
                                np.ones((max(20, round(4 * pitch)), 1), np.uint8))
    contours, _ = cv2.findContours(vertical, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rules = sorted((x1 + x + width / 2, y1 + y + height) for contour in contours
                   for x, y, width, height in [cv2.boundingRect(contour)] if height >= 9 * pitch)
    def boundary(after, lo, hi):
        return next((rule for rule in rules if lo <= rule[0] - after <= hi), None)
    start = min(rules, key=lambda r: abs(r[0] - left), default=None)
    if start is None or abs(start[0] - left) > .02 * w:
        return None
    wheel_end = boundary(start[0], .06 * w, .10 * w)
    code_end = boundary(wheel_end[0], .018 * w, .04 * w) if wheel_end else None
    name_end = boundary(code_end[0], .035 * w, .075 * w) if code_end else None
    kind_end = boundary(name_end[0], .035 * w, .075 * w) if name_end else None
    if kind_end is None:
        return None
    endings = [r[1] for r in (wheel_end, name_end) if name_bottom + 2 * pitch < r[1] < name_bottom + 7 * pitch]
    if not endings:
        return None
    bottom = min(endings)
    horizontal = cv2.morphologyEx(band, cv2.MORPH_OPEN,
                                  np.ones((1, max(20, round(.2 * w))), np.uint8))
    contours, _ = cv2.findContours(horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    ys = sorted(y1 + y + height / 2 for contour in contours for x, y, width, height in [cv2.boundingRect(contour)]
                if width >= .2 * w)
    def snap(y):
        nearest = min(ys, key=lambda v: abs(v - y), default=y)
        return nearest if abs(nearest - y) <= .4 * pitch else y
    top, first, name_bottom, bottom = map(snap, (top, top + pitch, name_bottom, bottom))
    rest_boxes = []
    for row in range(14):
        a, b = first + row * (name_bottom - first) / 14, first + (row + 1) * (name_bottom - first) / 14
        for column in range(3):
            l = start[0] + column * (wheel_end[0] - start[0]) / 3
            r = start[0] + (column + 1) * (wheel_end[0] - start[0]) / 3
            rest_boxes.append((round(l + 1), round(a + 1), round(r - 1), round(b - 1)))
    leave_first = snap(first + pitch)
    rows = round((bottom - leave_first) / pitch)
    if not 12 <= rows <= 22:
        return None
    leave_boxes = []
    for row in range(rows):
        a, b = snap(leave_first + row * pitch), snap(leave_first + (row + 1) * pitch)
        leave_boxes.append({key: (round(l + 2), round(a + 2), round(r - 2), round(b - 2))
                            for key, l, r in (("code", wheel_end[0], code_end[0]),
                                              ("name", code_end[0], name_end[0]),
                                              ("kind", name_end[0], kind_end[0]))})
    outer = max((r[0] for r in rules if r[0] > kind_end[0] + .1 * w), default=w - 1)
    crop = (max(0, round(start[0] - 4)), max(0, round(top - 4)), min(w, round(outer + 4)),
            min(h, round(bottom + 4)))
    return {"bounds": crop, "rest": rest_boxes, "leave": leave_boxes}


def code_reading(value: str) -> str | None:
    value = re.sub(r"[\s|:：.,，。]", "", unicodedata.normalize("NFKC", value)).upper()
    if value == "T":
        # Printed 丁 is routinely read as Latin T; T is outside this roster's A–N alphabet.
        value = "丁"
    if value == "成":
        # A small printed 戊 often retains the inner stroke but is OCRed as 成.
        value = "戊"
    if value.isdigit():
        value = str(int(value))
    return value if value in ALLOWED_CODE_SET else None


def agreed_code(readings) -> str | None:
    votes = Counter(code for text in readings if (code := code_reading(text)))
    if not votes:
        return None
    ranked = votes.most_common()
    if ranked[0][1] < 2 or len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def kind_reading(readings) -> str | None:
    readings = [''.join(re.findall(r'[一-鿿]+', value)) for value in readings]
    exact = {value for value in readings if value in KINDS}
    if len(exact) == 1:
        return next(iter(exact))
    if len(exact) > 1:
        return None
    # The long, small-print kinds are often read as several different wrong
    # characters, but their distinctive consecutive characters survive.
    if any(re.fullmatch(r'補.{0,2}休', value) for value in readings):
        return '補休'
    if sum('防假' in value for value in readings) >= 2:
        return '災防假'
    if (sum('心調' in value for value in readings) >= 2 and any('身心' in value for value in readings) or
            any(value.startswith('身心調') and value.endswith('假') for value in readings)):
        return '身心調適假'
    # In narrow two-character cells, the second glyph is frequently merged with
    # the table rule. Require repeated readings of an unambiguous first glyph.
    first_chars = Counter(value[0] for value in readings if len(value) >= 2)
    unique_starts = {kind[0]: kind for kind in KINDS if len(kind) == 2 and
                     sum(other.startswith(kind[0]) for other in KINDS) == 1}
    observed = {char: count for char, count in first_chars.items() if char in unique_starts}
    if len(observed) == 1:
        char, count = next(iter(observed.items()))
        if count >= 2:
            return unique_starts[char]
    last_chars = Counter(value[-1] for value in readings if len(value) >= 2)
    unique_ends = {kind[-1]: kind for kind in KINDS if len(kind) == 2 and
                   sum(other.endswith(kind[-1]) for other in KINDS) == 1}
    observed = {char: count for char, count in last_chars.items() if char in unique_ends}
    if len(observed) == 1:
        char, count = next(iter(observed.items()))
        if count >= 2:
            return unique_ends[char]
    votes = Counter()
    for value in readings:
        if len(value) < 2:
            continue
        scores = sorted((similarity(value, kind), kind) for kind in KINDS)
        if scores[-1][0] >= .67 and scores[-1][0] - scores[-2][0] >= .2:
            votes[scores[-1][1]] += 1
    ranked = votes.most_common()
    if not ranked or ranked[0][1] < 2 or len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def read_leave_table(photo: Path, template, *, names=None, directory=None, fixed_codes=None) -> dict:
    """Return only entries printed in the leave table, never infer leave from missing duties."""
    if not shutil.which("tesseract"):
        return {"available": False}
    page = warp_page(photo, template)
    geometry = locate_leave(page) if page is not None else None
    if geometry is None:
        return {"available": False}
    # Erase long table rules for OCR only. The evidence crop uses untouched source pixels.
    import cv2
    original_page = page
    ink = (np.asarray(page) < 150).astype(np.uint8)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, round(page.width * .045)), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((round(page.height * .05), 1), np.uint8))
    values = np.asarray(page).copy()
    # Remove photograph shading before introducing white rule pixels. Otherwise
    # Otsu separates the grey paper from the erased rules instead of finding text.
    background = cv2.morphologyEx(values, cv2.MORPH_CLOSE, np.ones((61, 61), np.uint8))
    values = cv2.divide(values, background, scale=255)
    values[cv2.dilate(horizontal | vertical, np.ones((3, 3), np.uint8)) > 0] = 255
    page = Image.fromarray(values)
    def occupied(box):
        # A bent row rule may be shorter than the page-wide rule filter. Remove
        # it locally before deciding whether a blank leave row contains a person.
        crop = np.asarray(page.crop(box)).copy()
        rule = cv2.morphologyEx((crop < 150).astype(np.uint8), cv2.MORPH_OPEN,
                               np.ones((1, max(10, round(crop.shape[1] * .65))), np.uint8))
        crop[rule > 0] = 255
        return _has_name_ink(Image.fromarray(crop))
    # Thin codes such as 丁 can disappear when the full-page rule mask is erased.
    occupied_rest = {i: box for i, box in enumerate(geometry["rest"])
                     if _has_name_ink(original_page.crop(box))}
    occupied_leave = {i: boxes for i, boxes in enumerate(geometry["leave"])
                      if any(occupied(boxes[key]) for key in ("code", "name"))}
    tiles = [(('rest', i), box) for i, box in occupied_rest.items()]
    tiles += [(('leave', i), boxes['code']) for i, boxes in occupied_leave.items()]
    code_variants = ((48, False), (64, False), (64, True), (96, True))
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(lambda v: _read_tiles(page, tiles, *v), code_variants))
        english = list(pool.map(lambda v: _read_tiles(page, tiles, *v, lang='eng'), code_variants))
    def single_code(box, lang, variant):
        crop = ImageOps.expand(_resize_tile(page.crop(box), *variant), border=20, fill=255)
        return ''.join(word.get('text', '') for word in _tesseract_tsv(crop, psm=7, lang=lang))
    def code_for(key):
        # Stacked OCR can turn I into L. A single-character lane is read as a
        # separate text line, where the top and bottom serifs remain visible.
        start = len(code_variants) if key[0] == 'rest' and key[1] % 3 == 2 and len(english) > len(code_variants) else 0
        chinese = agreed_code([b.get(key, "") for b in batches[start:]])
        latin = agreed_code([b.get(key, "") for b in english[start:]])
        # The two numeric lanes and the alphabet/Chinese lane are distinct in 輪休.
        if key[0] == 'rest':
            if key[1] % 3 < 2:
                choices = {c for c in (latin, chinese) if c and c.isdigit()}
                return next(iter(choices)) if len(choices) == 1 else None
            if chinese and not chinese.isascii():
                return chinese if not latin or latin == chinese else None
            choices = {c for c in (latin, chinese) if c and not c.isdigit()}
            return next(iter(choices)) if len(choices) == 1 else None
        return chinese if chinese and not chinese.isascii() else latin
    codes = {key: code_for(key) for key, _ in tiles}
    missing = [(key, box) for key, box in tiles
               if not codes[key] or key[0] == 'rest' and key[1] % 3 == 2]
    jobs = [(key, box, lang, variant) for key, box in missing for lang in ('eng', 'chi_tra') for variant in code_variants]
    batches.extend({} for _ in code_variants)
    english.extend({} for _ in code_variants)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = pool.map(lambda job: single_code(job[1], job[2], job[3]), jobs)
        for (key, _, lang, variant), value in zip(jobs, values):
            (english if lang == 'eng' else batches)[len(code_variants) + code_variants.index(variant)][key] = value
    codes.update({key: code_for(key) for key, _ in missing})
    # Printed numeric lanes are in ascending order. Use this only to reject or
    # disambiguate observed OCR candidates (e.g. 79 vs 19), never invent a code.
    for lane in range(2):
        indices = [i for i in occupied_rest if i % 3 == lane]
        for position, i in enumerate(indices):
            votes = Counter(c for b in (batches, english) for v in b
                            if (c := code_reading(v.get(('rest', i), ''))) and c.isdigit())
            candidates = {c for c, count in votes.items() if count >= 2}
            lower = next((int(codes[('rest', j)]) for j in reversed(indices[:position]) if codes[('rest', j)]), 0)
            upper = next((int(codes[('rest', j)]) for j in indices[position + 1:] if codes[('rest', j)]), 85)
            plausible = {c for c in candidates if lower < int(c) < upper}
            codes[('rest', i)] = next(iter(plausible)) if len(plausible) == 1 else None
    # A rejected numeric candidate was not necessarily missing in the stacked
    # pass (Linux can read 34 as 3). Check that entire cell again as a line, and
    # require three readings plus the verified neighbouring range before using it.
    unresolved = [i for i in occupied_rest if i % 3 < 2 and not codes[('rest', i)]]
    recovery_jobs = [(i, variant) for i in unresolved for variant in code_variants]
    recovery = {i: [] for i in unresolved}
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = pool.map(lambda job: single_code(occupied_rest[job[0]], 'eng', job[1]), recovery_jobs)
        for (i, _), value in zip(recovery_jobs, values):
            recovery[i].append(value)
    for i, readings in recovery.items():
        votes = Counter(c for value in readings if (c := code_reading(value)) and c.isdigit())
        candidate = agreed_code(readings)
        if not candidate or not candidate.isdigit() or votes[candidate] < 3:
            continue
        indices = [j for j in occupied_rest if j % 3 == i % 3]
        position = indices.index(i)
        lower = next((int(codes[('rest', j)]) for j in reversed(indices[:position]) if codes[('rest', j)]), 0)
        upper = next((int(codes[('rest', j)]) for j in indices[position + 1:] if codes[('rest', j)]), 85)
        if lower < int(candidate) < upper:
            codes[('rest', i)] = candidate
    # Long-rule removal can erase a whole narrow code even when it is legible
    # on the untouched photograph. Revisit only unresolved rest cells.
    unresolved = [i for i in occupied_rest if not codes[('rest', i)]]
    def original_code(i):
        crop = np.asarray(original_page.crop(occupied_rest[i]))
        treatments = (crop,
                      cv2.createCLAHE(clipLimit=2, tileGridSize=(4, 4)).apply(crop),
                      cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
        language = 'eng' if i % 3 < 2 else 'chi_tra'
        readings = []
        for values in treatments:
            tile = ImageOps.expand(_resize_tile(Image.fromarray(values), 96, False), border=20, fill=255)
            try:
                readings.append(''.join(word.get('text', '') for word in _tesseract_tsv(tile, psm=7, lang=language)))
            except subprocess.TimeoutExpired:
                readings.append('')
        candidate = agreed_code(readings)
        return candidate, sum(code_reading(value) == candidate for value in readings) if candidate else 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, (candidate, support) in zip(unresolved, pool.map(original_code, unresolved)):
            if not candidate:
                continue
            if i % 3 < 2:
                if not candidate.isdigit():
                    continue
                indices = [j for j in occupied_rest if j % 3 == i % 3]
                position = indices.index(i)
                lower = next((int(codes[('rest', j)]) for j in reversed(indices[:position])
                              if codes[('rest', j)] and codes[('rest', j)].isdigit()), 0)
                upper = next((int(codes[('rest', j)]) for j in indices[position + 1:]
                              if codes[('rest', j)] and codes[('rest', j)].isdigit()), 85)
                if support < 3 and not lower < int(candidate) < upper:
                    continue
            elif candidate.isdigit():
                continue
            codes[('rest', i)] = candidate
    rest = []; unknown = 0
    for i in occupied_rest:
        code = codes[('rest', i)]
        if code and code not in rest:
            rest.append(code)
        elif not code:
            unknown += 1
    # Keep the photographed three-lane positions, including unreadable cells.
    # A flat list cannot preserve alignment after even one missed code.
    rest_rows = []
    for row in range(14):
        cells = [codes.get(('rest', row * 3 + lane)) if row * 3 + lane in occupied_rest else None
                 for lane in range(3)]
        cells = [cell or '?' if row * 3 + lane in occupied_rest else None
                 for lane, cell in enumerate(cells)]
        rest_rows.append(cells)
    while rest_rows and not any(rest_rows[-1]):
        rest_rows.pop()
    jobs = [(i, key, variant) for i in occupied_leave for key in ('name', 'kind') for variant in VARIANTS]
    readings = {i: {'name': [], 'kind': []} for i in occupied_leave}
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = pool.map(lambda job: _read_name_cell(page, occupied_leave[job[0]][job[1]], *job[2]), jobs)
        for (i, key, _), value in zip(jobs, values):
            readings[i][key].append(value)
    # Global rule removal also erases parts of widely spaced kind characters.
    # For unresolved kinds, reread the untouched cell with several local contrast
    # treatments. No other fields use these alternate pixels.
    unresolved_kind = [i for i in occupied_leave if kind_reading(readings[i]['kind']) is None]
    def alternate_kinds(i):
        box = occupied_leave[i]['kind']
        crop = np.asarray(original_page.crop(box))
        contrast = cv2.createCLAHE(clipLimit=2, tileGridSize=(4, 4)).apply(crop)
        treatments = ((crop, 96), (contrast, 96),
                      (cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1], 96),
                      (contrast, 128))
        return [_read_name_cell(Image.fromarray(values), (0, 0, crop.shape[1], crop.shape[0]), height, False)
                for values, height in treatments]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, extra in zip(unresolved_kind, pool.map(alternate_kinds, unresolved_kind)):
            readings[i]['kind'].extend(extra)
    leave = [{"code": codes[('leave', i)], "name": readings[i]['name'],
              "kind": kind_reading(readings[i]['kind']), "kind_readings": readings[i]['kind']} for i in occupied_leave]
    config = Path(__file__).resolve().parents[1] / 'configs'
    directory = load_directory(config / 'staff_names.txt') if directory is None else directory
    fixed_codes = load_fixed_codes(config / 'fixed_staff_codes.json') if fixed_codes is None else fixed_codes
    inverse = {name: code for code, name in {**(names or {}), **fixed_codes}.items()}
    for entry in leave:
        name = match_names({'?': entry['name']}, directory).get('?')
        code = inverse.get(name)
        if code:
            # The same day's verified name table disambiguates a faint leave
            # code (e.g. printed 70 read as 10); rotating codes are never reused
            # from a different roster.
            entry['code'] = code
    return {"available": True, "rest": rest, "rest_rows": rest_rows, "leave": leave,
            "unreadable_rest": unknown}


def render_leave_table(photo: Path, template) -> Image.Image | None:
    page = warp_page(photo, template, color=True)
    geometry = locate_leave(page) if page is not None else None
    return page.crop(geometry['bounds']) if geometry else None
