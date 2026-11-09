"""Boxes laid over the roster grid: merged-cell notes and floating call-outs.

Some rosters carry text boxes on top of the table — 「8-12時防爆訓練：72.73」 or
「12-15專案：81.28.K.F.L」 as a merged block of cells, or a speech bubble such as 「丁帶領替代役
新生認識環境17-19」 with a pointer aimed at one cell. They hide the printed rules under them, and
the line recogniser reads their sentences as duty codes ("72", "卯", "A" ...), which then show up as
phantom hours for real people.

A real duty code always sits in a cell that is closed on every side, so a block of cells whose shared
rules have all vanished is not part of the table: nothing read there is a duty. That is what this
module looks for, on the already registered page:

1. ``rule_visibility`` measures, for every rule segment between two cells, which share of it is still
   visible (long dark strokes along the registered rule; glyph strokes are too short to count).
2. ``find_boxes`` looks for rectangles of at least four cells, two columns wide, whose *inner* rules
   are all gone (most of them completely).  A single missing segment — a small white label with a
   code or two on it, a faint rule, a pen stroke — never forms such a rectangle.
3. A pointer follows the cells whose rules are partly hidden away from the box; the cell the pointer
   touches is read as a code ("A" from the arrow head) but only if the recogniser was unsure.

The module only describes the overlays; ``Overlays.apply`` moves the predictions of a cell payload out
of ``pred`` so that every consumer (people list, bot, counts) sees a table without them.  The ignored
predictions stay in ``cell["ignored"]`` for the viewer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

VERSION = 1
OPEN, WEAK = .5, .85       # visible share of a rule segment: below OPEN it is gone, below WEAK it is damaged
MIN_CELLS, MIN_COLUMNS, MAX_CELLS = 4, 2, 40
MAX_TOTAL_CELLS = 80       # boxes together cover at most this many cells (of 665); more means the ruling is lost, not boxed
POINTER_CONFIDENCE = .8    # in a pointer cell only readings below this are dropped
POINTER_DEPTH = 6
UNRELIABLE_SHARE = .05     # more than this share of missing inner rules outside the boxes: the registration is off
SAMPLE_HALF_WIDTH = 3      # px either side of the fitted rule where its ink is looked for
BOUNDARY_VISIBLE = .5      # at least this share of a box's outline rules must still be visible


@dataclass(frozen=True)
class Box:
    rows: tuple[int, int]
    columns: tuple[int, int]

    def cells(self) -> set[tuple[int, int]]:
        return {(r, c) for r in range(self.rows[0], self.rows[1] + 1)
                for c in range(self.columns[0], self.columns[1] + 1)}


@dataclass
class Visibility:
    """Visible share (0–1) of each rule segment between two cells of the duty table.

    ``vertical[(row, rule)]``: rule *c* is the left edge of column *c* (right edge of column *c*-1), looked at
    along ``row``. ``horizontal[(rule, column)]``: rule *r* is the top edge of row *r*, looked at along ``column``.
    """
    rows: tuple[int, int]
    columns: tuple[int, int]
    vertical: dict[tuple[int, int], float] = field(default_factory=dict)
    horizontal: dict[tuple[int, int], float] = field(default_factory=dict)

    def inner(self, rows: tuple[int, int], columns: tuple[int, int]) -> list[float]:
        """Shares of the rule segments strictly inside a block of cells."""
        r0, r1 = rows; c0, c1 = columns
        return ([self.vertical[(r, k)] for r in range(r0, r1 + 1) for k in range(c0 + 1, c1 + 1)] +
                [self.horizontal[(r, c)] for r in range(r0 + 1, r1 + 1) for c in range(c0, c1 + 1)])

    def outline(self, rows: tuple[int, int], columns: tuple[int, int]) -> list[float]:
        r0, r1 = rows; c0, c1 = columns
        return ([self.vertical[(r, c0)] for r in range(r0, r1 + 1)] +
                [self.vertical[(r, c1 + 1)] for r in range(r0, r1 + 1)] +
                [self.horizontal[(r0, c)] for c in range(c0, c1 + 1)] +
                [self.horizontal[(r1 + 1, c)] for c in range(c0, c1 + 1)])


def rule_visibility(registration, *, rows: tuple[int, int], columns: tuple[int, int]) -> Visibility:
    """Which share of every printed rule between two cells can still be seen on the registered page."""
    from .grid import rule_masks
    gray = np.asarray(registration.image.convert("L"))
    height, width = gray.shape
    first_row, last_row = rows; first_col, last_col = columns
    middle = (registration.y_at(first_row, width / 2) + registration.y_at(last_row + 1, width / 2)) / 2
    pitch_x = (registration.x_at(last_col + 1, middle) - registration.x_at(first_col, middle)) / (last_col - first_col + 1)
    pitch_y = (registration.y_at(last_row + 1, width / 2) - registration.y_at(first_row, width / 2)) / (last_row - first_row + 1)
    vertical, horizontal = rule_masks(gray, pitch_x, pitch_y)
    result = Visibility(rows, columns)
    offsets = range(-SAMPLE_HALF_WIDTH, SAMPLE_HALF_WIDTH + 1)

    def share(mask, curve, start, stop, along_y: bool) -> float:
        samples = np.arange(int(np.ceil(start)), int(np.floor(stop)) + 1)
        if len(samples) < 4:
            return 1.0
        across = np.polyval(curve, samples)
        limit = (width, height) if along_y else (height, width)
        if across.min() < 0 or across.max() > limit[0] - 1 or samples.min() < 0 or samples.max() > limit[1] - 1:
            return 1.0   # that part of the table lies outside the photo: nothing to judge
        hit = np.zeros(len(samples), bool)
        for offset in offsets:
            index = np.round(across + offset).astype(int).clip(0, limit[0] - 1)
            hit |= (mask[samples, index] if along_y else mask[index, samples]) > 0
        return float(hit.mean())

    for row in range(first_row, last_row + 1):
        for rule in range(first_col, last_col + 2):
            top, bottom = registration.corner(rule, row)[1], registration.corner(rule, row + 1)[1]
            result.vertical[(row, rule)] = share(vertical, registration.x_curves[rule], top + 3, bottom - 3, True)
    for rule in range(first_row, last_row + 2):
        for column in range(first_col, last_col + 1):
            left, right = registration.corner(column, rule)[0], registration.corner(column + 1, rule)[0]
            result.horizontal[(rule, column)] = share(horizontal, registration.y_curves[rule], left + 3, right - 3, False)
    return result


def _components(cells: set[tuple[int, int]], visibility: Visibility, limit: float) -> list[set[tuple[int, int]]]:
    """Cells joined through shared rule segments that are visible for less than ``limit``."""
    def neighbours(cell):
        row, column = cell
        if visibility.vertical.get((row, column), 1) < limit and (row, column - 1) in cells:
            yield (row, column - 1)
        if visibility.vertical.get((row, column + 1), 1) < limit and (row, column + 1) in cells:
            yield (row, column + 1)
        if visibility.horizontal.get((row, column), 1) < limit and (row - 1, column) in cells:
            yield (row - 1, column)
        if visibility.horizontal.get((row + 1, column), 1) < limit and (row + 1, column) in cells:
            yield (row + 1, column)
    seen: set[tuple[int, int]] = set(); output = []
    for start in sorted(cells):
        if start in seen:
            continue
        group, stack = {start}, [start]; seen.add(start)
        while stack:
            for other in neighbours(stack.pop()):
                if other not in seen:
                    seen.add(other); group.add(other); stack.append(other)
        output.append(group)
    return output


def find_boxes(visibility: Visibility) -> list[Box]:
    """Rectangles of cells without any inner rule, the largest first; never overlapping."""
    r_lo, r_hi = visibility.rows; c_lo, c_hi = visibility.columns
    cells = {(r, c) for r in range(r_lo, r_hi + 1) for c in range(c_lo, c_hi + 1)}
    candidates = []
    for group in _components(cells, visibility, WEAK):
        if len(group) < MIN_CELLS:
            continue
        rs = sorted({r for r, _ in group}); cs = sorted({c for _, c in group})
        for r0 in rs:
            for r1 in (r for r in rs if r >= r0):
                for c0 in cs:
                    for c1 in (c for c in cs if c >= c0 + MIN_COLUMNS - 1):
                        count = (r1 - r0 + 1) * (c1 - c0 + 1)
                        if not MIN_CELLS <= count <= MAX_CELLS:
                            continue
                        inner = visibility.inner((r0, r1), (c0, c1))
                        if len(inner) < 3 or max(inner) >= WEAK:
                            continue
                        strong = sum(1 for v in inner if v < OPEN) / len(inner)
                        if strong < .5:
                            continue
                        outline = visibility.outline((r0, r1), (c0, c1))
                        if sum(1 for v in outline if v >= WEAK) / len(outline) < BOUNDARY_VISIBLE:
                            continue   # the box's own frame is gone as well: a registration problem, not a box
                        candidates.append((count, strong, r0, r1, c0, c1))
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2], item[4]))
    taken: set[tuple[int, int]] = set(); boxes = []
    for count, _, r0, r1, c0, c1 in candidates:
        box = Box((r0, r1), (c0, c1))
        if box.cells() & taken:
            continue
        taken |= box.cells(); boxes.append(box)
    return boxes


def pointer_cells(visibility: Visibility, boxes: list[Box]) -> set[tuple[int, int]]:
    """Cells under a call-out's pointer, plus the cells it points at (just beyond its far end)."""
    r_lo, r_hi = visibility.rows; c_lo, c_hi = visibility.columns
    cells = {(r, c) for r in range(r_lo, r_hi + 1) for c in range(c_lo, c_hi + 1)}
    inside = set().union(*(box.cells() for box in boxes)) if boxes else set()
    result: set[tuple[int, int]] = set()
    for box in boxes:
        def distance(cell, box=box):
            return max(box.rows[0] - cell[0], cell[0] - box.rows[1], box.columns[0] - cell[1],
                       cell[1] - box.columns[1], 0)
        # cells joined to the box through damaged rules, walking away from it
        chain: dict[tuple[int, int], int] = {}
        frontier = [cell for cell in box.cells()]
        for depth in range(1, POINTER_DEPTH + 1):
            grown = []
            for row, column in frontier:
                for other, rule in (((row, column - 1), visibility.vertical.get((row, column), 1)),
                                    ((row, column + 1), visibility.vertical.get((row, column + 1), 1)),
                                    ((row - 1, column), visibility.horizontal.get((row, column), 1)),
                                    ((row + 1, column), visibility.horizontal.get((row + 1, column), 1))):
                    if other in cells and other not in inside and other not in chain and rule < WEAK:
                        chain[other] = depth; grown.append(other)
            frontier = grown
            if not frontier:
                break
        # a pointer is a few cells long; a big damaged area is something else
        if not chain or len(chain) > 3 * POINTER_DEPTH:
            continue
        result |= set(chain)
        for (row, column), depth in chain.items():
            if depth < 2:   # a rule hidden right beside the box is its shadow, not a pointer
                continue
            for other in ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)):
                if other in cells and other not in inside and other not in chain and distance(other) > distance((row, column)):
                    result.add(other)
    return result - inside


@dataclass
class Overlays:
    boxes: list[Box] = field(default_factory=list)
    pointer: set[tuple[int, int]] = field(default_factory=set)
    unreliable: bool = False
    open_share: float = 0.0

    def in_box(self, row: int, column: int) -> bool:
        return any(b.rows[0] <= row <= b.rows[1] and b.columns[0] <= column <= b.columns[1] for b in self.boxes)

    def apply(self, cell: dict) -> bool:
        """Move the readings that belong to an overlay out of ``cell["pred"]``; True when anything moved."""
        key = (cell["r"], cell["c"])
        if self.in_box(*key):
            kind, dropped = "box", list(cell["pred"])
        elif key in self.pointer:
            kind, dropped = "pointer", [p for p in cell["pred"] if p["conf"] < POINTER_CONFIDENCE]
        else:
            return False
        cell["overlay"] = kind
        if dropped:
            cell["ignored"] = dropped
            cell["pred"] = [p for p in cell["pred"] if p not in dropped]
        return bool(dropped)

    def to_dict(self) -> dict:
        return {"version": VERSION, "unreliable": self.unreliable, "open_share": round(self.open_share, 4),
                "boxes": [{"rows": list(b.rows), "columns": list(b.columns)} for b in self.boxes],
                "pointer": sorted([r, c] for r, c in self.pointer)}

    @classmethod
    def from_dict(cls, payload: dict) -> "Overlays":
        return cls(boxes=[Box(tuple(b["rows"]), tuple(b["columns"])) for b in payload.get("boxes", [])],
                   pointer={(r, c) for r, c in payload.get("pointer", [])},
                   unreliable=bool(payload.get("unreliable")), open_share=float(payload.get("open_share", 0)))

    def summary(self) -> str:
        if not self.boxes:
            return "none"
        return "；".join(f"列{b.rows[0]}–{b.rows[1]}×欄{b.columns[0]}–{b.columns[1]}" for b in self.boxes)


def detect(registration, *, rows: tuple[int, int], columns: tuple[int, int]) -> Overlays:
    """Overlay boxes (and their pointers) on a registered roster page."""
    visibility = rule_visibility(registration, rows=rows, columns=columns)
    boxes = find_boxes(visibility)
    inside = set().union(*(box.cells() for box in boxes)) if boxes else set()
    # how much of the table's inner ruling is gone outside the boxes: a lot means the registration is unreliable
    inner = [v for (row, rule), v in visibility.vertical.items()
             if columns[0] < rule <= columns[1] and (row, rule - 1) not in inside and (row, rule) not in inside]
    inner += [v for (rule, column), v in visibility.horizontal.items()
              if rows[0] < rule <= rows[1] and (rule - 1, column) not in inside and (rule, column) not in inside]
    share = sum(1 for v in inner if v < OPEN) / max(1, len(inner))
    if share > UNRELIABLE_SHARE or len(inside) > MAX_TOTAL_CELLS:
        # a faded photo or a registration that is off can also be "tiled" with boxes: never discard that much
        return Overlays(unreliable=True, open_share=max(share, len(inside) / max(1, len(visibility.vertical))))
    return Overlays(boxes=boxes, pointer=pointer_cells(visibility, boxes), open_share=share)


def save(overlays: Overlays, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(overlays.to_dict()), encoding="utf-8")


def load(dataset: Path, group: str) -> Overlays | None:
    """The overlays recorded for one photo of a dataset; ``None`` when none were recorded (older runs)."""
    path = Path(dataset) / "overlays" / f"{group[:16]}.json"
    if not path.is_file():
        return None
    try:
        return Overlays.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, KeyError, TypeError):
        return None
