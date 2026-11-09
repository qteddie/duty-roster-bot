#!/usr/bin/env python3
"""LINE Messaging API webhook for the daily roster reader."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from uuid import uuid4

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

from backend.schedule_service import (
    ScheduleUploadError,
    analyze_schedule_image,
    create_corrected_schedule_preview,
)

load_dotenv()

LINE_API_BASE = "https://api.line.me/v2/bot/message"
LINE_DATA_BASE = "https://api-data.line.me/v2/bot/message"
LINE_TEXT_LIMIT = 5_000
LINE_MESSAGES_PER_REQUEST = 5
DISPLAY_CONFIDENCE_THRESHOLD = 0.5
CROP_DIRECTORY = Path(
    os.getenv("LINE_CROP_DIR", Path(__file__).resolve().parent / "data" / "line_crops")
).resolve()
CROP_DIRECTORY.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Work Schedule LINE Bot")
app.mount("/line-crops", StaticFiles(directory=str(CROP_DIRECTORY)), name="line-crops")
analysis_lock = asyncio.Lock()


@app.middleware("http")
async def _cache_line_images(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/line-crops/") and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=86400, immutable"
    return response


def _setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"尚未設定 {name}")
    return value


def _request(method: str, url: str, *, body: bytes | None = None) -> tuple[bytes, str]:
    request = UrlRequest(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {_setting('LINE_CHANNEL_ACCESS_TOKEN')}",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read(), response.headers.get_content_type()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LINE API {exc.code}: {detail}") from exc


async def _send_api_message(endpoint: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await asyncio.to_thread(_request, "POST", f"{LINE_API_BASE}/{endpoint}", body=body)


async def _reply(reply_token: str, text: str) -> None:
    await _send_api_message("reply", {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:LINE_TEXT_LIMIT]}],
    })


async def _push_messages(target_id: str, messages: list[dict[str, str]]) -> None:
    for start in range(0, len(messages), LINE_MESSAGES_PER_REQUEST):
        await _send_api_message("push", {
            "to": target_id,
            "messages": messages[start:start + LINE_MESSAGES_PER_REQUEST],
        })


def _is_visible(item: dict[str, Any]) -> bool:
    confidence = item.get("confidence", 1)
    return isinstance(confidence, (int, float)) and confidence >= DISPLAY_CONFIDENCE_THRESHOLD


def _needs_confirmation(item: dict[str, Any]) -> bool:
    confidence = item.get("confidence", 1)
    return not isinstance(confidence, (int, float)) or confidence < 0.8 or bool(item.get("note"))


MOBILE_SHIFT_NAMES = {
    "國際線": "國際線",
    "國際線（二）": "國際二線",
    "國際線備勤": "國際備勤",
    "國內線一線": "國內一線",
    "國內線二線": "國內二線",
    "國內線備勤": "國內備勤",
    "國內工作管制門": "管制門",
}


def _mobile_shift_name(value: Any) -> str:
    label = str(value or "未知")
    return MOBILE_SHIFT_NAMES.get(label, label)


def _display_document_date(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return ""
    return f"{parsed.month}/{parsed.day}"


def format_line_report(result: dict[str, Any]) -> str:
    """Format a result as one LINE text message with blank uncertain slots."""
    name = result.get("employee_name", "員工")
    code = result.get("employee_code", "")
    conflicts = {
        item.get("time", "--"): item
        for item in result.get("assignment_conflicts", [])
    }
    is_day_off = result.get(
        "is_day_off", not result.get("assignments") and not conflicts,
    )
    date_text = _display_document_date(result.get("document_date"))
    heading = f"{name}（{code}）" if code else name
    lines = [f"{heading}{date_text}"]
    if is_day_off:
        return "\n".join([*lines, "當日休假"])

    assignments_by_time: dict[str, list[dict[str, Any]]] = {}
    for item in result.get("assignments", []):
        assignments_by_time.setdefault(item.get("time", "--"), []).append(item)
    slots = result.get("shift_slots") or sorted({*assignments_by_time, *conflicts})
    slots = sorted({*slots, *conflicts})
    visible_by_slot = {
        slot: [item for item in assignments_by_time.get(slot, []) if _is_visible(item)]
        for slot in slots
    }
    working_hours = sum(any(item.get("needs_work") for item in visible) for visible in visible_by_slot.values())
    resting_hours = sum(bool(visible) and not any(item.get("needs_work") for item in visible)
                        for visible in visible_by_slot.values())
    conflict_hours = len(conflicts)
    conflict_summary = f"｜待確認{conflict_hours}h" if conflict_hours else ""
    lines.append(
        f"摘要｜上班{working_hours}h｜休息{resting_hours}h{conflict_summary}"
    )
    for slot in slots:
        if slot in conflicts:
            candidates = conflicts[slot].get("candidates", [])
            candidate_text = "／".join(
                f"{_mobile_shift_name(item.get('shift'))}{float(item.get('confidence', 0)):.0%}"
                for item in candidates
            )
            lines.append(f"• {slot}｜待確認")
            if candidate_text:
                lines.append(f"　{candidate_text}")
            continue
        visible = visible_by_slot[slot]
        if not visible:
            lines.append(f"• {slot}｜—")
            continue
        duties = "／".join(
            f"{'上班' if item.get('needs_work') else '休息'}"
            f"{'?' if _needs_confirmation(item) else ''}"
            f"｜{_mobile_shift_name(item.get('shift'))}"
            for item in visible
        )
        lines.append(f"• {slot}｜{duties}")
    return "\n".join(lines)[:LINE_TEXT_LIMIT]


def _fetch_image(message_id: str) -> tuple[bytes, str]:
    return _request("GET", f"{LINE_DATA_BASE}/{message_id}/content")


def _jpeg_bytes(content: bytes, *, max_edge: int, quality: int) -> bytes:
    """Create a compact, correctly oriented JPEG for LINE image delivery."""
    with Image.open(BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, "JPEG", quality=quality, optimize=True, progressive=True)
        return output.getvalue()


def _save_crop(crop: dict[str, Any]) -> dict[str, str] | None:
    public_base = os.getenv("LINE_PUBLIC_BASE_URL", "").rstrip("/")
    if not public_base.startswith("https://"):
        return None
    image_id = uuid4().hex
    original_name = f"{image_id}-original.jpg"
    preview_name = f"{image_id}-preview.jpg"
    CROP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    (CROP_DIRECTORY / original_name).write_bytes(
        _jpeg_bytes(crop["content"], max_edge=2400, quality=85)
    )
    (CROP_DIRECTORY / preview_name).write_bytes(
        _jpeg_bytes(crop["content"], max_edge=720, quality=72)
    )
    return {
        "original": f"{public_base}/line-crops/{quote(original_name, safe='')}",
        "preview": f"{public_base}/line-crops/{quote(preview_name, safe='')}",
    }


def format_line_error(exc: Exception) -> str:
    """Return an actionable, non-sensitive failure message for LINE users."""
    detail = str(exc)
    if "Incorrect API key" in detail or "invalid_api_key" in detail:
        return "班表辨識無法開始：OpenAI API 金鑰無效或已失效。請通知 Bot 管理員更新設定。"
    if "LINE API 400" in detail and (
        "originalContentUrl" in detail or "previewImageUrl" in detail
    ):
        return (
            "班表已辨識完成，但 LINE 無法傳送結果圖片（圖片網址格式不符合 LINE 規範）。"
            "這是 Bot 設定問題，不是照片不清楚；請稍後再試。"
        )
    if "LINE API" in detail:
        return "班表處理完成，但 LINE 傳送結果時遭拒。請稍後再試；若持續發生請通知 Bot 管理員。"
    if "timed out" in detail.lower() or "connection" in detail.lower():
        return "班表辨識時無法連線到服務。請稍後再試；不需要重新拍攝照片。"
    return "班表辨識在處理過程中暫時失敗。請稍後再試；若持續發生請通知 Bot 管理員。"


def format_line_total_price(result: dict[str, Any]) -> str:
    """Show the complete estimated API charge after every LINE roster reply."""
    usage = result.get("usage") or {}
    amount = usage.get("estimated_usd")
    price = f"US${amount:.6f}" if isinstance(amount, (int, float)) else "費率或用量不足，無法估算"
    return f"本次 API 總價（估算）：{price}｜API {usage.get('api_calls', 0)} 次"


async def _process_image(target_id: str, message_id: str) -> None:
    try:
        async with analysis_lock:
            payload, content_type = await asyncio.to_thread(_fetch_image, message_id)
            image_type = content_type if content_type.startswith("image/") else "image/jpeg"
            corrected = await asyncio.to_thread(
                create_corrected_schedule_preview,
                payload,
                content_type=image_type,
            )
            corrected_urls = _save_crop(corrected)
            if not corrected_urls:
                raise RuntimeError(
                    "LINE_PUBLIC_BASE_URL 必須是 https 網址，才能回傳矯正後圖片"
                )
            await _push_messages(target_id, [{
                "type": "image",
                "originalContentUrl": corrected_urls["original"],
                "previewImageUrl": corrected_urls["preview"],
            }])
            result = await analyze_schedule_image(
                payload,
                filename=f"line-{message_id}.jpg",
                content_type=image_type,
                all_employees=True,
                include_crop_images=True,
                use_final_markdown=True,
            )
        messages: list[dict[str, str]] = []
        for employee in result["employees"]:
            messages.append({"type": "text", "text": format_line_report(employee)})
            crop_urls = _save_crop(employee.get("crop_image", {})) if employee.get("crop_image") else None
            if crop_urls:
                messages.append({
                    "type": "image",
                    "originalContentUrl": crop_urls["original"],
                    "previewImageUrl": crop_urls["preview"],
                })
        await _push_messages(target_id, messages)
    except ScheduleUploadError as exc:
        await _push_messages(target_id, [{"type": "text", "text": str(exc)}])
    except Exception as exc:
        print(f"LINE 班表辨識失敗：{exc}")
        await _push_messages(target_id, [{
            "type": "text",
            "text": format_line_error(exc),
        }])


def _valid_signature(body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    digest = hmac.new(
        _setting("LINE_CHANNEL_SECRET").encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def _target_id(event: dict[str, Any]) -> str | None:
    source = event.get("source", {})
    # Group message events also contain the sender's userId. Follow-up push
    # messages must target the conversation, not that member's 1:1 chat.
    return source.get("groupId") or source.get("roomId") or source.get("userId")


def _is_image_message_event(event: dict[str, Any]) -> bool:
    return (
        event.get("type") == "message"
        and event.get("message", {}).get("type") == "image"
    )


@app.get("/line/health")
def health() -> dict[str, bool]:
    return {
        "status": True,
        "line_token_configured": bool(os.getenv("LINE_CHANNEL_ACCESS_TOKEN")),
        "line_secret_configured": bool(os.getenv("LINE_CHANNEL_SECRET")),
    }


@app.post("/line/callback")
async def callback(request: Request, background_tasks: BackgroundTasks) -> Response:
    body = await request.body()
    if not _valid_signature(body, request.headers.get("x-line-signature")):
        return Response(status_code=400, content="Invalid signature")

    for event in json.loads(body).get("events", []):
        # Text, stickers and every non-image event are intentionally silent.
        if not _is_image_message_event(event):
            continue
        reply_token = event.get("replyToken")
        message = event.get("message", {})
        target_id = _target_id(event)
        if not reply_token or not target_id:
            continue
        await _reply(reply_token, "已收到班表，正在辨識；完成後會在這個聊天室回覆。")
        background_tasks.add_task(_process_image, target_id, message["id"])
    return Response(status_code=200, content="OK")


def main() -> None:
    _setting("LINE_CHANNEL_ACCESS_TOKEN")
    _setting("LINE_CHANNEL_SECRET")
    uvicorn.run(
        "line_bot:app",
        host="0.0.0.0",
        port=int(os.getenv("LINE_BOT_PORT", "8080")),
        reload=False,
    )


if __name__ == "__main__":
    main()
