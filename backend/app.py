from __future__ import annotations

import os

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

from backend.schedule_service import (
    ALLOWED_TYPES,
    MAX_UPLOAD_BYTES,
    ScheduleUploadError,
    analyze_schedule_image,
)


app = FastAPI(
    title="蕭志明勤務表辨識 API",
    description="影像前處理與 OpenAI 視覺辨識服務",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "openai_key_configured": bool(os.environ.get("OPENAI_API_KEY")),
    }


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)) -> dict:
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        return await analyze_schedule_image(
            payload, filename=file.filename, content_type=file.content_type
        )
    except ScheduleUploadError as exc:
        status_code = 413 if "15 MB" in str(exc) else 415 if "JPG" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"班表辨識失敗：{exc}") from exc
