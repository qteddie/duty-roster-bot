"""Local upload simulator for the V2 line model.

``python -m all_staff.cli demo`` starts a small web server: upload one roster
photo and it runs the same path a production upload would take — perspective
correction, 19×30 context cells, segmentation and line OCR — then shows the
whole sheet, a per-person duty list and every cell's evidence.

Nothing is sent to LINE, Discord or OpenAI, and the V1 service is untouched.
Each upload is kept under ``reports/all_staff_demo/runs/<id>/`` (photo, cells,
``result.json``) so it can be reopened from the history list.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from PIL import Image, ImageOps

from .dataset import GRID_VERSION
from .name_table import NAMES_VERSION, load_directory
from .lines import DUTY_VERSION, RECHECK_CODES
from .viewer import PAGE, common_payload, people, sheet_payload

MAX_UPLOAD = 40 * 1024 * 1024
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "MPO": ".jpg"}


class DemoError(ValueError):
    """A user-facing failure (bad image, grid not found, …)."""


class NotARoster(DemoError):
    """The picture is not a roster page at all (a selfie, a meme, a screenshot of something else).

    A group is full of such photos, so the bot treats this differently from a roster that was
    merely photographed badly.
    """


# What separates a roster from everything else that gets sent to a group: the photos in data/schedules
# read a header date and 79–98 names, with 438–556 confident codes; an avatar and the rich-menu image
# read none of the three (0 names, 7–29 confident codes). The thresholds sit far from both.
EVIDENCE_NAMES, EVIDENCE_CODES, CONFIDENT = 10, 120, .75


def roster_evidence(data: dict) -> bool:
    """Did an analysed picture read like a roster? True when it has a date, names or many confident codes."""
    if data.get("run", {}).get("roster_date"):
        return True
    named = sum(1 for readings in (data.get("names") or {}).values()
                if any(len(re.findall(r"[一-鿿]", reading)) >= 2 for reading in readings))
    confident = sum(1 for sheet in data.get("sheets", []) for cell in sheet.get("cells", [])
                    for prediction in cell.get("pred", []) if prediction.get("conf", 0) >= CONFIDENT)
    return named >= EVIDENCE_NAMES or confident >= EVIDENCE_CODES


class DemoApp:
    staff_names: Path | None = None   # None: configs/staff_names.txt (see ``_staff_files``)
    fixed_codes: Path | None = None

    def __init__(self, root: Path, recognizer: Path, *, roster: Path | None, template: Path,
                 runs: Path, device="cpu", staff_names: Path | None = None, fixed_codes: Path | None = None):
        import torch
        from .lines import LineOCR
        checkpoint = torch.load(recognizer, map_location="cpu", weights_only=False)
        self.context = checkpoint.get("context") or {"fx": .22, "fy": .12}
        self.engine = LineOCR(recognizer, roster, device=device, context=self.context)
        self.root, self.recognizer, self.roster, self.template = root, recognizer, roster, template
        self.runs = runs; runs.mkdir(parents=True, exist_ok=True)
        self.staff_names, self.fixed_codes = staff_names, fixed_codes
        self.lock = threading.Lock()
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=3)

    def _staff_files(self) -> tuple[Path, Path]:
        """The staff list and fixed codes the name and leave tables are matched with: configs/, unless the
        LINE bot passed the copies its 幹部 edit (``staff_book.live_files``)."""
        configs = self.root / "configs"
        return (self.staff_names or configs / "staff_names.txt", self.fixed_codes or configs / "fixed_staff_codes.json")

    def analyze(self, payload: bytes, name: str) -> dict:
        from .context import prepare_context
        from .schema import load_annotations
        if not payload:
            raise DemoError("沒有收到檔案")
        try:
            with Image.open(BytesIO(payload)) as probe:
                kind = probe.format; probe.verify()
        except Exception:
            raise DemoError("無法讀取這個檔案；請上傳 JPG 或 PNG 班表照片") from None
        # Not a gate any more: a roster LINE has shrunk can be missed here and still be cut below. It only tells
        # a picture without a table from a roster that could not be cut, when cutting fails.
        table = self._has_table(payload)
        started = time.time()
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
        folder = self.runs / run_id
        upload_dir = folder / "upload"; upload_dir.mkdir(parents=True)
        stem = re.sub(r"[^\w.\-]+", "_", Path(name).stem)[:60] or "upload"
        photo = upload_dir / (stem + EXTENSIONS.get(kind, ".jpg"))
        photo.write_bytes(payload)
        cells = folder / "cells"
        dating = self._pool.submit(self._roster_date, photo)   # header OCR runs beside the cell OCR
        naming = self._pool.submit(self._name_table, photo)    # so does the name-table OCR
        try:
            with self.lock:  # prepare patches roster_resnet globally; one upload at a time
                report = prepare_context(upload_dir, cells, self.template, fx=self.context["fx"],
                                         fy=self.context["fy"], only_sources=[photo])
                timings = {"prepare": round(time.time() - started, 1)}
                items = load_annotations(cells / "annotations.jsonl")
                if not items and not table:
                    raise NotARoster("這張圖片找不到班表的表格框線，看起來不是班表。")
                if not items:
                    reason = "；".join(f.get("error", "") for f in report.get("failures", [])) or \
                             "此照片可能在排除清單或無法定位格線"
                    raise DemoError(f"無法從照片切出勤務格：{reason}")
                url = lambda path: "/runs/" + path.relative_to(self.runs).as_posix()
                overview = next(iter(sorted((cells / "debug").glob("*.jpg"))), None)
                t1 = time.time()
                sheet = sheet_payload(cells, items, self.engine, self.context, url, name=name,
                                      overview=url(overview) if overview else None)
                sheet["split"] = ""
                timings["ocr"] = round(time.time() - t1, 1)
        except NotARoster:   # cleared up outside the lock, so other uploads need not wait for its OCR
            self._discard(folder, dating, naming)
            raise
        data = {**common_payload(self.recognizer, self.roster, self.context, self.root),
                "sheets": [sheet]}
        data["people"] = people(sheet, data)
        data['duty_version'] = DUTY_VERSION
        data["grid_version"] = GRID_VERSION
        # code → raw readings (the bot snaps them to known names), and the codes whose name cell is blank on the sheet
        data["names"], data["names_blank"] = naming.result()
        data["names_version"] = NAMES_VERSION
        data["run"] = {"id": run_id, "name": name, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "seconds": round(time.time() - started, 1), "timings": timings,
                       "photo": url(photo), "codes": sum(len(c["pred"]) for c in sheet["cells"]),
                       "roster_date": dating.result()}
        if not roster_evidence(data):   # something with lines in it, but no date, no names and hardly any codes
            self._discard(folder)
            raise NotARoster("這張圖片讀不到班表的日期、姓名或勤務代號，看起來不是班表。")
        (folder / "result.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    @staticmethod
    def _discard(folder: Path, *running):
        """Remove what an upload that is not a roster left on disk, once the OCR still reading it has finished."""
        for future in running:
            try:
                future.result()
            except Exception:
                pass
        shutil.rmtree(folder, ignore_errors=True)

    def _has_table(self, payload: bytes) -> bool:
        """Can the roster table's ruling be found in the picture? Only a clear 「no table here」 says no.

        Anything that goes wrong in the check lets the picture through to the full analysis.
        """
        from backend.phase1_cell_pipeline import GridTemplate, _registration_corners
        try:
            with Image.open(BytesIO(payload)) as raw:
                image = ImageOps.exif_transpose(raw).convert("RGB")
            corners, _ = _registration_corners(image, GridTemplate.from_json(self.template))
        except Exception as exc:
            print(f"檢查表格框線時出錯，照常處理：{type(exc).__name__}: {exc}", flush=True)
            return True
        return corners is not None

    def _roster_date(self, photo: Path) -> str | None:
        """The date printed in the roster header (ISO), or None when it cannot be read."""
        from backend.phase1_cell_pipeline import GridTemplate
        from .bot import today
        from .header_date import read_header_date
        try:
            day = read_header_date(photo, GridTemplate.from_json(self.template), today())
        except Exception as exc:
            print(f"讀取班表日期失敗：{type(exc).__name__}: {exc}", flush=True)
            return None
        return day.isoformat() if day else None

    def ensure_grid(self, run_id: str, *, force: bool = False) -> dict:
        """Read a saved run's cells again with the current grid (``GRID_VERSION``) and save the result.

        Rosters saved before the last four columns (A1A3站, 內機, 內部管理, 全區帶勤) were read, and before the
        text of notes and call-outs laid over the table was ignored, are read again from their saved photo.
        Name table, leave table and dates are kept. The new cells are built next to the old ones and only
        swapped in once complete, so a run that cannot be read again is left exactly as it was.
        """
        from .context import prepare_context
        from .schema import load_annotations
        with self.lock:
            folder = self.runs / run_id
            path = folder / "result.json"
            data = self.result(run_id)
            if data.get("grid_version") == GRID_VERSION and not force:
                return data
            photo = next(iter(sorted((folder / "upload").glob("*"))), None)
            if photo is None:
                raise DemoError("找不到這次辨識的原始照片")
            cells, fresh, previous = folder / "cells", folder / "cells.next", folder / "cells.old"
            temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
            for leftover in (fresh, previous):
                shutil.rmtree(leftover, ignore_errors=True)
            try:
                prepare_context(folder / "upload", fresh, self.template, fx=self.context["fx"],
                                fy=self.context["fy"], only_sources=[photo])
                items = load_annotations(fresh / "annotations.jsonl")
                if not items:
                    raise DemoError("無法從照片切出勤務格")
                url = lambda target: "/runs/" + (cells / target.relative_to(fresh)).relative_to(self.runs).as_posix()
                overview = next(iter(sorted((fresh / "debug").glob("*.jpg"))), None)
                sheet = sheet_payload(fresh, items, self.engine, self.context, url,
                                      name=data.get("run", {}).get("name") or photo.name,
                                      overview=url(overview) if overview else None)
                sheet["split"] = ""
                data = {**data, **common_payload(self.recognizer, self.roster, self.context, self.root),
                        "sheets": [sheet]}
                data["people"] = people(sheet, data)
                data["duty_version"], data["grid_version"] = DUTY_VERSION, GRID_VERSION
                data["run"] = {**data.get("run", {}), "codes": sum(len(c["pred"]) for c in sheet["cells"]),
                               "regridded": time.strftime("%Y-%m-%d %H:%M:%S")}
                temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                if cells.exists():
                    cells.replace(previous)
                try:
                    fresh.replace(cells)
                except BaseException:
                    if previous.exists():
                        previous.replace(cells)
                    raise
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
                shutil.rmtree(fresh, ignore_errors=True)
            shutil.rmtree(previous, ignore_errors=True)
            for derived in ("people", "slots", "works"):   # pictures drawn from the old cell positions
                shutil.rmtree(folder / derived, ignore_errors=True)
            return data

    def upgrade_runs(self, *, force: bool = False, only: list[str] | None = None, limit: int | None = None,
                     progress=print) -> dict:
        """Read every saved run that was made with an older grid again, newest first, one at a time.

        ``limit`` caps how many runs are read again in this call. A run that cannot be read again keeps its old
        result and is not tried again (its reason is kept in ``regrid-v<n>.failed``) until ``force``.
        """
        report = {"upgraded": [], "current": [], "failed": {}}
        for path in sorted(self.runs.glob("*/result.json"), reverse=True):
            run_id = path.parent.name
            if only and run_id not in only:
                continue
            if limit is not None and len(report["upgraded"]) + len(report["failed"]) >= limit:
                break
            try:
                grid = json.loads(path.read_text(encoding="utf-8")).get("grid_version")
            except (OSError, ValueError):
                continue
            if grid == GRID_VERSION and not force:
                report["current"].append(run_id); continue
            marker = path.parent / f"regrid-v{GRID_VERSION}.failed"
            if marker.is_file() and not force:
                report["failed"][run_id] = marker.read_text(encoding="utf-8"); continue
            started = time.time()
            try:
                self.ensure_grid(run_id, force=force)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                marker.write_text(reason, encoding="utf-8")
                report["failed"][run_id] = reason
                progress(f"重讀失敗（{run_id}）：{reason}")
                continue
            marker.unlink(missing_ok=True)
            report["upgraded"].append(run_id)
            progress(f"已重讀 {run_id}（{time.time() - started:.0f} 秒）")
        return report

    def ensure_names(self, run_id: str, *, force: bool = False) -> dict:
        """Read (or re-read) the name table of a stored run from its saved photo and save it.

        Returns {"names": …, "names_blank": …, "names_version": …}; runs from before the name table was read,
        or read by an older version, get current readings. Runs saved before blank cells were told apart have
        no blank codes, and are not read again for them.
        """
        with self.lock:
            folder = self.runs / run_id
            path = folder / "result.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("names_version") == NAMES_VERSION and not force:
                return {"names": data.get("names", {}), "names_blank": data.get("names_blank", []),
                        "names_version": NAMES_VERSION}
            photo = next(iter(sorted((folder / "upload").glob("*"))), None)
            if photo is None:
                raise DemoError("找不到這次辨識的原始照片")
            data["names"], data["names_blank"] = self._name_table(photo)
            data["names_version"] = NAMES_VERSION
            temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
            return {"names": data["names"], "names_blank": data["names_blank"], "names_version": NAMES_VERSION}

    def ensure_duties(self, run_id: str) -> dict:
        """Recheck saved 1/11, 2/22 and 3/33 cells once; retain the dates and name table."""
        from types import SimpleNamespace
        from .viewer import cell_payload
        with self.lock:
            path = self.runs / run_id / 'result.json'
            data = self.result(run_id)
            if data.get('duty_version') == DUTY_VERSION:
                return data
            for sheet in data['sheets']:
                for index, cell in enumerate(sheet['cells']):
                    if cell.get('overlay'):   # inside a note / call-out: what is read there is no duty
                        continue
                    if not any(line['text'] in RECHECK_CODES for line in cell.get('lines', [])):
                        continue
                    image_path = self.runs / run_id / 'cells/images' / Path(cell['img']).name
                    with Image.open(image_path) as image:
                        item = SimpleNamespace(sample_id=cell['id'], row=cell['r'], column=cell['c'], reviewed=False)
                        updated = cell_payload(item, image.copy(), self.engine, self.context, cell['img'])
                    if 'truth' in cell:
                        updated['truth'] = cell['truth']
                    sheet['cells'][index] = updated
            # Production uploads contain one sheet. Combine any additional saved sheets.
            combined = {'cells': [c for sheet in data['sheets'] for c in sheet['cells']]}
            data['people'] = people(combined, data)
            data['run']['codes'] = sum(len(c['pred']) for c in combined['cells'])
            data['duty_version'] = DUTY_VERSION
            temporary = path.with_name(f'{path.name}.{uuid4().hex}.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path)
            return data

    def _name_table(self, photo: Path) -> tuple[dict, list[str]]:
        """(code → raw name readings, codes whose name cell is blank) from the 「姓名代碼對照表」 (``read_name_cells``;
        ({}, []) when it cannot be read)."""
        from backend.phase1_cell_pipeline import GridTemplate
        from .name_table import read_name_cells
        try:
            return read_name_cells(photo, GridTemplate.from_json(self.template),
                                   directory=load_directory(self._staff_files()[0]))
        except Exception as exc:
            print(f"讀取姓名對照表失敗：{type(exc).__name__}: {exc}", flush=True)
            return {}, []

    def ensure_leave(self, run_id: str) -> dict:
        """Read the printed leave table once, including for rosters uploaded before this feature."""
        from backend.phase1_cell_pipeline import GridTemplate
        from .leave_table import LEAVE_VERSION, read_leave_table
        with self.lock:
            folder = self.runs / run_id
            path = folder / "result.json"
            data = self.result(run_id)
            if data.get("leave_table_version") == LEAVE_VERSION and "leave_table" in data:
                return data["leave_table"]
            photo = next(iter(sorted((folder / "upload").glob("*"))), None)
            if photo is None:
                return {"available": False}
            from .name_table import load_fixed_codes, match_names
            directory = load_directory(self._staff_files()[0])
            fixed = load_fixed_codes(self._staff_files()[1])
            names = match_names(data.get("names", {}), directory, fixed_codes=fixed)
            leave = read_leave_table(photo, GridTemplate.from_json(self.template), names=names,
                                     directory=directory, fixed_codes=fixed)
            data.update(leave_table=leave, leave_table_version=LEAVE_VERSION)
            temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
            return leave

    def leave_table_image(self, run_id: str) -> tuple[str, str] | None:
        from backend.phase1_cell_pipeline import GridTemplate
        from .leave_table import render_leave_table
        from .evidence import write_person_images
        self.result(run_id)
        folder = self.runs / run_id
        original, preview = folder / "leave-table-v1.jpg", folder / "leave-table-v1-preview.jpg"
        relative = lambda p: p.relative_to(self.runs).as_posix()
        with self.lock:
            if original.is_file() and preview.is_file():
                return relative(original), relative(preview)
            photo = next(iter(sorted((folder / "upload").glob("*"))), None)
            if photo is None:
                return None
            image = render_leave_table(photo, GridTemplate.from_json(self.template))
            if image is None:
                return None
            write_person_images(image, original, preview)
            return relative(original), relative(preview)

    def person_image(self, run_id: str, code: str) -> tuple[str, str] | None:
        """Blue-outlined roster photo of one person's cells: (original, preview) paths under runs/.

        Rendered on first request and cached in ``runs/<id>/people/``. ``None`` when the
        code has no cells in that roster or the run predates geometry saving.
        """
        from .evidence import render_person, write_person_images
        result = self.result(run_id)
        person = next((p for p in result.get("people", []) if p["code"] == code), None)
        if not person or not person.get("slots"):
            return None
        stem = code if re.fullmatch(r"[0-9A-Za-z]+", code) else "u" + "".join(f"{ord(ch):x}" for ch in code)
        if result.get('duty_version') == DUTY_VERSION:
            stem += f'-digits-v{DUTY_VERSION}'  # LINE must fetch the newly corrected highlight image
        if result.get('grid_version') == GRID_VERSION:
            stem += f'-g{GRID_VERSION}'         # ... and the one drawn on the re-read cells
        folder = self.runs / run_id / "people"
        original, preview = folder / f"{stem}.jpg", folder / f"{stem}-preview.jpg"
        relative = lambda path: path.relative_to(self.runs).as_posix()
        if original.is_file() and preview.is_file():
            return relative(original), relative(preview)
        cells = self.runs / run_id / "cells"
        geometry_path = next(iter(sorted((cells / "geometry").glob("*.json"))), None)
        if geometry_path is None:
            return None
        corrected_path = cells / "corrected" / f"{geometry_path.stem}.jpg"
        if not corrected_path.is_file():
            return None
        geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
        with Image.open(corrected_path) as corrected:
            image = render_person(corrected, geometry, [(s["row"], s["column"]) for s in person["slots"]])
        if image is None:
            return None
        write_person_images(image, original, preview)
        return relative(original), relative(preview)

    def roster_image(self, run_id: str) -> tuple[str, str] | None:
        """Use the uploaded file itself when LINE accepts it, plus a small preview."""
        from .evidence import write_roster_images
        self.result(run_id)
        folder = self.runs / run_id
        original, preview = folder / "roster-v1.jpg", folder / "roster-v1-preview.jpg"
        relative = lambda path: path.relative_to(self.runs).as_posix()
        with self.lock:
            photo = next(iter(sorted((folder / "upload").glob("*"))), None)
            if not (original.is_file() and preview.is_file()):
                if photo is None:
                    return None
                write_roster_images(photo, original, preview)
            source = photo if photo and photo.suffix.lower() in (".jpg", ".jpeg", ".png") and \
                photo.stat().st_size <= 10_000_000 else original
            return relative(source), relative(preview)

    def slot_image(self, run_id: str, row: int, area: str) -> tuple[str, str] | None:
        """Work headers and one hour of the selected area; also works for older stored runs."""
        from .bot import AREAS, FIRST_ROW, LAST_ROW
        from .evidence import render_slot
        result = self.result(run_id)  # validates the run ID and checks it exists
        if area not in AREAS or not FIRST_ROW <= row <= LAST_ROW:
            raise DemoError("時段或區域不正確")
        stem = f"h{row + 1:02d}-{row + 2:02d}-{area}-v1"
        if result.get("grid_version") == GRID_VERSION:
            stem += f"-g{GRID_VERSION}"
        return self._grid_crop(run_id, "slots", stem,
                               lambda corrected, geometry: render_slot(corrected, geometry, row, AREAS[area][1]))

    def work_image(self, run_id: str, columns, rows=None) -> tuple[str, str] | None:
        """Work headers and every hour of one work's ``columns``, from the first to the last of ``rows``.

        ``rows`` defaults to the whole day. Cached in ``runs/<id>/works/`` under the columns and hours shown,
        so a re-read that changes a work's staffed hours gets a new picture and a new URL.
        """
        from .bot import FIRST_ROW, LAST_ROW
        from .dataset import FULL_DUTY_COLUMNS
        from .evidence import render_work
        result = self.result(run_id)  # validates the run ID and checks it exists
        columns = sorted({int(column) for column in columns})
        rows = sorted({int(row) for row in rows}) if rows is not None else [FIRST_ROW, LAST_ROW]
        if not columns or not set(columns) <= set(FULL_DUTY_COLUMNS) or \
                not rows or rows[0] < FIRST_ROW or rows[-1] > LAST_ROW:
            raise DemoError("工作欄位或時段不正確")
        first, last = rows[0], rows[-1]
        stem = "c" + "_".join(f"{column:02d}" for column in columns) + f"-h{first + 1:02d}-{last + 2:02d}-v1"
        if result.get("grid_version") == GRID_VERSION:
            stem += f"-g{GRID_VERSION}"
        return self._grid_crop(run_id, "works", stem, lambda corrected, geometry:
                               render_work(corrected, geometry, columns, range(first, last + 1)))

    def _grid_crop(self, run_id: str, kind: str, stem: str, render) -> tuple[str, str] | None:
        """A crop of the corrected roster, ``render(corrected, geometry)``, cached as runs/<id>/<kind>/<stem>.jpg.

        Shared by 查看時段 and 查看工作; also works for older stored runs, whose geometry has no headers.
        ``None`` when the run kept no corrected photo or the crop needs a cell the geometry does not have.
        """
        from .bot import FIRST_ROW, LAST_ROW
        from .dataset import FIRST_DUTY_COLUMN, FULL_DUTY_COLUMNS, LAST_DUTY_COLUMN
        from .evidence import geometry_payload, write_person_images
        folder = self.runs / run_id / kind
        original, preview = folder / f"{stem}.jpg", folder / f"{stem}-preview.jpg"
        relative = lambda path: path.relative_to(self.runs).as_posix()
        with self.lock:
            if original.is_file() and preview.is_file():
                return relative(original), relative(preview)
            cells = self.runs / run_id / "cells"
            geometry_path = next(iter(sorted((cells / "geometry").glob("*.json"))), None)
            if geometry_path is None:
                return None
            corrected_path = cells / "corrected" / f"{geometry_path.stem}.jpg"
            if not corrected_path.is_file():
                return None
            geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
            with Image.open(corrected_path) as corrected:
                if not geometry.get("headers"):
                    # Old runs saved duty cells only. Register their corrected photo to locate headers/time.
                    from backend.phase1_cell_pipeline import GridTemplate
                    from .grid import register
                    template = GridTemplate.from_json(self.template)
                    registration = register(corrected, template.grid_x, template.grid_y,
                                            rows=(FIRST_ROW, LAST_ROW),
                                            columns=(FIRST_DUTY_COLUMN, LAST_DUTY_COLUMN))
                    geometry = {**geometry_payload(registration, range(FIRST_ROW, LAST_ROW + 1), FULL_DUTY_COLUMNS),
                                "cells": geometry["cells"]}
                image = render(corrected, geometry)
            if image is None:
                return None
            write_person_images(image, original, preview)
            return relative(original), relative(preview)

    def staff_table_image(self, run_id: str) -> tuple[str, str] | None:
        """The original daily staff name/code table, cached under this roster's run."""
        from backend.phase1_cell_pipeline import GridTemplate
        from .evidence import render_staff_table, write_person_images
        self.result(run_id)
        folder = self.runs / run_id
        original, preview = folder / "staff-table-v1.jpg", folder / "staff-table-v1-preview.jpg"
        relative = lambda path: path.relative_to(self.runs).as_posix()
        with self.lock:
            if original.is_file() and preview.is_file():
                return relative(original), relative(preview)
            photo = next(iter(sorted((folder / "upload").glob("*"))), None)
            if photo is None:
                return None
            image = render_staff_table(photo, GridTemplate.from_json(self.template))
            if image is None:
                return None
            write_person_images(image, original, preview)
            return relative(original), relative(preview)

    def history(self, limit=50) -> list[dict]:
        output = []
        for path in sorted(self.runs.glob("*/result.json"), reverse=True)[:limit]:
            try:
                run = json.loads(path.read_text(encoding="utf-8"))["run"]
                output.append({k: run[k] for k in ("id", "name", "created", "codes")})
            except Exception:
                continue
        return output

    def result(self, run_id: str) -> dict:
        if not re.fullmatch(r"[\w\-]+", run_id):
            raise DemoError("紀錄編號無效")
        path = self.runs / run_id / "result.json"
        if not path.is_file():
            raise DemoError("找不到這筆紀錄")
        return json.loads(path.read_text(encoding="utf-8"))


def make_handler(app: DemoApp):
    page = PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def send(self, payload: bytes, content_type="application/json; charset=utf-8", status=200):
            self.send_response(status); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(payload)

        def send_json(self, payload, status=200):
            self.send(json.dumps(payload, ensure_ascii=False).encode(), status=status)

        def do_GET(self):
            route = urlparse(self.path).path
            if route in ("/", "/index.html"):
                return self.send(page, "text/html; charset=utf-8")
            if route == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT); self.end_headers(); return
            if route == "/data.js":
                return self.send(b"window.DEMO_MODE = true;\n", "text/javascript; charset=utf-8")
            if route == "/api/runs":
                return self.send_json(app.history())
            if route.startswith("/api/runs/"):
                try:
                    return self.send_json(app.result(unquote(route[len("/api/runs/"):])))
                except DemoError as exc:
                    return self.send_json({"error": str(exc)}, 404)
            if route.startswith("/runs/"):
                path = (app.runs / unquote(route[len("/runs/"):])).resolve()
                try:
                    path.relative_to(app.runs.resolve())
                except ValueError:
                    return self.send_error(HTTPStatus.FORBIDDEN)
                if not path.is_file():
                    return self.send_error(HTTPStatus.NOT_FOUND)
                kind = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".webp": "image/webp", ".json": "application/json"}.get(path.suffix.lower(),
                                                                             "application/octet-stream")
                return self.send(path.read_bytes(), kind)
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/api/analyze":
                return self.send_error(HTTPStatus.NOT_FOUND)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD:
                return self.send_json({"error": "檔案超過 40 MB"}, 413)
            name = parse_qs(parsed.query).get("name", ["upload.jpg"])[0]
            try:
                return self.send_json(app.analyze(self.rfile.read(length), name))
            except DemoError as exc:
                return self.send_json({"error": str(exc)}, 422)
            except Exception as exc:  # keep the server alive and show the reason
                traceback.print_exc()
                return self.send_json({"error": f"辨識失敗：{exc}"}, 500)

        def log_message(self, fmt, *args):
            # args[0] is the request line for access logs but an HTTPStatus for log_error
            message = fmt % args
            if "/api/" in message or not str(args[0] if args else "").startswith(("GET", "POST")):
                print(f"{self.address_string()} {message}", flush=True)

    return Handler


def serve(app: DemoApp, host="127.0.0.1", port=8776, open_browser=True):
    server = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}"
    print(f"全員辨識上傳模擬：{url}（Ctrl+C 結束）", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
