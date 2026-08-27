#!/usr/bin/env python3
"""End-to-end self-test for the Markdown-to-local-LLM event pipeline."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Final


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from core.database import connect_database
DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
SKILL_FILE: Final = PROJECT_ROOT / "vault" / "skills" / "test_skill.md"
PLUGIN: Final = PROJECT_ROOT / "plugins" / "ai" / "gen_local_llm.py"
SETUP_DATABASE: Final = PROJECT_ROOT / "core" / "setup_database.py"
WORKER: Final = PROJECT_ROOT / "daemon" / "worker.py"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(command), flush=True)
    result = subprocess.run(
        command, cwd=PROJECT_ROOT, text=True, capture_output=True, check=False
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    if result.returncode != 0:
        raise RuntimeError(f"command failed with exit code {result.returncode}")
    return result


def main() -> int:
    SKILL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SKILL_FILE.write_text(
        '---\nrequired_inputs: ["topic"]\n---\n\n'
        "Schrijf een beknopt concept over {{topic}}.\n",
        encoding="utf-8",
    )
    print(f"[OK] Test-skill aangemaakt: {SKILL_FILE.relative_to(PROJECT_ROOT)}")

    run([sys.executable, str(SETUP_DATABASE), "--database", str(DATABASE)])
    run([str(PLUGIN), "--register", "--db", str(DATABASE)])

    payload = {
        "skill_file": str(SKILL_FILE.relative_to(PROJECT_ROOT)),
        "inputs": {"topic": "event-driven AI op een Raspberry Pi"},
    }
    with connect_database(DATABASE) as connection:
        cursor = connection.execute(
            "INSERT INTO events_queue (event_type, payload) VALUES (?, ?)",
            ("AI_GENERATION", json.dumps(payload, ensure_ascii=False)),
        )
        event_id = int(cursor.lastrowid)
        connection.commit()
    print(f"[OK] Test-event geïnjecteerd: id={event_id}")

    os.environ["LOCAL_LLM_MOCK"] = "1"
    run([str(WORKER), "--database", str(DATABASE), "--once"])

    with connect_database(DATABASE) as connection:
        row = connection.execute(
            "SELECT status, payload, error_log FROM events_queue WHERE id=?",
            (event_id,),
        ).fetchone()
    if row is None or row[0] != "SIMULATED":
        detail = "event ontbreekt" if row is None else f"status={row[0]} error={row[2]}"
        raise RuntimeError(f"pipeline self-test mislukt: {detail}")

    final_payload = json.loads(row[1])
    output_path = Path(final_payload["filepath"])
    output = output_path.read_text(encoding="utf-8")
    print(f"[OK] Eventstatus: {row[0]}")
    print(f"[OK] Outputbestand: {output_path.relative_to(PROJECT_ROOT)}")
    print("--- PIPELINE OUTPUT ---")
    print(output.rstrip())
    print("--- EINDE PIPELINE OUTPUT ---")
    print("[PASS] End-to-end self-test voltooid zonder fouten.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
