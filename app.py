#!/usr/bin/env python3
"""Local NightCafe generation API (run with: uvicorn app:app --host 127.0.0.1 --port 8090)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from core.database import connect_database
from core.paths import CORE_ROOT, DATABASE_PATH, MEDIA_DIR, PUBLISHED_DIR

ROOT = CORE_ROOT
DB = DATABASE_PATH
MEDIA_DIR = MEDIA_DIR / "nightcafe"
PUBLISHED_MEDIA_DIR = PUBLISHED_DIR / "media"

app = FastAPI(title="NightCafe Local Generation API", version="1.0")
class GenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=12000)
    model: str | None = None
    format: str | None = Field(None, pattern=r"^\d{1,2}:\d{1,2}$")
    negative_prompt: str = Field(default="", max_length=4000)
    steps: int | None = Field(None, ge=1, le=200)
    live: bool = False


class OverlayRequest(BaseModel):
    input: str | None = None
    text: str = ""
    border: int = Field(default=24, ge=0, le=200)
    font_size: int = Field(default=34, ge=8, le=160)


@app.post("/api/nightcafe/generations", status_code=202)
def start_generation(request: GenerationRequest) -> dict[str, object]:
    payload = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    payload["mode"] = "LIVE" if request.live else "SIMULATED"
    with connect_database(DB) as connection:
        route = connection.execute(
            "SELECT target_plugin_name FROM event_routes WHERE event_type='NIGHTCAFE_GENERATE'"
        ).fetchone()
        if route is None:
            raise HTTPException(status_code=503, detail="NIGHTCAFE_GENERATE route is not registered")
        cursor = connection.execute(
            "INSERT INTO events_queue(event_type,payload,status,next_attempt_at) VALUES (?,?, 'PENDING', CURRENT_TIMESTAMP)",
            ("NIGHTCAFE_GENERATE", json.dumps(payload, ensure_ascii=False)),
        )
        connection.commit()
        event_id = int(cursor.lastrowid)
    return {"job_id": str(event_id), "event_id": event_id, "status": "PENDING"}


@app.get("/api/nightcafe/generations/{job_id}")
def generation_status(job_id: str) -> dict[str, object]:
    try: event_id = int(job_id)
    except ValueError: raise HTTPException(status_code=400, detail="invalid generation id")
    with connect_database(DB, read_only=True) as connection:
        row = connection.execute("SELECT id,status,payload,error_log,created_at,updated_at FROM events_queue WHERE id=? AND event_type='NIGHTCAFE_GENERATE'", (event_id,)).fetchone()
    if row is None: raise HTTPException(status_code=404, detail="generation not found")
    payload = json.loads(row["payload"] or "{}")
    return {"job_id": str(row["id"]), "event_id": row["id"], "status": row["status"], "result": payload.get("result") or payload.get("nightcafe"), "error": row["error_log"], "created_at": row["created_at"], "updated_at": row["updated_at"]}


@app.get("/api/nightcafe/results")
def list_results() -> list[dict[str, object]]:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    allowed = {".png", ".jpg", ".jpeg", ".webp"}
    return [
        {"filename": p.name, "path": str(p), "bytes": p.stat().st_size, "modified_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
        for p in sorted(MEDIA_DIR.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)
        if p.is_file() and p.suffix.lower() in allowed and p.stat().st_size >= 256
    ]


@app.post("/api/nightcafe/overlay")
def create_overlay(request: OverlayRequest) -> dict[str, object]:
    from plugins.media.image_overlay import apply_overlay, latest_image

    source = (ROOT / request.input).resolve() if request.input else latest_image(MEDIA_DIR)
    if ROOT not in source.parents or not source.is_file():
        raise HTTPException(status_code=400, detail="input must be an existing project image")
    output = PUBLISHED_MEDIA_DIR / f"{source.stem}_overlay.jpg"
    try:
        apply_overlay(source, output, text=request.text, border=request.border, font_size=request.font_size)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "COMPLETED", "source": str(source), "output": str(output)}
