#!/usr/bin/env python3
"""Local NightCafe generation API (run with: uvicorn app:app --host 127.0.0.1 --port 8090)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DB = ROOT / "db" / "events.db"
AUTOMATION = ROOT / "plugins" / "media" / "nightcafe_automation.py"
MEDIA_DIR = ROOT / "vault" / "media" / "nightcafe"

app = FastAPI(title="NightCafe Local Generation API", version="1.0")
_jobs: dict[str, dict[str, object]] = {}
_lock = threading.Lock()


class GenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=12000)
    model: str | None = None
    format: str | None = Field(None, pattern=r"^\d{1,2}:\d{1,2}$")
    negative_prompt: str = Field(default="", max_length=4000)
    steps: int | None = Field(None, ge=1, le=200)
    live: bool = False


def _run_job(job_id: str, request: GenerationRequest) -> None:
    command = [str(ROOT / "venv" / "bin" / "python3"), str(AUTOMATION), "--db", str(DB), "--prompt", request.prompt,
               "--name", "API Generation", "--sequence", "1", "--run-date", datetime.now(timezone.utc).strftime("%Y-%m-%d")]
    if request.live:
        command.append("--live")
    else:
        command.append("--simulate")
    for flag, value in (("--model", request.model), ("--format", request.format), ("--negative-prompt", request.negative_prompt)):
        if value:
            command.extend([flag, value])
    if request.steps is not None:
        command.extend(["--steps", str(request.steps)])
    env = os.environ.copy()
    try:
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=700, env=env, check=False)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        result = json.loads(lines[-1]) if lines else {"error": completed.stderr[-2000:]}
        status = "COMPLETED" if completed.returncode == 0 else "FAILED"
    except Exception as exc:
        result, status = {"error": str(exc)}, "FAILED"
    with _lock:
        _jobs[job_id].update({"status": status, "result": result})


@app.post("/api/nightcafe/generations", status_code=202)
def start_generation(request: GenerationRequest) -> dict[str, object]:
    job_id = uuid4().hex
    with _lock:
        _jobs[job_id] = {"status": "RUNNING", "created_at": datetime.now(timezone.utc).isoformat()}
    threading.Thread(target=_run_job, args=(job_id, request), daemon=True).start()
    return {"job_id": job_id, "status": "RUNNING"}


@app.get("/api/nightcafe/generations/{job_id}")
def generation_status(job_id: str) -> dict[str, object]:
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="generation not found")
    return {"job_id": job_id, **job}


@app.get("/api/nightcafe/results")
def list_results() -> list[dict[str, object]]:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    allowed = {".png", ".jpg", ".jpeg", ".webp"}
    return [
        {"filename": p.name, "path": str(p), "bytes": p.stat().st_size, "modified_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
        for p in sorted(MEDIA_DIR.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)
        if p.is_file() and p.suffix.lower() in allowed and p.stat().st_size >= 256
    ]
