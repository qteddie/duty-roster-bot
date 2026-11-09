"""Shared image-to-schedule workflow used by the web API and Discord bot."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
import os
import re
import subprocess
import tempfile
import time
from datetime import date
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageOps

from backend.api_usage import save_usage, summarize_usage, usage_record
from backend.phase1_cell_pipeline import (
    GridAlignmentError,
    GridTemplate,
    correct_perspective,
    registered_grid_lines,
    render_corrected_roster_preview,
)
from backend.phase1_multilabel_compare import (
    create_employee_evidence_image,
    recognize_source_multilabel,
    resolve_assignments_from_final,
)
from backend.preprocessing import PreprocessResult, highlighted_roster_crop, preprocess_roster
from backend.batch_reader import read_roster_batch, read_roster_batch_from_markdown
from schedule_reader import EMPLOYEES, RosterProfile, ScheduleExtraction, result_dict

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ScheduleUploadError(ValueError):
    """A safe, user-facing validation error for an uploaded roster image."""


def _valid_prefix_number(value: str, *, maximum: int) -> int | None:
    for width in (2, 1):
        if len(value) >= width:
            number = int(value[:width])
            if 1 <= number <= maximum:
                return number
    return None


def _parse_roster_header_date(text: str) -> str | None:
    """Parse noisy numeric OCR such as ``202649 A258`` as 2026-09-25."""
    for line in text.upper().splitlines():
        year_match = re.search(r"(20\d{2})", line)
        if year_match is None:
            continue
        year = int(year_match.group(1))
        digits = "".join(re.findall(r"\d", line[year_match.end():]))
        # In this printed header Tesseract commonly reads 年 as a leading 4
        # and 日 as a trailing 8: 2026年9月23日 -> 202649238.
        if len(digits) >= 4 and digits.startswith("4"):
            digits = digits[1:]
        if len(digits) >= 3 and digits.endswith("8"):
            digits = digits[:-1]
        if len(digits) < 2:
            continue
        month_width = 2 if len(digits) >= 3 and 10 <= int(digits[:2]) <= 12 else 1
        month = int(digits[:month_width])
        day_digits = digits[month_width:]
        day = _valid_prefix_number(day_digits, maximum=31)
        if not 1 <= month <= 12 or day is None:
            continue
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return None


def extract_roster_document_date(source: Path) -> str | None:
    """Read the printed date from the upper roster header without guessing."""
    try:
        with Image.open(source) as raw:
            image = ImageOps.exif_transpose(raw).convert("L")
        width, height = image.size
        header = image.crop((
            round(width * .28), round(height * .01),
            round(width * .92), round(height * .16),
        )).resize((round(width * 1.28), round(height * .30)))
        header = ImageOps.autocontrast(header)
        buffer = BytesIO()
        header.save(buffer, format="PNG")
        completed = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "11"],
            input=buffer.getvalue(), capture_output=True, timeout=6, check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _parse_roster_header_date(completed.stdout.decode("utf-8", errors="replace"))


def _reconcile_markdown_employee(employee: dict, *, final: list[dict],
                                 template: GridTemplate) -> dict:
    """Keep the LINE text identical to the Phase 1 cells used by its proof image."""
    code = employee["employee_code"]
    assignments, conflicts = resolve_assignments_from_final(
        final, template, employee_code=code,
    )
    roster_profile = (
        RosterProfile.DOMESTIC_CONTROL
        if (
            any(item["shift"].startswith("國內") for item in assignments)
            or any(
                candidate.get("shift", "").startswith("國內")
                for conflict in conflicts
                for candidate in conflict.get("candidates", [])
            )
        )
        else RosterProfile.INTERNATIONAL
    )
    extraction = ScheduleExtraction(
        document_date=employee.get("document_date"),
        employee_name=EMPLOYEES[code],
        employee_code=code,
        roster_profile=roster_profile,
        assignments=assignments,
        warnings=employee.get("warnings", []),
    )
    data = result_dict(extraction)
    data["assignment_conflicts"] = conflicts
    if conflicts:
        data["is_day_off"] = False
        data["warnings"].append(
            f"{len(conflicts)} 個時段辨識到多個班次，需人工確認。"
        )
    return data


def _line_phase1_template() -> Path:
    configured = os.getenv("LINE_PHASE1_TEMPLATE", "").strip()
    path = Path(configured) if configured else (
        Path(__file__).resolve().parents[1] / "configs" / "phase1_roster_template.local.json"
    )
    if not path.is_file():
        raise ScheduleUploadError(
            "LINE 逐格辨識設定檔不存在；請設定 LINE_PHASE1_TEMPLATE。"
        )
    return path


def _roster_resnet_checkpoint() -> Path:
    configured = os.getenv("ROSTER_RESNET_CHECKPOINT", "").strip()
    path = Path(configured) if configured else (
        Path(__file__).resolve().parents[1] / "models" / "roster-resnet18.pt"
    )
    if not path.is_file():
        raise ScheduleUploadError(
            "本機 ResNet checkpoint 不存在；請先訓練模型或設定 ROSTER_RESNET_CHECKPOINT。"
        )
    return path


def create_corrected_schedule_preview(
    payload: bytes,
    *,
    content_type: str | None,
) -> dict[str, bytes | str]:
    """Return the perspective-corrected roster before any model recognition."""
    if content_type not in ALLOWED_TYPES:
        raise ScheduleUploadError("只接受 JPG、PNG 或 WEBP 圖片")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ScheduleUploadError("圖片不可超過 15 MB")
    if not payload:
        raise ScheduleUploadError("上傳的圖片是空的")

    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
    with tempfile.TemporaryDirectory(prefix="roster-preview-") as temp_dir:
        source = Path(temp_dir) / f"source{suffix}"
        source.write_bytes(payload)
        template = GridTemplate.from_json(_line_phase1_template())
        corrected, _ = correct_perspective(source, template)
        try:
            grid_lines = registered_grid_lines(corrected, template)
        except GridAlignmentError as exc:
            raise ScheduleUploadError(str(exc)) from exc
        corrected = render_corrected_roster_preview(corrected, template, grid_lines)
        output = BytesIO()
        corrected.convert("RGB").save(
            output, format="JPEG", quality=92, optimize=True, progressive=True
        )
    return {"filename": "perspective-corrected.jpg", "content": output.getvalue()}


async def analyze_schedule_image(
    payload: bytes,
    *,
    filename: str | None,
    content_type: str | None,
    all_employees: bool = False,
    include_crop_images: bool = False,
    use_final_markdown: bool = False,
    artifacts_dir: Path | None = None,
    compact_artifacts: bool = False,
) -> dict:
    """Extract a schedule; optionally retain a per-request diagnostic directory."""
    if content_type not in ALLOWED_TYPES:
        raise ScheduleUploadError("只接受 JPG、PNG 或 WEBP 圖片")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ScheduleUploadError("圖片不可超過 15 MB")
    if not payload:
        raise ScheduleUploadError("上傳的圖片是空的")
    local_checkpoint = _roster_resnet_checkpoint() if use_final_markdown else None
    if not use_final_markdown and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("伺服器尚未設定 OPENAI_API_KEY")

    started = time.monotonic()
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
    if artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=False)
    workspace = (nullcontext(str(artifacts_dir)) if artifacts_dir is not None
                 else tempfile.TemporaryDirectory(prefix="roster-"))
    with workspace as temp_dir:
        temp_root = Path(temp_dir)
        source = temp_root / f"source{suffix}"
        cropped = temp_root / "cropped.png"
        source.write_bytes(payload)
        document_date = (
            await asyncio.to_thread(extract_roster_document_date, source)
            if use_final_markdown else None
        )

        # The local classifier performs perspective correction and grid crops.
        # Legacy strip/zoom preparation only feeds the remote image reader.
        if use_final_markdown:
            prepared = PreprocessResult(
                image_path=source,
                method="local_resnet_grid",
                zoom_image_paths=[],
            )
        else:
            prepared = await asyncio.to_thread(preprocess_roster, source, cropped)
        preprocessing_seconds = time.monotonic() - started
        model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
        codes = list(EMPLOYEES) if all_employees else ["己"]
        markdown_recognition = None
        phase1_template = None
        if use_final_markdown:
            phase_records: list[dict] = []

            def record_response(stage, response, elapsed):
                if artifacts_dir is None:
                    return
                folder = artifacts_dir / "api" / stage
                folder.mkdir(parents=True, exist_ok=True)
                index = len(list(folder.glob("*.json"))) + 1
                data = {"elapsed_seconds": elapsed, "response": response.model_dump(mode="json", warnings=False)}
                (folder / f"{index:02d}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))

            def observe_usage(response, elapsed: float) -> None:
                record = usage_record(response, model, elapsed)
                phase_records.append(record)
                save_usage(record)
                if artifacts_dir is not None:
                    (artifacts_dir / "phase-usage.json").write_text(json.dumps(phase_records, indent=2))
                record_response("cells", response, elapsed)

            recognition_started = time.monotonic()
            phase1_template = GridTemplate.from_json(_line_phase1_template())
            if artifacts_dir is not None:
                phase1_template.to_json(artifacts_dir / "template.json")
            try:
                markdown_recognition = await asyncio.to_thread(
                    recognize_source_multilabel,
                    source,
                    phase1_template,
                    model=model,
                    resnet_checkpoint=local_checkpoint,
                    usage_observer=observe_usage,
                    compact_artifacts=compact_artifacts,
                    **({"artifacts_dir": artifacts_dir / "grid"} if artifacts_dir is not None else {}),
                )
            except GridAlignmentError as exc:
                raise ScheduleUploadError(str(exc)) from exc
            # Persist Phase 1 before the text-only Luna call, so the returned
            # lifetime total includes every API call made for this LINE request.
            if artifacts_dir is not None:
                (artifacts_dir / "recognition.json").write_text(json.dumps(markdown_recognition, ensure_ascii=False, indent=2))
                (artifacts_dir / "recognition.md").write_text(markdown_recognition["markdown_grid"] + "\n")
            # The local model's cell labels are already the source of truth;
            # construct employee schedules deterministically without a second
            # text-only Luna request.
            assembly_started = time.monotonic()
            batch = {
                "employees": [
                    _reconcile_markdown_employee(
                        {"employee_code": code, "document_date": document_date, "warnings": []},
                        final=markdown_recognition["final"], template=phase1_template,
                    )
                    for code in codes
                ],
                "usage": summarize_usage([]), "usage_lifetime": summarize_usage([]),
                "calls": [],
            }
            result_assembly_seconds = time.monotonic() - assembly_started
            batch["recognition"] = {
                key: value for key, value in markdown_recognition.items() if key != "final"
            }
            batch["timing_phase1_seconds"] = round(time.monotonic() - recognition_started, 2)
        else:
            batch = await asyncio.to_thread(
                read_roster_batch, prepared.image_path, prepared.zoom_image_paths or [],
                model=model,
                codes=codes,
            )
        stage_timings = {"preprocessing_seconds": round(preprocessing_seconds, 4)}
        if markdown_recognition is not None:
            stage_timings.update(markdown_recognition.get("timing", {}))
            stage_timings["employee_result_assembly_seconds"] = round(result_assembly_seconds, 4)
        batch["timing"] = {"stages": stage_timings}
        evidence_seconds = 0.0
        for employee in batch["employees"]:
            is_day_off = employee.get("is_day_off", not employee.get("assignments"))
            schedule_type = (
                "off" if is_day_off
                else "domestic" if employee["roster_profile"] == "domestic_control"
                else "international"
            )
            employee["schedule_type"] = schedule_type
            employee["preprocessing"] = {
                "method": prepared.method,
            }
            if markdown_recognition is not None:
                employee["recognition_source"] = "local_resnet18_epoch23"
            if include_crop_images and schedule_type != "off":
                evidence_started = time.monotonic()
                crop_path = (
                    create_employee_evidence_image(
                        source, phase1_template, final=markdown_recognition["final"],
                        employee_code=employee["employee_code"],
                        destination=temp_root / f"{employee['employee_code']}-marked-roster.jpg",
                    )
                    if markdown_recognition is not None and phase1_template is not None
                    else None
                )
                if crop_path is None:
                    cell_boxes = [
                        item["cell_box"]
                        for item in employee.get("assignments", [])
                        if item.get("cell_box")
                    ]
                    if cell_boxes:
                        crop_path = highlighted_roster_crop(
                            prepared.image_path,
                            temp_root / f"{employee['employee_code']}-highlighted.jpg",
                            cell_boxes,
                        )
                if crop_path is None:
                    crop_path = (
                        prepared.domestic_crop_path if schedule_type == "domestic"
                        else prepared.international_crop_path
                    )
                if crop_path is not None and crop_path.is_file():
                    employee["crop_image"] = {
                        "filename": crop_path.name,
                        "content": crop_path.read_bytes(),
                    }
                evidence_seconds += time.monotonic() - evidence_started
        batch["timing"]["stages"]["evidence_render_seconds"] = round(evidence_seconds, 4)
        batch["timing"]["total_seconds"] = round(time.monotonic() - started, 4)
        if all_employees:
            return batch
        return {
            **batch["employees"][0],
            "usage": batch["usage"],
            "usage_lifetime": batch["usage_lifetime"],
            "timing": batch["timing"],
        }
