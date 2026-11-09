"""Persist observed Responses usage; prices are estimates, not invoice totals."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICING_DATE = "2026-09-09"


def usage_record(response, requested_model: str, elapsed: float) -> dict:
    raw = response.usage.model_dump() if response.usage is not None else None
    model = response.model or requested_model
    record = {"response_id": response.id, "model": model, "seconds": round(elapsed, 2),
              "usage": raw, "estimated_usd": None, "pricing_date": PRICING_DATE,
              "pricing_source": PRICING_SOURCE}
    if raw is None:
        return record
    inputs, outputs = raw["input_tokens"], raw["output_tokens"]
    details = raw.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0)
    writes = details.get("cache_creation_tokens", details.get("cache_write_tokens", 0))
    # Only verified rates. Unknown models deliberately remain unpriced.
    if model == "gpt-5.6-luna" or model.startswith("gpt-5.6-luna-"):
        rates = (0.40, 0.04, 0.50, 1.80) if inputs > 272000 else (0.20, 0.02, 0.25, 1.20)
    elif model == "gpt-5.4-mini" or model.startswith("gpt-5.4-mini-"):
        rates = (0.75, 0.075, 0.75, 4.50)
    else:
        return record
    tier = getattr(response, "service_tier", "default") or "default"
    if tier not in {"default", "standard"}:
        return record
    record["estimated_usd"] = (
        max(0, inputs - cached - writes) * rates[0] + cached * rates[1]
        + writes * rates[2] + outputs * rates[3]
    ) / 1_000_000
    return record


def summarize_usage(records: list[dict]) -> dict:
    priced = [r["estimated_usd"] for r in records if r["estimated_usd"] is not None]
    return {
        "api_calls": len(records),
        "input_tokens": sum((r["usage"] or {}).get("input_tokens", 0) for r in records),
        "output_tokens": sum((r["usage"] or {}).get("output_tokens", 0) for r in records),
        "estimated_usd": sum(priced) if len(priced) == len(records) else None,
        "known_estimated_usd": sum(priced),
        "unpriced_calls": len(records) - len(priced),
        "models": sorted({r["model"] for r in records}),
    }


def save_usage(record: dict) -> dict:
    """No photos, names, keys or prompts are saved. SQLite survives bot restarts."""
    path = Path(os.getenv("API_USAGE_DB", str(Path(__file__).resolve().parents[1] / "data/api_usage.sqlite3")))
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, created TEXT, record TEXT)")
        connection.execute("INSERT OR IGNORE INTO calls VALUES (?, ?, ?)", (
            record["response_id"], datetime.now(timezone.utc).isoformat(), json.dumps(record),
        ))
        rows = connection.execute("SELECT record FROM calls").fetchall()
    return summarize_usage([json.loads(row[0]) for row in rows])
