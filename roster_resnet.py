#!/usr/bin/env python3
"""Prepare, label, train, evaluate, and run the local roster ResNet."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import shutil
import tempfile
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse
from uuid import uuid4

from PIL import Image, ImageDraw

from backend.resnet_classifier import LABELS, RosterResNetClassifier, build_resnet18, prepare_image
from backend.phase1_cell_pipeline import (
    GridTemplate,
    _complete_regular_grid_lines,
    _pixel_lines,
    align_semantic_row_lines,
    analysis_time_window,
    cell_polygon,
    correct_perspective,
    crop_mapped_cells,
    detect_grid_lines,
    registered_column_models,
    registered_grid_lines,
    registered_row_models,
    validate_grid_alignment,
)

ROOT = Path(__file__).resolve().parent
FIELDS = ("sample_id", "image", "source_group", "row", "column", "split",
          "ding", "wu", "ji", "geng")
LABEL_FIELDS = dict(zip(LABELS, ("ding", "wu", "ji", "geng")))

# Photos intentionally excluded from the human-label dataset. Keep the raw
# source file in data/schedules, but do not recreate its cells on later ingest.
EXCLUDED_SOURCE_GROUPS = {
    "1a76ea0031aad6db",  # 9_11.jpg (formerly UI sheet 2)
    "79515628d851c6e1",  # 9_20.jpg (formerly UI sheet 5)
}
# These source photos are UI sheets 7, 8, and 12. Their detector omits one
# pre-body header rule, so physical hourly lines are one index earlier than the
# template's semantic row numbers.
ROW_INDEX_UP_ONE_GROUPS = {
    "79515628d851c6e1",  # 9_20.jpg (UI sheet 6)
    "169780f6061eeca3",  # 9_2.jpg (UI sheet 1)
    "820e94ac45798bf3",  # 9_21.jpg
    "8568fb6f36f29e96",  # 9_22.jpg
    "8822f93c8ae8b94b",  # 9_9.JPG (UI sheet 9)
    "d5627424ffb815ef",  # 9_13.jpg
}

# UI sheet 6 starts two detected physical columns earlier than the template's
# semantic indexes. Without this correction every selected duty is two cells
# to the right even though the detected rules themselves are sound.
COLUMN_INDEX_UP_TWO_GROUPS = {
    "79515628d851c6e1",  # 9_20.jpg
}

# The reconstructed faint divider on 9/12 belongs to the time/location area;
# the first duty cell starts at the following detected edge.
COLUMN_INDEX_DOWN_ONE_GROUPS = {
    "984e16ffecd718a1",  # 9_12.jpg (UI sheet 9)
}

# UI sheet 10 has a faint first-duty divider. The regular grid completer can
# reconstruct that one missing rule, but the production safety check rejects
# invented anchors by design. This reviewed source-specific exception lets the
# label dataset use the reconstructed rule instead of retaining stale crops.
ALLOW_INTERPOLATED_FIRST_DUTY_GROUPS = {
    "984e16ffecd718a1",  # 9_12.jpg
}

# These sheets have reliable detected x positions, but local glyph strokes can
# make the optional per-column slope fit cross neighbouring columns. Keep the
# detected edges and use vertical models rather than replacing them with the
# calibration template.
STRAIGHT_DETECTED_COLUMN_GROUPS = {
    "1a76ea0031aad6db",  # 9_11.jpg (UI sheet 2)
    "78c0047a6ecf7f81",  # 9_19.jpg (UI sheet 5)
}


def _source_photo(cell_dir: Path) -> Path | None:
    schedule = cell_dir.parents[2]
    for name in ("input.jpg", "input.png", "input.webp"):
        candidate = schedule / name
        if candidate.is_file():
            return candidate
    return None


def _split_for(group: str) -> str:
    value = int(hashlib.sha256(group.encode()).hexdigest()[:8], 16) % 100
    return "train" if value < 70 else "val" if value < 85 else "test"


def _save_label_debug(image: Image.Image, template: GridTemplate,
                      grid_lines: tuple[list[int], list[int]], row_models, column_models,
                      destination: Path) -> None:
    debug = image.copy()
    draw = ImageDraw.Draw(debug)
    for x in grid_lines[0]:
        draw.line((x, 0, x, debug.height - 1), fill=(245, 158, 11), width=2)
    for y in grid_lines[1]:
        draw.line((0, y, debug.width - 1, y), fill=(245, 158, 11), width=2)
    for row in sorted(template.row_map):
        for column in sorted(template.column_map):
            points = cell_polygon(row, column, grid_lines, row_models, column_models)
            draw.line([*points, points[0]], fill=(37, 99, 235), width=4, joint="curve")
    debug.thumbnail((2200, 1600), Image.Resampling.LANCZOS)
    debug.save(destination, format="JPEG", quality=90, optimize=True)


def prepare_dataset(simulations: Path, dataset: Path) -> dict:
    images_dir = dataset / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    existing = {}
    manifest = dataset / "labels.csv"
    if manifest.is_file():
        with manifest.open(newline="", encoding="utf-8") as handle:
            existing = {row["sample_id"]: row for row in csv.DictReader(handle)}
    selected: dict[tuple[str, str], Path] = {}
    for path in sorted(simulations.glob("*/**/pipeline/grid/cells/r*_c*.png")):
        photo = _source_photo(path.parent)
        if photo is None:
            continue
        group = hashlib.sha256(photo.read_bytes()).hexdigest()
        selected[(group, path.name)] = path
    rows = []
    for (group, filename), source in sorted(selected.items()):
        stem = Path(filename).stem
        row_number, column_number = (int(part[1:]) for part in stem.split("_"))
        sample_id = f"{group[:16]}_{stem}"
        destination = images_dir / f"{sample_id}.png"
        if not destination.exists():
            shutil.copy2(source, destination)
        prior = existing.get(sample_id, {})
        rows.append({
            "sample_id": sample_id,
            "image": destination.relative_to(dataset).as_posix(),
            "source_group": group,
            "row": row_number,
            "column": column_number,
            "split": _split_for(group),
            **{field: prior.get(field, "") for field in LABEL_FIELDS.values()},
        })
    temporary = manifest.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(manifest)
    summary = {"samples": len(rows), "groups": len({row["source_group"] for row in rows})}
    summary.update({split: sum(row["split"] == split for row in rows)
                    for split in ("train", "val", "test")})
    return summary


def ingest_schedules(schedules: Path, dataset: Path, template_path: Path, *,
                     only_sources: list[Path] | None = None,
                     progress: Callable[[int, int, Path], None] | None = None) -> dict:
    """Turn raw schedule photos into label-ready cell crops without an API call."""
    images_dir = dataset / "images"
    debug_dir = dataset / "debug"
    images_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)
    manifest = dataset / "labels.csv"
    existing = {}
    if manifest.is_file():
        with manifest.open(newline="", encoding="utf-8") as handle:
            existing = {row["sample_id"]: row for row in csv.DictReader(handle)}
    template = GridTemplate.from_json(template_path)
    extensions = {".jpg", ".jpeg", ".png", ".webp"}
    sources = (
        sorted(path.resolve() for path in only_sources)
        if only_sources is not None else
        sorted(path for path in schedules.rglob("*")
               if path.is_file() and path.suffix.lower() in extensions)
    )
    target_groups = {
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sources if path.is_file()
    }
    # A single-photo web upload must not remove other sheets from labels.csv.
    rows = [row for row in existing.values() if row["source_group"] not in target_groups]
    failures, fallbacks, reused, seen_groups = [], [], [], set()
    for source_index, source in enumerate(sources, 1):
        if progress:
            progress(source_index, len(sources), source)
        group = hashlib.sha256(source.read_bytes()).hexdigest()
        if group in seen_groups:
            continue
        seen_groups.add(group)
        if group[:16] in EXCLUDED_SOURCE_GROUPS:
            continue
        corrected = None
        try:
            corrected, _ = correct_perspective(source, template)
            validate_grid_alignment(corrected, template)
            try:
                grid_lines = registered_grid_lines(corrected, template)
            except Exception:
                if group[:16] not in ALLOW_INTERPOLATED_FIRST_DUTY_GROUPS:
                    raise
                detected_x, detected_y = detect_grid_lines(corrected)
                expected_x = _pixel_lines(template.grid_x, corrected.width)
                required_x = max(column + 1 for column in template.column_map) + 1
                completed_x = _complete_regular_grid_lines(
                    detected_x, expected_x, required_count=required_x,
                )
                if len(completed_x) < required_x:
                    raise
                grid_lines = (completed_x, detected_y)
                fallbacks.append({"file": str(source),
                                  "fallback": "interpolated_first_duty"})
            if group[:16] in COLUMN_INDEX_UP_TWO_GROUPS:
                detected_x = grid_lines[0]
                semantic_x = detected_x.copy()
                for index in range(min(template.column_map), len(semantic_x)):
                    semantic_x[index] = detected_x[index - 2]
                grid_lines = (semantic_x, grid_lines[1])
            if group[:16] in COLUMN_INDEX_DOWN_ONE_GROUPS:
                detected_x = grid_lines[0]
                semantic_x = detected_x.copy()
                required_x = max(column + 1 for column in template.column_map) + 1
                for index in range(min(template.column_map), required_x):
                    semantic_x[index] = detected_x[index + 1]
                grid_lines = (semantic_x, grid_lines[1])
            semantic_y, _row_index_offset = align_semantic_row_lines(
                grid_lines[1], _pixel_lines(template.grid_y, corrected.height),
                first_body_row=min(template.row_map),
            )
            grid_lines = (grid_lines[0], semantic_y)
            row_models = registered_row_models(corrected, grid_lines[1])
            try:
                if group[:16] in COLUMN_INDEX_UP_TWO_GROUPS | COLUMN_INDEX_DOWN_ONE_GROUPS:
                    # Earlier semantic indexes intentionally share positions
                    # with unused header columns after the -2 remap. A global
                    # crossing check would reject that deliberate aliasing.
                    column_models = [(float(x), 0.0) for x in grid_lines[0]]
                else:
                    column_models = registered_column_models(corrected, grid_lines[0])
            except Exception as exc:
                # Label preparation is reviewed by a human.  If only the
                # per-column slope fit is unstable, keep the detected x edges
                # as straight lines rather than rejecting the whole sheet.
                # The detected list can contain a glyph stroke masquerading as
                # a column, which shifts every following semantic index.  The
                # perspective-corrected canvas is calibrated to the template,
                # so its normalized x edges are the safer human-label fallback.
                if group[:16] in STRAIGHT_DETECTED_COLUMN_GROUPS:
                    column_models = [(float(x), 0.0) for x in grid_lines[0]]
                    fallback = "straight_detected_columns"
                else:
                    template_x = _pixel_lines(template.grid_x, corrected.width)
                    grid_lines = (template_x, grid_lines[1])
                    column_models = [(float(x), 0.0) for x in template_x]
                    fallback = "template_columns"
                fallbacks.append({"file": str(source), "reason": str(exc),
                                  "fallback": fallback})
            analysis_image, origin = analysis_time_window(
                corrected, template, y_lines=grid_lines[1], row_models=row_models,
            )
            cells = crop_mapped_cells(
                analysis_image, template, origin=origin, inner_fraction=.96,
                canvas_size=corrected.size, grid_lines=grid_lines,
                row_models=row_models, column_models=column_models,
            )
            _save_label_debug(corrected, template, grid_lines, row_models, column_models,
                              debug_dir / f"{group[:16]}.jpg")
        except Exception as exc:
            # Never discard already-labelled rows merely because a later
            # re-ingest cannot register this photo.
            prior_rows = [row for row in existing.values() if row["source_group"] == group]
            if prior_rows:
                rows.extend(prior_rows)
                debug_path = debug_dir / f"{group[:16]}.jpg"
                if not debug_path.exists() and corrected is not None:
                    xs = _pixel_lines(template.grid_x, corrected.width)
                    ys = _pixel_lines(template.grid_y, corrected.height)
                    _save_label_debug(
                        corrected, template, (xs, ys),
                        [(float(y), 0.0) for y in ys],
                        [(float(x), 0.0) for x in xs], debug_path,
                    )
                reused.append({"file": str(source), "reason": str(exc),
                               "reused_samples": len(prior_rows)})
            else:
                failures.append({"file": str(source), "error": str(exc)})
            continue
        for cell in cells:
            stem = f"r{cell.row:02d}_c{cell.column:02d}"
            sample_id = f"{group[:16]}_{stem}"
            destination = images_dir / f"{sample_id}.png"
            # Low compression is much faster for thousands of small training
            # crops; model input is unchanged and disk growth is modest.
            cell.image.save(destination, format="PNG", compress_level=1)
            prior = existing.get(sample_id, {})
            rows.append({
                "sample_id": sample_id,
                "image": destination.relative_to(dataset).as_posix(),
                "source_group": group,
                "row": cell.row,
                "column": cell.column,
                "split": prior.get("split") or _split_for(group),
                **{field: prior.get(field, "") for field in LABEL_FIELDS.values()},
            })
    # A labeler may be open during the two-minute image pass. Reload its latest
    # values immediately before replacing the manifest so concurrent labels
    # are never rolled back to the snapshot taken at startup.
    latest = {}
    if manifest.is_file():
        with manifest.open(newline="", encoding="utf-8") as handle:
            latest = {row["sample_id"]: row for row in csv.DictReader(handle)}
    for row in rows:
        current = latest.get(row["sample_id"], {})
        for field in LABEL_FIELDS.values():
            if current.get(field) in ("0", "1"):
                row[field] = current[field]
    temporary = manifest.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: (row["source_group"], int(row["row"]),
                                                       int(row["column"]))))
    temporary.replace(manifest)
    report = {
        "source_files": len(sources), "unique_photos": len(seen_groups),
        "successful_photos": len({row["source_group"] for row in rows}),
        "samples": len(rows), "preserved_labels": sum(
            all(row[field] in ("0", "1") for field in LABEL_FIELDS.values()) for row in rows
        ),
        "fallbacks": fallbacks, "reused_existing": reused, "failures": failures,
    }
    (dataset / "ingest-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


class LabelStore:
    def __init__(self, dataset: Path):
        self.dataset = dataset
        self.manifest = dataset / "labels.csv"
        self.lock = threading.Lock()

    def rows(self) -> list[dict]:
        with self.manifest.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def update(self, sample_id: str, labels: dict[str, int]) -> None:
        self.update_many({sample_id: labels})

    def update_many(self, updates: dict[str, dict[str, int]]) -> None:
        with self.lock:
            rows = self.rows()
            matched: set[str] = set()
            for row in rows:
                sample_id = row["sample_id"]
                if sample_id in updates:
                    for label, field in LABEL_FIELDS.items():
                        row[field] = str(int(bool(updates[sample_id].get(label, 0))))
                    matched.add(sample_id)
            missing = set(updates) - matched
            if missing:
                raise KeyError(", ".join(sorted(missing)))
            temporary = self.manifest.with_suffix(".csv.tmp")
            with temporary.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            temporary.replace(self.manifest)


def _auto_label_group(dataset: Path, source_group: str, checkpoint: Path) -> dict:
    """Persist model suggestions separately from human-confirmed labels."""
    if not checkpoint.is_file():
        raise ValueError(f"自動標註模型不存在：{checkpoint}")
    store = LabelStore(dataset)
    rows = [row for row in store.rows() if row["source_group"] == source_group]
    if not rows:
        raise ValueError("上傳照片沒有產生可標註格子")
    classifier = RosterResNetClassifier(checkpoint)
    images = []
    for row in rows:
        with Image.open(dataset / row["image"]) as image:
            images.append(image.copy())
    predictions = classifier.predict(images)
    suggestions = {}
    for row, prediction in zip(rows, predictions, strict=True):
        suggestions[row["sample_id"]] = {
            "labels": {label: label in prediction.labels for label in LABELS},
            "probabilities": prediction.probabilities,
        }
    destination = dataset / "auto-labels.json"
    existing = json.loads(destination.read_text(encoding="utf-8")) if destination.is_file() else {}
    existing.update(suggestions)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return {"source_group": source_group, "samples": len(suggestions)}


LABEL_PAGE = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>整張班表標註</title>
<style>:root{--bg:#0f172a;--panel:#1e293b;--muted:#94a3b8}*{box-sizing:border-box}body{font-family:system-ui;background:var(--bg);color:#f8fafc;margin:0}.wrap{max-width:1800px;margin:auto;padding:18px}.toolbar{position:sticky;top:0;z-index:5;background:rgba(15,23,42,.96);padding:12px 0}.top,.people,.actions,.upload{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.top{justify-content:space-between}.people button{font-size:24px;min-width:100px}.people button.active{outline:4px solid white;transform:translateY(-2px)}button{cursor:pointer;border:0;border-radius:10px;padding:11px 16px;background:#334155;color:white;font-weight:700}.ding{background:#dc2626}.wu{background:#ea580c}.ji{background:#2563eb}.geng{background:#16a34a}.actions{margin-top:10px}.actions button{font-size:16px}.save{background:#7c3aed}.uploadButton{background:#0891b2}.danger{background:#475569}.meta{color:var(--muted)}.sheet{display:grid;background:#64748b;gap:1px;border:1px solid #64748b;min-width:1450px}.head,.time,.cell{background:white;color:#111827;min-width:0}.head,.time{display:flex;align-items:center;justify-content:center;font-weight:800}.head{height:46px}.time{font-size:14px}.cell{height:108px;position:relative;cursor:pointer;overflow:hidden}.cell img{width:100%;height:100%;object-fit:contain}.cell:hover{box-shadow:inset 0 0 0 4px #facc15}.cell.current{box-shadow:inset 0 0 0 5px #facc15}.cell.auto{box-shadow:inset 0 0 0 2px #22d3ee}.badges{position:absolute;top:3px;right:3px;display:flex;gap:2px}.badge{color:white;border-radius:5px;padding:2px 5px;font-weight:900;font-size:14px}.scroll{overflow:auto;border-radius:12px;margin-top:10px}.upload{background:#1e293b;padding:12px;border-radius:12px;margin-bottom:10px}.debug{margin-top:24px;background:#1e293b;padding:18px;border-radius:14px}.debug img{display:block;width:100%;height:auto;background:white}.help{color:#cbd5e1;line-height:1.65}.done{color:#4ade80;font-weight:800}.autoText{color:#22d3ee;font-weight:800}</style></head><body><div class="wrap"><div class="toolbar"><div class="upload"><input id="photo" type="file" accept="image/jpeg,image/png,image/webp"><button class="uploadButton" onclick="uploadPhoto()">上傳、切格並自動標註</button><span id="uploadStatus" class="meta"></span></div><div class="top"><h1>整張班表標註</h1><div><span id="progress"></span>　<span id="status"></span></div></div><div class="people" id="people"></div><div class="actions"><button onclick="previous()">← 上一張</button><button onclick="next()">下一張 →</button><button class="danger" onclick="clearPerson()">清除本張目前人物</button><button class="save" onclick="saveSheet()">確認人工標註並儲存（Enter）</button><span class="meta" id="summary"></span></div></div><div class="scroll"><div class="sheet" id="sheet"></div></div><p class="help">上傳照片後會自動切格並以目前 checkpoint 預標註。<span class="autoText">淺藍框代表模型建議，尚未算成人工標註</span>；請逐格修正後按「確認人工標註並儲存」。快捷鍵 1–4 選人物、Enter 儲存、左右鍵換班表。</p><section class="debug"><h2>格線 Debug</h2><p class="help">藍框是實際送入分類器的目標格，黃色線是偵測到的表格線。若藍框沒有對準，請先不要標註這張。</p><img id="debugImage" alt="格線偵測 Debug"></section></div><script>
const labels=['丁','戊','己','庚'], classes={'丁':'ding','戊':'wu','己':'ji','庚':'geng'};let sheets=[],index=0,active='丁',state={};const people=document.getElementById('people'),sheet=document.getElementById('sheet'),progress=document.getElementById('progress'),statusEl=document.getElementById('status'),summary=document.getElementById('summary'),debugImage=document.getElementById('debugImage'),uploadStatus=document.getElementById('uploadStatus');
async function init(){sheets=await(await fetch('/api/sheets')).json();let first=sheets.findIndex(x=>!x.completed);index=first<0?0:first;render()}
function render(){if(!sheets.length){statusEl.textContent='尚無照片，請先上傳';sheet.innerHTML='';return}let x=sheets[index];state={};x.cells.forEach(c=>state[c.sample_id]={...c.labels});progress.textContent=`第 ${index+1} / ${sheets.length} 張・${x.split}`;statusEl.textContent=x.completed?'✓ 已完成人工確認':x.auto_labeled?'模型已預標註，等待人工確認':'尚未整張確認';statusEl.className=x.completed?'done':x.auto_labeled?'autoText':'';people.innerHTML=labels.map((l,i)=>`<button class="${classes[l]} ${active===l?'active':''}" onclick="selectPerson('${l}')">${i+1} ${l}</button>`).join('');debugImage.src='/image/'+encodeURIComponent(x.debug_image);drawGrid();updateSummary()}
function drawGrid(){let x=sheets[index],rows=[...new Set(x.cells.map(c=>c.row))].sort((a,b)=>a-b),cols=[...new Set(x.cells.map(c=>c.column))].sort((a,b)=>a-b);sheet.style.gridTemplateColumns=`74px repeat(${cols.length},1fr)`;let html='<div class="head">時段</div>'+cols.map(c=>`<div class="head">C${String(c).padStart(2,'0')}</div>`).join('');for(let r of rows){html+=`<div class="time">${x.row_names[String(r)]||('r'+r)}</div>`;for(let c of cols){let item=x.cells.find(v=>+v.row===+r&&+v.column===+c),chosen=state[item.sample_id]||{},badges=labels.filter(l=>chosen[l]).map(l=>`<span class="badge ${classes[l]}">${l}</span>`).join('');html+=`<div class="cell ${item.auto?'auto':''} ${chosen[active]?'current':''}" onclick="toggle('${item.sample_id}')"><img src="/image/${encodeURIComponent(item.image)}"><div class="badges">${badges}</div></div>`}}sheet.innerHTML=html}
function selectPerson(l){active=l;people.querySelectorAll('button').forEach((b,i)=>b.classList.toggle('active',labels[i]===l));drawGrid();updateSummary()}
function toggle(id){state[id][active]=!state[id][active];drawGrid();updateSummary()}function clearPerson(){if(!confirm(`清除本張所有「${active}」標記？`))return;Object.values(state).forEach(x=>x[active]=false);drawGrid();updateSummary()}
function updateSummary(){summary.textContent=`目前：${active}　`+labels.map(l=>`${l} ${Object.values(state).filter(x=>x[l]).length} 格`).join('　')}
function previous(){index=Math.max(0,index-1);render()}function next(){index=Math.min(sheets.length-1,index+1);render()}
async function saveSheet(){let x=sheets[index],response=await fetch('/api/sheet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_group:x.source_group,labels:state})});if(!response.ok){alert(await response.text());return}x.cells.forEach(c=>{c.labels={...state[c.sample_id]};c.labeled=true});x.completed=true;let n=sheets.findIndex((v,i)=>i>index&&!v.completed);index=n>=0?n:Math.min(sheets.length-1,index+1);render()}
async function uploadPhoto(){let file=document.getElementById('photo').files[0];if(!file){alert('請先選擇照片');return}uploadStatus.textContent='正在切格與自動標註…';let response=await fetch('/api/upload',{method:'POST',headers:{'Content-Type':file.type,'X-Filename':encodeURIComponent(file.name)},body:file});if(!response.ok){uploadStatus.textContent='失敗：'+await response.text();return}let result=await response.json();sheets=await(await fetch('/api/sheets')).json();index=sheets.findIndex(x=>x.source_group===result.source_group);if(index<0)index=sheets.length-1;uploadStatus.textContent=`完成：${result.samples} 格，請人工確認`;render()}
function shortcutNumber(e){let match=/^(?:Digit|Numpad)([1-4])$/.exec(e.code||'');if(match)return +match[1];return /^[1-4]$/.test(e.key||'')?+e.key:0}
window.addEventListener('keydown',e=>{let number=shortcutNumber(e);if(number){e.preventDefault();selectPerson(labels[number-1]);return}if(e.key==='Enter'){e.preventDefault();saveSheet()}else if(e.key==='ArrowLeft'){e.preventDefault();previous()}else if(e.key==='ArrowRight'){e.preventDefault();next()}},true);init();
</script></body></html>"""


def serve_labeler(dataset: Path, schedules: Path, template: Path, checkpoint: Path,
                  host: str, port: int, open_browser: bool) -> None:
    store = LabelStore(dataset)
    upload_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload: bytes, content_type: str, status=200):
            self.send_response(status); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/":
                return self._send(LABEL_PAGE.encode(), "text/html; charset=utf-8")
            if route == "/api/sheets":
                auto_path = dataset / "auto-labels.json"
                auto_labels = (
                    json.loads(auto_path.read_text(encoding="utf-8"))
                    if auto_path.is_file() else {}
                )
                grouped: dict[str, list[dict]] = {}
                for row in store.rows():
                    grouped.setdefault(row["source_group"], []).append(row)
                sheets = []
                for source_group, rows in sorted(grouped.items()):
                    cells = []
                    for row in sorted(rows, key=lambda item: (int(item["row"]), int(item["column"]))):
                        labeled = all(row[field] in ("0", "1") for field in LABEL_FIELDS.values())
                        suggestion = auto_labels.get(row["sample_id"], {}) if not labeled else {}
                        suggested_labels = suggestion.get("labels", {})
                        cells.append({
                            "sample_id": row["sample_id"], "image": row["image"],
                            "row": int(row["row"]), "column": int(row["column"]),
                            "labeled": labeled,
                            "auto": bool(suggestion),
                            "probabilities": suggestion.get("probabilities", {}),
                            "labels": ({label: row[field] == "1"
                                        for label, field in LABEL_FIELDS.items()}
                                       if labeled else
                                       {label: bool(suggested_labels.get(label, False))
                                        for label in LABELS}),
                        })
                    unique_rows = sorted({cell["row"] for cell in cells})
                    sheets.append({
                        "source_group": source_group, "split": rows[0]["split"],
                        "debug_image": f"debug/{source_group[:16]}.jpg",
                        "completed": all(cell["labeled"] for cell in cells),
                        "auto_labeled": any(cell["auto"] for cell in cells),
                        "row_names": {str(row): f"{row + 1:02d}-{row + 2:02d}"
                                      for row in unique_rows},
                        "cells": cells,
                    })
                return self._send(json.dumps(sheets, ensure_ascii=False).encode(), "application/json")
            if route.startswith("/image/"):
                relative = Path(unquote(route[len("/image/"):]))
                target = (dataset / relative).resolve()
                if dataset.resolve() not in target.parents or not target.is_file():
                    return self._send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)
                content_type = "image/jpeg" if target.suffix.lower() in (".jpg", ".jpeg") else "image/png"
                return self._send(target.read_bytes(), content_type)
            self._send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)

        def do_POST(self):
            route = urlparse(self.path).path
            if route not in ("/api/label", "/api/sheet", "/api/upload"):
                return self._send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if route == "/api/upload":
                    content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
                    suffixes = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
                    if content_type not in suffixes or length <= 0 or length > 15 * 1024 * 1024:
                        raise ValueError("只接受 15 MB 以下 JPG、PNG、WEBP")
                    original_name = Path(unquote(self.headers.get("X-Filename", "upload"))).stem
                    safe_stem = "".join(character for character in original_name
                                        if character.isalnum() or character in "-_")[:60] or "upload"
                    payload = self.rfile.read(length)
                    source_group = hashlib.sha256(payload).hexdigest()
                    schedules.mkdir(parents=True, exist_ok=True)
                    destination = schedules / f"{safe_stem}-{uuid4().hex[:8]}{suffixes[content_type]}"
                    destination.write_bytes(payload)
                    with upload_lock:
                        report = ingest_schedules(
                            schedules, dataset, template,
                            only_sources=[destination],
                        )
                        failure = next((item for item in report["failures"]
                                        if item["file"] == str(destination)), None)
                        if failure:
                            raise ValueError(f"照片切格失敗：{failure['error']}")
                        auto = _auto_label_group(dataset, source_group, checkpoint)
                    return self._send(json.dumps(auto, ensure_ascii=False).encode(), "application/json")
                body = json.loads(self.rfile.read(length))
                if route == "/api/label":
                    store.update(body["sample_id"], body["labels"])
                else:
                    group_rows = [row for row in store.rows()
                                  if row["source_group"] == body["source_group"]]
                    expected = {row["sample_id"] for row in group_rows}
                    received = set(body["labels"])
                    if not expected or received != expected:
                        raise ValueError("整張儲存的格子不完整，請重新整理頁面後再試")
                    store.update_many(body["labels"])
            except (ValueError, KeyError, json.JSONDecodeError, OSError) as exc:
                return self._send(str(exc).encode(), "text/plain", HTTPStatus.BAD_REQUEST)
            self._send(b'{"ok":true}', "application/json")

        def log_message(self, *_args):
            pass

    url = f"http://{host}:{port}"
    print(f"標註介面：{url}（Ctrl+C 結束）")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _load_labeled(dataset: Path, split: str | None = None) -> list[dict]:
    with (dataset / "labels.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [row for row in rows if (split is None or row["split"] == split) and
            all(row[field] in ("0", "1") for field in LABEL_FIELDS.values())]


def dataset_status(dataset: Path) -> dict:
    with (dataset / "labels.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    labeled = [row for row in rows if all(row[field] in ("0", "1")
                                           for field in LABEL_FIELDS.values())]
    return {
        "total": len(rows), "labeled": len(labeled), "remaining": len(rows) - len(labeled),
        "by_split": {
            split: {"labeled": sum(row["split"] == split for row in labeled),
                    "total": sum(row["split"] == split for row in rows)}
            for split in ("train", "val", "test")
        },
        "positive_cells": {
            label: sum(row[field] == "1" for row in labeled)
            for label, field in LABEL_FIELDS.items()
        },
    }


def _metrics(probabilities, targets, thresholds=None):
    import torch
    thresholds = thresholds or torch.full((len(LABELS),), 0.5)
    predictions = probabilities >= thresholds
    targets = targets.bool()
    result = {}
    for index, label in enumerate(LABELS):
        tp = int((predictions[:, index] & targets[:, index]).sum())
        fp = int((predictions[:, index] & ~targets[:, index]).sum())
        fn = int((~predictions[:, index] & targets[:, index]).sum())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        result[label] = {"precision": precision, "recall": recall,
                         "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
                         "support": int(targets[:, index].sum())}
    return result


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def train(dataset: Path, output: Path, epochs: int, batch_size: int, image_size: int,
          learning_rate: float, seed: int) -> dict:
    import torch
    from torch.utils.data import DataLoader, Dataset
    random.seed(seed); torch.manual_seed(seed)
    device = torch.device("mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
                          else "cuda" if torch.cuda.is_available() else "cpu")
    train_rows, val_rows = _load_labeled(dataset, "train"), _load_labeled(dataset, "val")
    if not train_rows or not val_rows:
        raise ValueError("train 與 val 都必須先有人工標註資料")

    class Cells(Dataset):
        def __init__(self, rows, augment): self.rows, self.augment = rows, augment
        def __len__(self): return len(self.rows)
        def __getitem__(self, index):
            row = self.rows[index]
            with Image.open(dataset / row["image"]) as image:
                tensor = prepare_image(image, image_size, augment=self.augment)
            target = torch.tensor([float(row[LABEL_FIELDS[label]]) for label in LABELS])
            return tensor, target

    positives = torch.tensor([sum(int(row[LABEL_FIELDS[label]]) for row in train_rows) for label in LABELS],
                             dtype=torch.float32)
    negatives = len(train_rows) - positives
    pos_weight = (negatives / positives.clamp_min(1)).clamp(1, 30).to(device)
    model = build_resnet18().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-4)
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    train_loader = DataLoader(Cells(train_rows, True), batch_size=batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(Cells(val_rows, False), batch_size=batch_size, shuffle=False, num_workers=0)
    best_f1, best_payload = -1.0, None
    history = []
    report_path = output.with_suffix(".json")
    started_at = time.time()
    base_report = {"status": "running", "checkpoint": str(output), "device": str(device),
                   "train_samples": len(train_rows), "val_samples": len(val_rows),
                   "total_epochs": epochs, "started_at": started_at, "history": []}
    _write_json_atomic(report_path, base_report)
    for epoch in range(1, epochs + 1):
        model.train(); total_loss = 0.0
        for images, targets in train_loader:
            images, targets = images.to(device), targets.to(device)
            optimizer.zero_grad(); loss = loss_fn(model(images), targets); loss.backward(); optimizer.step()
            total_loss += float(loss) * len(images)
        model.eval(); probabilities, targets_all = [], []
        with torch.no_grad():
            for images, targets in val_loader:
                probabilities.append(torch.sigmoid(model(images.to(device))).cpu()); targets_all.append(targets)
        probabilities, targets_all = torch.cat(probabilities), torch.cat(targets_all)
        metrics = _metrics(probabilities, targets_all)
        macro_f1 = sum(item["f1"] for item in metrics.values()) / len(LABELS)
        record = {"epoch": epoch, "loss": total_loss / len(train_rows),
                  "val_macro_f1": macro_f1, "metrics": metrics}
        history.append(record); print(json.dumps(record, ensure_ascii=False))
        if macro_f1 > best_f1:
            best_f1 = macro_f1
            best_payload = {"model_state": {key: value.detach().cpu() for key, value in model.state_dict().items()},
                            "labels": LABELS, "image_size": image_size,
                            "thresholds": {label: 0.5 for label in LABELS}, "epoch": epoch,
                            "val_metrics": metrics}
        _write_json_atomic(report_path, {
            **base_report, "current_epoch": epoch, "best_epoch": best_payload["epoch"],
            "best_macro_f1": best_f1, "updated_at": time.time(), "history": history,
        })
    output.parent.mkdir(parents=True, exist_ok=True); torch.save(best_payload, output)
    report = {**base_report, "status": "complete", "current_epoch": epochs,
              "best_epoch": best_payload["epoch"], "best_macro_f1": best_f1,
              "updated_at": time.time(), "completed_at": time.time(), "history": history}
    _write_json_atomic(report_path, report)
    return report


def serve_monitor(checkpoint: Path, host: str, port: int, open_browser: bool) -> None:
    dashboard = ROOT / "training-dashboard.html"
    report = checkpoint.with_suffix(".json")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = urlparse(self.path).path
            if route in ("/", "/training-dashboard.html"):
                payload, content_type = dashboard.read_bytes(), "text/html; charset=utf-8"
            elif route == "/report.json" and report.is_file():
                payload, content_type = report.read_bytes(), "application/json; charset=utf-8"
            else:
                self.send_error(HTTPStatus.NOT_FOUND); return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload)

        def do_POST(self):
            if urlparse(self.path).path != "/api/infer-schedule":
                self.send_error(HTTPStatus.NOT_FOUND); return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            suffixes = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
            length = int(self.headers.get("Content-Length", "0"))
            if content_type not in suffixes or length <= 0 or length > 15 * 1024 * 1024:
                self.send_error(HTTPStatus.BAD_REQUEST, "只接受 15 MB 以下 JPG、PNG、WEBP"); return
            try:
                from backend.phase1_multilabel_compare import recognize_source_multilabel
                started = time.monotonic()
                with tempfile.TemporaryDirectory(prefix="roster-web-infer-") as folder:
                    source = Path(folder) / ("source" + suffixes[content_type])
                    source.write_bytes(self.rfile.read(length))
                    template = GridTemplate.from_json(ROOT / "configs/phase1_roster_template.local.json")
                    result = recognize_source_multilabel(
                        source, template, model="local-resnet18",
                        resnet_checkpoint=checkpoint,
                    )
                payload = json.dumps({
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "recognition_policy": result["recognition_policy"],
                    "checkpoint": result.get("checkpoint"),
                    "markdown_grid": result["markdown_grid"],
                    "cell_predictions": result.get("cell_predictions", []),
                }, ensure_ascii=False).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
            except Exception as exc:
                payload = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
                self.send_response(HTTPStatus.UNPROCESSABLE_ENTITY)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    url = f"http://{host}:{port}"
    print(f"訓練監控：{url}（Ctrl+C 結束）")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def evaluate(dataset: Path, checkpoint: Path, split: str) -> dict:
    rows = _load_labeled(dataset, split)
    if not rows: raise ValueError(f"{split} 沒有已標註資料")
    classifier = RosterResNetClassifier(checkpoint)
    predictions, targets = [], []
    for start in range(0, len(rows), 64):
        batch = rows[start:start + 64]; images = []
        for row in batch:
            with Image.open(dataset / row["image"]) as image: images.append(image.convert("RGB").copy())
        predictions.extend([[result.probabilities[label] for label in LABELS]
                            for result in classifier.predict(images)])
        targets.extend([[int(row[LABEL_FIELDS[label]]) for label in LABELS] for row in batch])
    import torch
    return {"split": split, "samples": len(rows),
            "metrics": _metrics(torch.tensor(predictions), torch.tensor(targets))}


def infer_dataset(dataset: Path, checkpoint: Path, split: str, output: Path) -> dict:
    rows = _load_labeled(dataset, split)
    if not rows:
        raise ValueError(f"{split} 沒有可供 inference 的資料")
    classifier = RosterResNetClassifier(checkpoint)
    records = []
    for start in range(0, len(rows), 64):
        batch = rows[start:start + 64]
        images = []
        for row in batch:
            with Image.open(dataset / row["image"]) as image:
                images.append(image.convert("RGB").copy())
        for row, prediction in zip(batch, classifier.predict(images)):
            actual = tuple(label for label, field in LABEL_FIELDS.items() if row[field] == "1")
            records.append({
                "sample_id": row["sample_id"], "image": row["image"],
                "row": row["row"], "column": row["column"],
                "actual": "".join(actual), "predicted": "".join(prediction.labels),
                "correct": str(actual == prediction.labels),
                **{f"prob_{LABEL_FIELDS[label]}": f"{prediction.probabilities[label]:.6f}"
                   for label in LABELS},
            })
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = ("sample_id", "image", "row", "column", "actual", "predicted", "correct",
              *(f"prob_{LABEL_FIELDS[label]}" for label in LABELS))
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(records)
    temporary.replace(output)
    return {"split": split, "samples": len(records),
            "correct": sum(row["correct"] == "True" for row in records),
            "errors": sum(row["correct"] != "True" for row in records),
            "output": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare"); prepare.add_argument("--simulations", type=Path, default=ROOT / "data/line-simulations"); prepare.add_argument("--dataset", type=Path, default=ROOT / "data/roster-resnet")
    ingest = sub.add_parser("ingest"); ingest.add_argument("--schedules", type=Path, default=ROOT / "data/schedules"); ingest.add_argument("--dataset", type=Path, default=ROOT / "data/roster-resnet"); ingest.add_argument("--template", type=Path, default=ROOT / "configs/phase1_roster_template.local.json")
    label = sub.add_parser("label"); label.add_argument("--dataset", type=Path, default=ROOT / "data/roster-resnet"); label.add_argument("--schedules", type=Path, default=ROOT / "data/schedules"); label.add_argument("--template", type=Path, default=ROOT / "configs/phase1_roster_template.local.json"); label.add_argument("--checkpoint", type=Path, default=ROOT / "models/roster-resnet18.pt"); label.add_argument("--host", default="127.0.0.1"); label.add_argument("--port", type=int, default=8765); label.add_argument("--no-browser", action="store_true")
    status = sub.add_parser("status"); status.add_argument("--dataset", type=Path, default=ROOT / "data/roster-resnet")
    training = sub.add_parser("train"); training.add_argument("--dataset", type=Path, default=ROOT / "data/roster-resnet"); training.add_argument("--output", type=Path, default=ROOT / "models/roster-resnet18.pt"); training.add_argument("--epochs", type=int, default=30); training.add_argument("--batch-size", type=int, default=64); training.add_argument("--image-size", type=int, default=128); training.add_argument("--learning-rate", type=float, default=3e-4); training.add_argument("--seed", type=int, default=42)
    monitor = sub.add_parser("monitor"); monitor.add_argument("--checkpoint", type=Path, default=ROOT / "models/roster-resnet18.pt"); monitor.add_argument("--host", default="127.0.0.1"); monitor.add_argument("--port", type=int, default=8766); monitor.add_argument("--no-browser", action="store_true")
    evaluation = sub.add_parser("evaluate"); evaluation.add_argument("--dataset", type=Path, default=ROOT / "data/roster-resnet"); evaluation.add_argument("--checkpoint", type=Path, default=ROOT / "models/roster-resnet18.pt"); evaluation.add_argument("--split", choices=("train", "val", "test"), default="test")
    inference = sub.add_parser("infer"); inference.add_argument("--dataset", type=Path, default=ROOT / "data/roster-resnet"); inference.add_argument("--checkpoint", type=Path, default=ROOT / "models/roster-resnet18.pt"); inference.add_argument("--split", choices=("train", "val", "test"), default="test"); inference.add_argument("--output", type=Path, default=ROOT / "models/roster-resnet18-test-predictions.csv")
    args = parser.parse_args()
    if args.command == "prepare": print(json.dumps(prepare_dataset(args.simulations, args.dataset), ensure_ascii=False, indent=2))
    elif args.command == "ingest":
        def show_progress(index: int, total: int, source: Path) -> None:
            print(f"[{index}/{total}] 正在切格：{source.name}", flush=True)
        print(json.dumps(
            ingest_schedules(args.schedules, args.dataset, args.template,
                             progress=show_progress),
            ensure_ascii=False, indent=2,
        ))
    elif args.command == "label": serve_labeler(args.dataset, args.schedules, args.template, args.checkpoint, args.host, args.port, not args.no_browser)
    elif args.command == "status": print(json.dumps(dataset_status(args.dataset), ensure_ascii=False, indent=2))
    elif args.command == "train": print(json.dumps(train(args.dataset, args.output, args.epochs, args.batch_size, args.image_size, args.learning_rate, args.seed), ensure_ascii=False, indent=2))
    elif args.command == "monitor": serve_monitor(args.checkpoint, args.host, args.port, not args.no_browser)
    elif args.command == "evaluate": print(json.dumps(evaluate(args.dataset, args.checkpoint, args.split), ensure_ascii=False, indent=2))
    elif args.command == "infer": print(json.dumps(infer_dataset(args.dataset, args.checkpoint, args.split, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
