#!/usr/bin/env python3
"""End-to-end Obsidian file test for the safe Substack channel fallback."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Final
from uuid import uuid4


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
PLUGIN: Final = PROJECT_ROOT / "plugins" / "channels" / "pub_substack.py"
WATCHDOG: Final = PROJECT_ROOT / "daemon" / "folder_watchdog.py"
WORKER: Final = PROJECT_ROOT / "daemon" / "worker.py"
OUTGOING: Final = PROJECT_ROOT / "vault" / "uitgaand"
ARCHIVE: Final = PROJECT_ROOT / "vault" / "gepubliceerd"
PLUGIN_NAME: Final = "Substack Publisher"
EVENT_TYPE: Final = "PUBLISH_SUBSTACK"


def parse_args() -> argparse.Namespace:
    """Parse database configuration."""
    parser = argparse.ArgumentParser(
        description="Test Substack syndication from a real Obsidian Markdown file."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    return parser.parse_args()


def run(command: list[str]) -> None:
    """Run one pipeline component and mirror its logs."""
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
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"command exited with {result.returncode}: {' '.join(command)}"
        )


def ensure_registration_and_route(database: Path) -> None:
    """Register the plugin and idempotently configure its event route."""
    run([sys.executable, str(PLUGIN), "--register", "--db", str(database)])
    with sqlite3.connect(database, timeout=30.0) as connection:
        registered = connection.execute(
            """
            SELECT executable_path, is_active
              FROM plugin_registry WHERE plugin_name=?
            """,
            (PLUGIN_NAME,),
        ).fetchone()
        if registered is None or registered[1] != 1:
            raise RuntimeError("Substack Publisher is not actively registered")
        if Path(registered[0]).resolve() != PLUGIN.resolve():
            raise RuntimeError("Substack Publisher executable path is incorrect")
        connection.execute(
            """
            INSERT INTO event_routes (event_type, target_plugin_name)
            VALUES (?, ?)
            ON CONFLICT(event_type) DO UPDATE SET
                target_plugin_name=excluded.target_plugin_name
            """,
            (EVENT_TYPE, PLUGIN_NAME),
        )
        connection.commit()
    print(f"[OK] Plugin actief en route ingesteld: {EVENT_TYPE} → {PLUGIN_NAME}")


def create_document() -> tuple[Path, str]:
    """Create a unique outgoing Markdown document with YAML frontmatter."""
    OUTGOING.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    topic = f"Substack Obsidian integratietest [{token}]"
    path = OUTGOING / f"substack_obsidian_test_{token}.md"
    path.write_text(
        "---\n"
        f"topic: {json.dumps(topic, ensure_ascii=False)}\n"
        "publish_channel: PUBLISH_SUBSTACK\n"
        "platform: substack\n"
        "---\n\n"
        "# Soevereine publicatie\n\n"
        "Dit document doorloopt de echte Obsidian-bestandsketen en eindigt "
        "veilig in de Substack mock-fallback.\n",
        encoding="utf-8",
    )
    print(f"[OK] Obsidian testbestand aangemaakt: {path}")
    return path, topic


def find_event(database: Path, topic: str) -> tuple[int, str, dict[str, object]]:
    """Find the uniquely tagged event emitted by the watchdog."""
    with sqlite3.connect(database, timeout=30.0) as connection:
        row = connection.execute(
            """
            SELECT id, status, payload
              FROM events_queue
             WHERE event_type=? AND json_extract(payload, '$.topic')=?
             ORDER BY id DESC LIMIT 1
            """,
            (EVENT_TYPE, topic),
        ).fetchone()
    if row is None:
        raise RuntimeError("watchdog did not create the Substack event")
    return int(row[0]), str(row[1]), json.loads(row[2])


def process_target(database: Path, event_id: int) -> tuple[str, dict[str, object]]:
    """Run bounded single worker cycles until this event is terminal."""
    for _ in range(20):
        with sqlite3.connect(database, timeout=30.0) as connection:
            row = connection.execute(
                "SELECT status, payload, error_log FROM events_queue WHERE id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"event {event_id} disappeared")
        if row[0] == "COMPLETED":
            return str(row[0]), json.loads(row[1])
        if row[0] == "FAILED":
            raise RuntimeError(f"event {event_id} failed: {row[2]}")
        run(
            [
                sys.executable,
                str(WORKER),
                "--database",
                str(database),
                "--once",
            ]
        )
        time.sleep(0.05)
    raise TimeoutError(f"event {event_id} did not complete")


def main() -> int:
    """Execute and verify the complete Substack file workflow."""
    database = parse_args().db.expanduser().resolve()
    if not database.is_file():
        print(f"[FAIL] Database ontbreekt: {database}", file=sys.stderr)
        return 2
    source: Path | None = None
    try:
        ensure_registration_and_route(database)
        source, topic = create_document()
        run(
            [
                sys.executable,
                str(WATCHDOG),
                "--db",
                str(database),
                "--watch-directory",
                str(OUTGOING),
                "--archive-directory",
                str(ARCHIVE),
                "--once",
            ]
        )
        event_id, initial_status, initial_payload = find_event(database, topic)
        archived = Path(str(initial_payload["draft_file"]))
        print(
            f"[OK] Watchdog-event: id={event_id} status={initial_status} "
            f"archive={archived}"
        )
        status, final_payload = process_target(database, event_id)
        result = final_payload.get("substack")
        if not isinstance(result, dict):
            raise AssertionError("completed event has no Substack result")
        if not (
            result.get("mock") is True
            and result.get("substack_contacted") is False
            and result.get("published") is False
        ):
            raise AssertionError(f"unsafe or invalid Substack mock result: {result}")
        if source.exists():
            raise AssertionError(f"original still exists: {source}")
        if not archived.is_file() or archived.parent.resolve() != ARCHIVE.resolve():
            raise AssertionError(f"archive missing or misplaced: {archived}")

        print(f"[OK] Eventstatus: {status}")
        print("[OK] Veilige Substack mock bevestigd; netwerk/publicatie niet uitgevoerd")
        print(f"[OK] Origineel verwijderd: {source}")
        print(f"[OK] Gearchiveerd bestand: {archived}")
        print(f"[PASS] Substack Obsidian-file test voltooid. Archief: {archived}")
        return 0
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
