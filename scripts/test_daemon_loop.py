#!/usr/bin/env python3
"""One-cycle integration test for the continuous event worker daemon."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Final
from uuid import uuid4


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
SETUP_DATABASE: Final = PROJECT_ROOT / "core" / "setup_database.py"
WORKER: Final = PROJECT_ROOT / "daemon" / "worker.py"
MOCK_PLUGIN: Final = PROJECT_ROOT / "plugins" / "channels" / "pub_mock.py"
PLUGIN_NAME: Final = "Mock Publisher (Test Zandbak)"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command, mirror its logs, and fail on a non-zero exit status."""
    print("$ " + " ".join(command), flush=True)
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    if result.returncode != 0:
        raise RuntimeError(f"command exited with status {result.returncode}")
    return result


def main() -> int:
    """Inject an event and prove that one worker poll handles it automatically."""
    run([sys.executable, str(SETUP_DATABASE), "--database", str(DATABASE)])
    run([str(MOCK_PLUGIN), "--register", "--db", str(DATABASE)])

    event_type = f"test.daemon.mock.{uuid4().hex}"
    payload = {
        "content": "Automatisch verwerkt door de continue worker daemon-test."
    }
    with sqlite3.connect(DATABASE) as connection:
        connection.execute(
            """
            INSERT INTO event_routes (event_type, target_plugin_name)
            VALUES (?, ?)
            """,
            (event_type, PLUGIN_NAME),
        )
        cursor = connection.execute(
            "INSERT INTO events_queue (event_type, payload) VALUES (?, ?)",
            (event_type, json.dumps(payload, ensure_ascii=False)),
        )
        event_id = int(cursor.lastrowid)
        connection.commit()
    print(f"[OK] PENDING test-event geïnjecteerd: id={event_id}")

    run([str(WORKER), "--database", str(DATABASE), "--once"])

    with sqlite3.connect(DATABASE) as connection:
        row = connection.execute(
            "SELECT status, retry_count, error_log FROM events_queue WHERE id = ?",
            (event_id,),
        ).fetchone()
        connection.execute("DELETE FROM event_routes WHERE event_type = ?", (event_type,))
        connection.commit()

    if row is None:
        raise RuntimeError("test-event is onverwacht uit de actieve queue verdwenen")
    status, retry_count, error_log = row
    print(
        f"[OK] Eindstatus: status={status} retry_count={retry_count} "
        f"error_log={error_log!r}"
    )
    if status != "SIMULATED" or retry_count != 0 or error_log is not None:
        raise RuntimeError("worker heeft het test-event niet succesvol afgehandeld")

    print("[PASS] Daemon claimde, routeerde en simuleerde het event automatisch.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
