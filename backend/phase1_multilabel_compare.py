#!/usr/bin/env python3
"""A separate multi-label experiment for already-cut Phase 1 roster cells.

This module deliberately does not alter ``phase1_cell_pipeline.py``.  It reads
the PNGs emitted by an existing ``--crops-dir`` run, so perspective correction,
grid mapping, border removal, and crop geometry are held constant while the
classifier strategy is compared.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field, model_validator

from backend.phase1_cell_pipeline import (
    CellCrop,
    GridAlignmentError,
    GridTemplate,
    canonical_grid_registration,
    TARGET_CODES,
    _image_data_url,
    _complete_regular_grid_lines,
    _markdown_column_headers,
    _pixel_lines,
    align_semantic_row_lines,
    analysis_time_window,
    correct_perspective,
    crop_mapped_cells,
    detect_grid_lines,
    original_grid_projector,
    registered_grid_lines,
    registered_row_models,
    registered_column_models,
    cell_polygon,
    export_crop_artifacts,
    validate_grid_alignment,
)
from schedule_reader import EMPLOYEES, shift_needs_work


KNOWN_COLUMN_RIGHT_ONE = {"984e16ffecd718a1"}


@dataclass(frozen=True)
class TargetPresence:
    """The experimental four-label prediction for one pre-existing cell crop."""

    row: int
    column: int
    ding: bool
    wu: bool
    ji: bool
    geng: bool
    confidence: float

    def active_codes(self) -> tuple[str, ...]:
        return tuple(code for code, present in (
            ("丁", self.ding), ("戊", self.wu), ("己", self.ji), ("庚", self.geng),
        ) if present)


class ModelTargetPresence(BaseModel):
    row: int
    column: int
    ding: bool
    wu: bool
    ji: bool
    geng: bool
    confidence: float = Field(ge=0, le=1)

    def as_presence(self) -> TargetPresence:
        return TargetPresence(**self.model_dump())


class ModelTargetPresenceBatch(BaseModel):
    cells: list[ModelTargetPresence]

    @model_validator(mode="after")
    def unique_cells(self) -> "ModelTargetPresenceBatch":
        indexes = [(item.row, item.column) for item in self.cells]
        if len(indexes) != len(set(indexes)):
            raise ValueError("每個 cell 只能回傳一次")
        return self


class ModelWuGengVerification(BaseModel):
    row: int
    column: int
    wu: bool
    geng: bool
    confidence: float = Field(ge=0, le=1)


class ModelWuGengBatch(BaseModel):
    cells: list[ModelWuGengVerification]

    @model_validator(mode="after")
    def unique_cells(self) -> "ModelWuGengBatch":
        indexes = [(item.row, item.column) for item in self.cells]
        if len(indexes) != len(set(indexes)):
            raise ValueError("每個 cell 只能回傳一次")
        return self


class ReviewGlyph(str, Enum):
    DING = "丁"
    WU = "戊"
    JI = "己"
    GENG = "庚"
    ZI = "子"
    CHOU = "丑"
    YI = "已"
    SI = "巳"
    XU = "戌"
    SHU = "戍"
    YI_B = "乙"
    BING = "丙"
    OTHER = "OTHER"


class ModelGlyphTranscription(BaseModel):
    row: int
    column: int
    glyphs: list[ReviewGlyph] = Field(min_length=1, max_length=4)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def normalize_glyphs(self) -> "ModelGlyphTranscription":
        # Vision models occasionally include OTHER beside a concrete glyph or
        # repeat a glyph. Normalize that harmless formatting drift instead of
        # discarding the entire day's otherwise valid API response.
        self.glyphs = list(dict.fromkeys(self.glyphs))
        if ReviewGlyph.OTHER in self.glyphs and len(self.glyphs) != 1:
            self.glyphs = [glyph for glyph in self.glyphs if glyph != ReviewGlyph.OTHER]
        return self


class ModelGlyphTranscriptionBatch(BaseModel):
    cells: list[ModelGlyphTranscription]


MULTILABEL_INSTRUCTIONS = """
Classify each supplied roster cell as a four-label, multi-label problem. Inspect
the whole center cell; it can contain one to four stacked or side-by-side
symbols. Do not split the cell into fixed halves and do not report non-target
text. For each cell, return only four booleans: whether a complete standalone
丁, 戊, 己, or 庚 appears anywhere in that same cell. More than one can be true.

Use exact glyph matching. Do not mistake Latin I/H/M/B/F/D, Arabic digits,
serifs, 乙, 丙, or similar Chinese glyphs for a target. A genuine target may be
above, below, or beside numbers/letters in the same cell; judge every visible
symbol independently. False positives are worse than false negatives.
""".strip()


PRECISION_REVIEW_INSTRUCTIONS = """
Transcribe the complete standalone Chinese glyphs visibly present in each exact
roster cell. Use only this closed vocabulary: 丁, 戊, 己, 庚, 子, 丑, 已, 巳,
戌, 戍, 乙, 丙, or OTHER. This is transcription, not target detection: preserve
子 as 子 and 丑 as 丑; never normalize a lookalike into 丁, 戊, 己, or 庚.

Each image includes neighboring cells for context. Transcribe only glyphs whose
visual center lies inside the red rectangle. Ignore digits, Latin letters, grid
lines, and glyphs centered outside the red rectangle even when their strokes
cross into it. A glyph centered on the red border still belongs to the target
cell when most of its ink is inside. A common valid compact pair is 己戊: read
the left glyph as 己 and the right glyph as 戊, return both, and never merge or
normalize the pair into 庚. If the complete Chinese glyph is not in the
vocabulary or is uncertain, return only OTHER. Multiple listed glyphs may be
returned only when each glyph center lies inside the same red rectangle. False
target transcriptions are worse than OTHER.
""".strip()


WU_GENG_INSTRUCTIONS = """
This is a focused 戊／庚 verifier for the supplied center roster cell. Ignore all
other glyphs and inspect the entire cell without fixed top/bottom splitting.
Return whether a complete standalone 戊 is present and whether a complete
standalone 庚 is present; both can be true and both can be false. Do not confuse
乙, 丙, Latin letters, digits, serifs, or fragments with either target. If any
glyph is unclear, return false rather than guessing. Pay special attention to
crowded cells: 庚 may be stacked beside another target or tightly surrounded by
numbers/letters, but it must still be checked as an independent glyph.
""".strip()


def load_existing_cell_crops(cells_dir: Path, template: GridTemplate) -> list[CellCrop]:
    """Load exactly the saved Phase 1 PNG inputs; never regenerate a crop here."""
    cells: list[CellCrop] = []
    for row in sorted(template.row_map):
        for column in sorted(template.column_map):
            path = cells_dir / f"r{row:02d}_c{column:02d}.png"
            if not path.is_file():
                raise FileNotFoundError(f"缺少既有裁切檔：{path}")
            with Image.open(path) as source:
                image = source.convert("RGB").copy()
            cells.append(CellCrop(row=row, column=column,
                                  bounds=(0, 0, image.width, image.height), image=image))
    return cells


def load_saved_primary(path: Path, cells: list[CellCrop]) -> dict[tuple[int, int], TargetPresence]:
    """Reuse a prior experimental first pass without uploading the same cells again."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    saved = payload.get("primary")
    if not isinstance(saved, list):
        raise ValueError("重用檔案缺少 primary multi-label 結果")
    output = {
        (item["row"], item["column"]): TargetPresence(**item)
        for item in saved
    }
    expected = {(cell.row, cell.column) for cell in cells}
    if set(output) != expected:
        raise ValueError("重用檔案的 cell index 與既有裁切檔不一致")
    return output


def _classify_batches(cells: list[CellCrop], *, model: str, instructions: str,
                      response_format: type[BaseModel], client: OpenAI | None,
                      batch_size: int,
                      usage_observer: Callable[[object, float], None] | None = None,
                      cell_annotations: dict[tuple[int, int], str] | None = None,
                      ) -> dict[tuple[int, int], BaseModel]:
    if batch_size < 1:
        raise ValueError("batch_size 必須大於 0")
    if not cells:
        return {}
    if client is None and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("尚未設定 OPENAI_API_KEY")
    api = client or OpenAI(timeout=180, max_retries=0)
    output: dict[tuple[int, int], BaseModel] = {}
    for start in range(0, len(cells), batch_size):
        batch = cells[start:start + batch_size]
        requested = [(cell.row, cell.column) for cell in batch]
        content: list[dict] = [{
            "type": "input_text",
            "text": "Classify only these cells: " + json.dumps(requested, ensure_ascii=False),
        }]
        for cell in batch:
            annotation = (cell_annotations or {}).get((cell.row, cell.column), "")
            text = f"cell row={cell.row}, column={cell.column}"
            if annotation:
                text += "; " + annotation
            content.append({"type": "input_text", "text": text})
            content.append({"type": "input_image", "image_url": _image_data_url(cell.image), "detail": "original"})
        started = time.monotonic()
        response = api.responses.parse(
            model=model,
            instructions=instructions,
            input=[{"role": "user", "content": content}],
            text_format=response_format,
        )
        if usage_observer is not None:
            usage_observer(response, time.monotonic() - started)
        parsed = response.output_parsed
        returned = {(item.row, item.column): item for item in parsed.cells} if parsed else {}
        output.update({index: returned[index] for index in requested if index in returned})
    return output


def classify_multilabel(cells: list[CellCrop], *, model: str, client: OpenAI | None = None,
                        batch_size: int = 48,
                        usage_observer: Callable[[object, float], None] | None = None) -> dict[tuple[int, int], TargetPresence]:
    raw = _classify_batches(cells, model=model, instructions=MULTILABEL_INSTRUCTIONS,
                            response_format=ModelTargetPresenceBatch, client=client,
                            batch_size=batch_size, usage_observer=usage_observer)
    output: dict[tuple[int, int], TargetPresence] = {}
    for cell in cells:
        item = raw.get((cell.row, cell.column))
        output[(cell.row, cell.column)] = (
            item.as_presence() if item is not None
            else TargetPresence(cell.row, cell.column, False, False, False, False, 0.0)
        )
    return output


def verify_wu_geng(cells: list[CellCrop], *, model: str, client: OpenAI | None = None,
                   batch_size: int = 48,
                   usage_observer: Callable[[object, float], None] | None = None) -> dict[tuple[int, int], ModelWuGengVerification]:
    raw = _classify_batches(cells, model=model, instructions=WU_GENG_INSTRUCTIONS,
                            response_format=ModelWuGengBatch, client=client,
                            batch_size=batch_size, usage_observer=usage_observer)
    return {index: item for index, item in raw.items() if isinstance(item, ModelWuGengVerification)}


def verify_multilabel(cells: list[CellCrop], *, model: str, client: OpenAI | None = None,
                      batch_size: int = 48,
                      usage_observer: Callable[[object, float], None] | None = None
                      ) -> dict[tuple[int, int], TargetPresence]:
    """Run a precision-first independent check of every positive first pass."""
    raw = _classify_batches(
        cells, model=model, instructions=PRECISION_REVIEW_INSTRUCTIONS,
        response_format=ModelGlyphTranscriptionBatch, client=client,
        batch_size=batch_size, usage_observer=usage_observer,
    )
    output = {}
    for index, item in raw.items():
        if not isinstance(item, ModelGlyphTranscription):
            continue
        glyphs = set(item.glyphs)
        output[index] = TargetPresence(
            row=item.row, column=item.column,
            ding=ReviewGlyph.DING in glyphs,
            wu=ReviewGlyph.WU in glyphs,
            ji=ReviewGlyph.JI in glyphs,
            geng=ReviewGlyph.GENG in glyphs,
            confidence=item.confidence,
        )
    return output


def intersect_multilabel(primary: TargetPresence,
                         review: TargetPresence | None) -> TargetPresence:
    """Accept a target only when two independent image readings agree."""
    if review is None:
        return TargetPresence(primary.row, primary.column, False, False, False, False, 0.0)
    return TargetPresence(
        row=primary.row,
        column=primary.column,
        ding=primary.ding and review.ding,
        wu=primary.wu and review.wu,
        ji=primary.ji and review.ji,
        geng=primary.geng and review.geng,
        confidence=min(primary.confidence, review.confidence),
    )


def needs_precision_review(prediction: TargetPresence) -> bool:
    """Review ambiguity-prone labels without rechecking every clear 丁/戊."""
    active = prediction.active_codes()
    return (
        len(active) > 1
        or prediction.ji
        or prediction.geng
        or (bool(active) and prediction.confidence < 0.95)
    )


def apply_wu_geng_override(primary: TargetPresence,
                           verifier: ModelWuGengVerification | None) -> TargetPresence:
    """Replace only the 戊／庚 pair when the focused verifier was requested."""
    if verifier is None:
        return primary
    return TargetPresence(
        row=primary.row, column=primary.column,
        ding=primary.ding, wu=verifier.wu, ji=primary.ji, geng=verifier.geng,
        confidence=min(primary.confidence, verifier.confidence),
    )


def _has_dense_ink(cell: CellCrop) -> bool:
    """Identify unusually crowded cell crops without trying to read their text."""
    grayscale = cell.image.convert("L")
    pixels = list(grayscale.getdata())
    if not pixels:
        return False
    dark_fraction = sum(pixel < 110 for pixel in pixels) / len(pixels)
    return dark_fraction >= 0.10


def wu_geng_candidates(cells: list[CellCrop],
                       primary: dict[tuple[int, int], TargetPresence]) -> list[CellCrop]:
    """Recheck possible hidden 庚 entries without re-sending every empty cell.

    A primary 戊／丁／己 hit means the cell already contains meaningful text, so
    庚 can be missed when it is tightly stacked alongside it.  Completely missed
    crowded cells are also included via a conservative ink-density trigger.
    """
    return [
        cell for cell in cells
        if not primary[(cell.row, cell.column)].geng
        and (
            primary[(cell.row, cell.column)].ding
            or primary[(cell.row, cell.column)].wu
            or primary[(cell.row, cell.column)].ji
            or _has_dense_ink(cell)
        )
    ]


def wu_only_candidates(cells: list[CellCrop],
                       primary: dict[tuple[int, int], TargetPresence]) -> list[CellCrop]:
    """Compatibility alias for callers of the original experiment helper."""
    return wu_geng_candidates(cells, primary)


def _display_codes(prediction: TargetPresence) -> str:
    return "+".join(prediction.active_codes()) or "·"


def render_multilabel_grid(predictions: dict[tuple[int, int], TargetPresence],
                           template: GridTemplate) -> str:
    columns = sorted(template.column_map)
    headers = _markdown_column_headers(template)
    lines = [
        "| 時段 | " + " | ".join(headers[column] for column in columns) + " |",
        "| --- | " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in sorted(template.row_map):
        values = [_display_codes(predictions[(row, column)]) for column in columns]
        lines.append("| " + template.row_map[row] + " | " + " | ".join(values) + " |")
    lines.extend(["", "圖例：`丁+戊` 代表此獨立 multi-label 實驗在同一格看見兩個目標；`·` 代表四個目標皆否。"])
    return "\n".join(lines)


PRIMARY_INNER_FRACTION = 0.96
PRECISION_REVIEW_BATCH_SIZE = 1
ASSIGNMENT_CONFLICT_MARGIN = 0.15
# Compatibility name used by the simulation snapshot metadata.
PRODUCTION_INNER_FRACTION = PRIMARY_INNER_FRACTION


def highlighted_context_cells(image: Image.Image, cells: list[CellCrop], *,
                              origin: tuple[int, int] = (0, 0),
                              scale: int = 3) -> list[CellCrop]:
    """Show neighboring cells while marking the exact cell to transcribe.

    Handwritten glyphs often cross a grid rule. A cell-only crop makes that
    neighboring fragment indistinguishable from a second glyph in the target
    cell, so precision review needs the boundary and nearby glyph centers.
    """
    origin_x, origin_y = origin
    output: list[CellCrop] = []
    for cell in cells:
        left, top, right, bottom = cell.bounds
        width, height = right - left, bottom - top
        context = (
            max(origin_x, left - width), max(origin_y, top - height),
            min(origin_x + image.width, right + width),
            min(origin_y + image.height, bottom + height),
        )
        local_context = tuple(value - offset for value, offset in zip(
            context, (origin_x, origin_y, origin_x, origin_y), strict=True,
        ))
        crop = image.crop(local_context)
        draw = ImageDraw.Draw(crop)
        target = (
            left - context[0], top - context[1],
            right - context[0] - 1, bottom - context[1] - 1,
        )
        draw.rectangle(target, outline=(255, 0, 0), width=2)
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
        output.append(CellCrop(cell.row, cell.column, cell.bounds, crop))
    return output


def recognize_source_multilabel(source: Path, template: GridTemplate, *, model: str,
                                client: OpenAI | None = None, batch_size: int = 48,
                                usage_observer: Callable[[object, float], None] | None = None,
                                artifacts_dir: Path | None = None,
                                compact_artifacts: bool = False,
                                resnet_checkpoint: Path | None = None) -> dict:
    """Production recognition using inset crops and two-pass agreement.

    The legacy 戊/庚 replacement verifier remains available to the comparison
    CLI. Production instead reviews every positive cell and intersects the two
    independent readings, so one pass cannot introduce a new target label.
    """
    stage_started = time.monotonic()
    timings = {}
    source_group = hashlib.sha256(source.read_bytes()).hexdigest()[:16] if source.is_file() else ""
    corrected, correction_method = correct_perspective(source, template)
    timings["perspective_correction_seconds"] = round(time.monotonic() - stage_started, 4)
    stage_started = time.monotonic()
    if artifacts_dir is not None:
        artifacts_dir.parent.mkdir(parents=True, exist_ok=True)
        corrected.save(artifacts_dir.parent / "registration-preview.png")
    raw_projector = original_grid_projector(corrected, template) if template.use_original_image else None
    grid_lines = row_models = None
    row_index_offset = 0
    if raw_projector is not None:
        canonical, correction_method = canonical_grid_registration(corrected, template)
        alignment_score = validate_grid_alignment(canonical, template)
        cells = raw_projector.mapped_cells(inner_fraction=PRIMARY_INNER_FRACTION)
        review_cells = highlighted_context_cells(raw_projector.image, cells)
        analysis_image = raw_projector.analysis_window()
    else:
        alignment_score = validate_grid_alignment(corrected, template)
        try:
            grid_lines = registered_grid_lines(corrected, template)
        except GridAlignmentError:
            if source_group not in KNOWN_COLUMN_RIGHT_ONE:
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
        if source_group in KNOWN_COLUMN_RIGHT_ONE:
            detected_x = grid_lines[0]
            semantic_x = detected_x.copy()
            required_x = max(column + 1 for column in template.column_map) + 1
            for index in range(min(template.column_map), required_x):
                semantic_x[index] = detected_x[index + 1]
            grid_lines = (semantic_x, grid_lines[1])
        expected_y = _pixel_lines(template.grid_y, corrected.height)
        semantic_y, row_index_offset = align_semantic_row_lines(
            grid_lines[1], expected_y, first_body_row=min(template.row_map),
        )
        grid_lines = (grid_lines[0], semantic_y)
        row_models = registered_row_models(corrected, grid_lines[1])
        column_models = (
            [(float(x), 0.0) for x in grid_lines[0]]
            if source_group in KNOWN_COLUMN_RIGHT_ONE
            else registered_column_models(corrected, grid_lines[0])
        )
        analysis_image, origin = analysis_time_window(corrected, template, y_lines=grid_lines[1],
                                                      row_models=row_models)
        cells = crop_mapped_cells(analysis_image, template, origin=origin,
                                  inner_fraction=PRIMARY_INNER_FRACTION,
                                  canvas_size=corrected.size, grid_lines=grid_lines,
                                  row_models=row_models, column_models=column_models)
        review_cells = highlighted_context_cells(analysis_image, cells, origin=origin)
    timings["grid_registration_and_crop_seconds"] = round(time.monotonic() - stage_started, 4)
    stage_started = time.monotonic()
    if artifacts_dir is not None:
        export_crop_artifacts(artifacts_dir, corrected=corrected, analysis_image=analysis_image,
                              cells=cells, template=template, grid_lines=grid_lines,
                              row_models=row_models, compact=compact_artifacts)
    timings["artifact_export_seconds"] = round(time.monotonic() - stage_started, 4)
    configured_checkpoint = os.getenv("ROSTER_RESNET_CHECKPOINT", "").strip()
    checkpoint = resnet_checkpoint or (Path(configured_checkpoint) if configured_checkpoint else None)
    if checkpoint is not None:
        from backend.resnet_classifier import LABELS, RosterResNetClassifier
        stage_started = time.monotonic()
        classifier = RosterResNetClassifier(checkpoint)
        local_predictions = classifier.predict(cell.image for cell in cells)
        timings["resnet_load_and_inference_seconds"] = round(time.monotonic() - stage_started, 4)
        stage_started = time.monotonic()
        primary = {}
        cell_predictions = []
        for cell, prediction in zip(cells, local_predictions, strict=True):
            scores = prediction.probabilities
            active = set(prediction.labels)
            confidence = (min(scores[label] for label in active) if active else
                          1.0 - max(scores.values()))
            primary[(cell.row, cell.column)] = TargetPresence(
                row=cell.row, column=cell.column,
                ding=LABELS[0] in active, wu=LABELS[1] in active,
                ji=LABELS[2] in active, geng=LABELS[3] in active,
                confidence=max(0.0, min(1.0, float(confidence))),
            )
            cell_predictions.append({
                "row": cell.row, "column": cell.column,
                "labels": list(prediction.labels),
                "probabilities": prediction.probabilities,
            })
        markdown_grid = render_multilabel_grid(primary, template)
        timings["result_render_seconds"] = round(time.monotonic() - stage_started, 4)
        timings["total_recognition_seconds"] = round(sum(timings.values()), 4)
        return {
            "markdown_grid": markdown_grid,
            "cell_count": len(cells), "reviewed_cell_count": 0,
            "wu_geng_verifier_count": 0,
            "recognition_policy": "local_resnet18",
            "checkpoint": str(checkpoint),
            "cell_inner_fraction": PRIMARY_INNER_FRACTION,
            "review_crop": None, "review_batch_size": 0,
            "correction_method": correction_method,
            "grid_alignment_score": alignment_score,
            "row_index_offset": row_index_offset,
            "primary": [asdict(primary[(cell.row, cell.column)]) for cell in cells],
            "review": [],
            "final": [asdict(primary[(cell.row, cell.column)]) for cell in cells],
            "cell_predictions": cell_predictions,
            "timing": timings,
        }
    primary = classify_multilabel(cells, model=model, client=client, batch_size=batch_size,
                                  usage_observer=usage_observer)
    review_by_index = {(cell.row, cell.column): cell for cell in review_cells}
    candidates = [review_by_index[(cell.row, cell.column)] for cell in cells
                  if needs_precision_review(primary[(cell.row, cell.column)])]
    claims = {(cell.row, cell.column): primary[(cell.row, cell.column)] for cell in candidates}
    if artifacts_dir is not None:
        review_dir = artifacts_dir / "review-cells"
        review_dir.mkdir(parents=True, exist_ok=True)
        for cell in candidates:
            cell.image.save(review_dir / f"r{cell.row:02d}_c{cell.column:02d}.png", format="PNG")
    reviewed = verify_multilabel(
        candidates, model=model, client=client,
        batch_size=PRECISION_REVIEW_BATCH_SIZE,
        usage_observer=usage_observer,
    )
    final = {
        index: (intersect_multilabel(prediction, reviewed.get(index))
                if index in claims else prediction)
        for index, prediction in primary.items()
    }
    return {
        "markdown_grid": render_multilabel_grid(final, template),
        "cell_count": len(cells),
        "reviewed_cell_count": len(candidates),
        "wu_geng_verifier_count": 0,
        "recognition_policy": "selective_claim_verification",
        "cell_inner_fraction": PRIMARY_INNER_FRACTION,
        "review_crop": "highlighted_3x3_context",
        "review_batch_size": PRECISION_REVIEW_BATCH_SIZE,
        "correction_method": correction_method,
        "grid_alignment_score": alignment_score,
        "primary": [asdict(primary[(cell.row, cell.column)]) for cell in cells],
        "review": [asdict(reviewed[(cell.row, cell.column)]) for cell in candidates
                   if (cell.row, cell.column) in reviewed],
        "final": [asdict(final[(cell.row, cell.column)]) for cell in cells],
    }


def resolve_assignments_from_final(
    final: list[dict], template: GridTemplate, *, employee_code: str,
) -> tuple[list[dict], list[dict]]:
    """Apply the one-duty-per-hour constraint to an employee's positive cells."""
    if employee_code not in EMPLOYEES:
        raise ValueError("不支援的員工代號")
    predictions = {
        (item["row"], item["column"]): TargetPresence(**item)
        for item in final
    }
    assignments: list[dict] = []
    conflicts: list[dict] = []
    for row in sorted(template.row_map):
        # Several physical columns can share one duty header. Collapse those
        # duplicates before deciding whether this is a true cross-duty conflict.
        candidates_by_shift: dict[str, tuple[int, TargetPresence]] = {}
        for column in sorted(template.column_map):
            prediction = predictions.get((row, column))
            if prediction is None or employee_code not in prediction.active_codes():
                continue
            shift = template.column_map[column]
            current = candidates_by_shift.get(shift)
            if current is None or prediction.confidence > current[1].confidence:
                candidates_by_shift[shift] = (column, prediction)
        if not candidates_by_shift:
            continue
        ranked = sorted(
            candidates_by_shift.items(), key=lambda item: item[1][1].confidence, reverse=True,
        )
        if len(ranked) > 1:
            margin = ranked[0][1][1].confidence - ranked[1][1][1].confidence
            if margin < ASSIGNMENT_CONFLICT_MARGIN:
                conflicts.append({
                    "time": template.row_map[row],
                    "reason": "同一時段辨識到多個班次，信心差距不足",
                    "candidates": [
                        {
                            "shift": shift,
                            "confidence": prediction.confidence,
                            "column": column,
                        }
                        for shift, (column, prediction) in ranked
                    ],
                })
                continue
        shift, (column, prediction) = ranked[0]
        assignments.append({
            "time": template.row_map[row],
            "shift": shift,
            "column": column,
            "cell_text": _display_codes(prediction),
            "column_header_evidence": shift,
            "confidence": prediction.confidence,
            "note": "",
            "cell_box": None,
        })
    return assignments, conflicts


def assignments_from_final(final: list[dict], template: GridTemplate, *, employee_code: str) -> list[dict]:
    """Derive one employee's assignments directly from authoritative cell labels.

    The Markdown-to-text model is convenient for the LINE flow, but it must not
    be allowed to omit a cell which Phase 1 has already positively classified
    and which the evidence image will therefore outline.
    """
    assignments, _ = resolve_assignments_from_final(
        final, template, employee_code=employee_code,
    )
    return assignments


DEBUG_OUTLINE_COLOR = (37, 99, 235)
WORK_OUTLINE_COLOR = (220, 38, 38)
REST_OUTLINE_COLOR = (22, 163, 74)


def _legend_font(size: int) -> ImageFont.ImageFont:
    """Use a bold Latin font so the compact legend stays legible in LINE."""
    for font_path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_evidence_legend(image: Image.Image, *, line_width: int) -> None:
    """Put a large English legend slightly left and below the upper-right edge."""
    draw = ImageDraw.Draw(image)
    font_size = max(20, min(40, round(image.width * 0.024)))
    font = _legend_font(font_size)
    labels = (("GRID", DEBUG_OUTLINE_COLOR), ("WORK", WORK_OUTLINE_COLOR), ("REST", REST_OUTLINE_COLOR))
    label_width = max(draw.textbbox((0, 0), label, font=font)[2] for label, _ in labels)
    title_height = draw.textbbox((0, 0), "LEGEND", font=font)[3]
    sample_width = max(round(font_size * 2.3), line_width * 10)
    padding = max(8, round(font_size * 0.45))
    row_height = max(font_size + padding // 2, line_width * 3)
    panel_width = padding * 3 + sample_width + label_width
    panel_height = padding * 2 + title_height + row_height * len(labels)
    margin = max(10, round(font_size * 0.45))
    right_inset = max(margin * 2, round(image.width * 0.10))
    left = max(margin, image.width - right_inset - panel_width)
    top = margin
    right = image.width - right_inset
    draw.rounded_rectangle(
        (left, top, right, top + panel_height), radius=padding,
        fill=(255, 255, 255), outline=(75, 85, 99), width=max(1, line_width // 2),
    )
    text_x = left + padding
    draw.text((text_x, top + padding // 2), "LEGEND", fill=(15, 23, 42), font=font)
    row_top = top + padding + title_height
    sample_left = text_x
    label_left = sample_left + sample_width + padding
    for index, (label, color) in enumerate(labels):
        center_y = row_top + row_height * index + row_height // 2
        draw.line((sample_left, center_y, sample_left + sample_width, center_y),
                  fill=color, width=line_width)
        draw.text((label_left, row_top + row_height * index), label, fill=(15, 23, 42), font=font)


def _evidence_display_row_end(template: GridTemplate) -> int:
    """Return the lower grid line for the LINE proof image.

    Stop at the lower boundary of the final configured hour (currently
    19-20). The proof intentionally retains the source photo's full
    width: a daily roster may shift horizontally, so a fixed right crop can
    hide a valid column such as domestic work control.
    """
    return min(max(template.row_map) + 1, len(template.grid_y) - 1)


def create_employee_evidence_image(source: Path, template: GridTemplate, *, final: list[dict],
                                   employee_code: str, destination: Path) -> Path | None:
    """Mark final assignments on the warped, full-width roster through 19:00.

    The display is intentionally a familiar roster crop, not a new summary
    card: use the perspective-corrected roster, retain its complete horizontal
    extent, and crop only below 19:00. Blue outlines show all OCR cells;
    personal work/rest cells override them with red/green outlines.
    """
    if employee_code not in EMPLOYEES:
        raise ValueError("不支援的員工代號")
    predictions = {
        (item["row"], item["column"]): TargetPresence(**item)
        for item in final
    }
    corrected, _ = correct_perspective(source, template)
    try:
        grid_lines = registered_grid_lines(corrected, template)
        expected_y = _pixel_lines(template.grid_y, corrected.height)
        semantic_y, _ = align_semantic_row_lines(
            grid_lines[1], expected_y, first_body_row=min(template.row_map),
        )
        grid_lines = (grid_lines[0], semantic_y)
        row_models = registered_row_models(corrected, grid_lines[1])
        column_models = registered_column_models(corrected, grid_lines[0])
    except GridAlignmentError:
        # Recognition already verifies registration before this proof image is
        # requested.  Retain a template-coordinate fallback for callers that
        # render an isolated evidence image from a synthetic/no-grid image.
        grid_lines = (
            _pixel_lines(template.grid_x, corrected.width),
            _pixel_lines(template.grid_y, corrected.height),
        )
        row_models = [(float(y), 0.0) for y in grid_lines[1]]
        column_models = [(float(x), 0.0) for x in grid_lines[0]]
    resolved_assignments, conflicts = resolve_assignments_from_final(
        final, template, employee_code=employee_code,
    )
    row_by_time = {time_slot: row for row, time_slot in template.row_map.items()}
    selected = [
        (row_by_time[item["time"]], item["column"])
        for item in resolved_assignments
    ]
    # A resolved hour shows only its winning cell. An unresolved conflict keeps
    # every candidate visible so a person can compare them in the proof image.
    selected.extend(
        (row_by_time[conflict["time"]], candidate["column"])
        for conflict in conflicts
        for candidate in conflict.get("candidates", [])
    )
    if not selected:
        return None
    display_row_end = _evidence_display_row_end(template)
    x_lines, _ = grid_lines
    # Horizontal rules may retain a slight slope even after perspective
    # correction.  Crop below the lowest end of the 19:00 rule so no edge is
    # cut off, and trace each selected cell along that same registered slope.
    bottom = min(
        corrected.height,
        round(max(
            row_models[display_row_end][0] + row_models[display_row_end][1] * x
            for x in (0, corrected.width - 1)
        )),
    )
    marked = corrected.crop((0, 0, corrected.width, bottom))
    boxes = {}
    for row, column in selected:
        left, right = x_lines[column], x_lines[column + 1]
        boxes[(row, column)] = cell_polygon(row, column, grid_lines, row_models, column_models)
    draw = ImageDraw.Draw(marked)
    line_width = max(4, round(min(marked.width, marked.height) * 0.005))
    for row in sorted(template.row_map):
        for column in sorted(template.column_map):
            points = cell_polygon(row, column, grid_lines, row_models, column_models)
            draw.line([*points, points[0]], fill=DEBUG_OUTLINE_COLOR,
                      width=max(2, line_width // 2), joint="curve")
    for row, column in selected:
        points = [
            (max(0, min(marked.width - 1, x)), max(0, min(marked.height - 1, y)))
            for x, y in boxes[(row, column)]
        ]
        if shift_needs_work(template.column_map[column]):
            draw.line([*points, points[0]], fill=WORK_OUTLINE_COLOR, width=line_width, joint="curve")
        else:
            draw.line([*points, points[0]], fill=REST_OUTLINE_COLOR, width=line_width, joint="curve")
    _draw_evidence_legend(marked, line_width=line_width)
    destination.parent.mkdir(parents=True, exist_ok=True)
    marked.save(destination, format="JPEG", quality=92, optimize=True, progressive=True)
    return destination


def _target_signature(value: str) -> str:
    return "+".join(code for code in TARGET_CODES if code in value)


def parse_baseline_grid(payload: dict, template: GridTemplate) -> dict[tuple[int, int], str]:
    """Read the existing Phase 1 JSON Markdown grid without touching its result."""
    markdown = payload.get("markdown_grid")
    if not isinstance(markdown, str):
        raise ValueError("baseline JSON 缺少 markdown_grid；請使用帶 --markdown 的既有結果")
    expected_headers = _markdown_column_headers(template)
    header_line = next((line for line in markdown.splitlines() if line.startswith("| 時段 |")), None)
    if header_line is None:
        raise ValueError("baseline Markdown 找不到表頭")
    actual_headers = [item.strip() for item in header_line.strip("|").split("|")][1:]
    columns = sorted(template.column_map)
    if actual_headers != [expected_headers[column] for column in columns]:
        raise ValueError("baseline 的欄位順序與目前 template 不一致")
    output: dict[tuple[int, int], str] = {}
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        fields = [item.strip() for item in line.strip("|").split("|")]
        if not fields or fields[0] not in template.row_map.values():
            continue
        row = next(index for index, time in template.row_map.items() if time == fields[0])
        for column, value in zip(columns, fields[1:]):
            output[(row, column)] = value
    return output


def render_comparison(baseline: dict[tuple[int, int], str], primary: dict[tuple[int, int], TargetPresence],
                      final: dict[tuple[int, int], TargetPresence],
                      verifier: dict[tuple[int, int], ModelWuGengVerification],
                      template: GridTemplate) -> str:
    rows = [
        "# Multi-label 比較實驗",
        "",
        "僅列出戊／庚 verifier 觸發的格，或與既有結果的目標代號不同的格。既有結果檔沒有被修改。",
        "",
        "| 時段 | 勤務 | Grid | 既有結果 | 初步 multi-label | 戊／庚 verifier | 實驗最終 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(template.row_map):
        for column in sorted(template.column_map):
            index = (row, column)
            old = baseline.get(index, "?")
            first = primary[index]
            resolved = final[index]
            checked = verifier.get(index)
            verification = (f"戊={'是' if checked.wu else '否'}；庚={'是' if checked.geng else '否'}"
                            if checked else "—")
            if checked is None and _target_signature(old) == _target_signature(_display_codes(resolved)):
                continue
            rows.append(
                f"| {template.row_map[row]} | {template.column_map[column]} | c{column:02d} | {old} | "
                f"{_display_codes(first)} | {verification} | {_display_codes(resolved)} |"
            )
    return "\n".join(rows) + "\n"


def run_comparison(*, cells_dir: Path, baseline_json: Path, template: GridTemplate, output: Path,
                   model: str = "gpt-5.6-luna", client: OpenAI | None = None,
                   batch_size: int = 48, reuse_primary: Path | None = None) -> dict:
    """Run the isolated experiment and write only new comparison artifacts."""
    if output.exists():
        raise FileExistsError(f"輸出資料夾已存在，拒絕覆寫：{output}")
    cells = load_existing_cell_crops(cells_dir, template)
    baseline_payload = json.loads(baseline_json.read_text(encoding="utf-8"))
    baseline = parse_baseline_grid(baseline_payload, template)
    primary = (load_saved_primary(reuse_primary, cells) if reuse_primary is not None
               else classify_multilabel(cells, model=model, client=client, batch_size=batch_size))
    pair_candidates = wu_only_candidates(cells, primary)
    verifier = verify_wu_geng(pair_candidates, model=model, client=client, batch_size=batch_size)
    final = {index: apply_wu_geng_override(prediction, verifier.get(index))
             for index, prediction in primary.items()}
    output.mkdir(parents=True)
    grid = render_multilabel_grid(final, template)
    comparison = render_comparison(baseline, primary, final, verifier, template)
    (output / "experimental-grid.md").write_text(grid + "\n", encoding="utf-8")
    (output / "comparison.md").write_text(comparison, encoding="utf-8")
    result = {
        "experiment": "phase1_multilabel_wu_geng_compare",
        "input_cells_dir": str(cells_dir),
        "baseline_json": str(baseline_json),
        "reused_primary": str(reuse_primary) if reuse_primary is not None else None,
        "cell_count": len(cells),
        "wu_geng_verifier_count": len(pair_candidates),
        "primary": [asdict(primary[(cell.row, cell.column)]) for cell in cells],
        "final": [asdict(final[(cell.row, cell.column)]) for cell in cells],
        "verifier": [item.model_dump() for item in verifier.values()],
        "artifacts": {
            "experimental_grid": str(output / "experimental-grid.md"),
            "comparison": str(output / "comparison.md"),
        },
    }
    (output / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
    return result


def _cli() -> int:
    parser = argparse.ArgumentParser(description="獨立 multi-label 與戊庚 verifier 比較實驗")
    parser.add_argument("--cells-dir", type=Path, required=True,
                        help="既有 Phase 1 --crops-dir 之下的 cells 資料夾")
    parser.add_argument("--baseline", type=Path, required=True,
                        help="既有 Phase 1 recognition.json（僅讀取）")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="必須是不存在的新資料夾")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"))
    parser.add_argument("--batch-size", type=int, default=48)
    parser.add_argument("--reuse-primary", type=Path,
                        help="重用先前 comparison.json 的 primary，避免重新上傳相同 cell")
    args = parser.parse_args()
    result = run_comparison(cells_dir=args.cells_dir, baseline_json=args.baseline,
                            template=GridTemplate.from_json(args.template), output=args.output,
                            model=args.model, batch_size=args.batch_size,
                            reuse_primary=args.reuse_primary)
    print(json.dumps({key: value for key, value in result.items() if key not in {"primary", "final", "verifier"}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
