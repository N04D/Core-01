#!/usr/bin/env python3
"""End-to-end test from an Obsidian Markdown file to a completed queue event."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from uuid import uuid4


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from core.database import connect_database
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
WATCHDOG: Final = PROJECT_ROOT / "daemon" / "folder_watchdog.py"
WORKER: Final = PROJECT_ROOT / "daemon" / "worker.py"
MOCK_PLUGIN: Final = PROJECT_ROOT / "plugins" / "channels" / "pub_mock.py"
OUTGOING: Final = PROJECT_ROOT / "vault" / "uitgaand"
ARCHIVE: Final = PROJECT_ROOT / "vault" / "gepubliceerd"
PLUGIN_NAME: Final = "Mock Publisher (Test Zandbak)"
LOGGER: Final = logging.getLogger("test_live_obsidian_file")


def parse_args() -> argparse.Namespace:
    """Parse test configuration."""
    parser = argparse.ArgumentParser(
        description="Test the real Obsidian file-to-event pipeline."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--publish-channel",
        choices=("PUBLISH_MOCK", "PUBLISH_LINKEDIN"),
        default="PUBLISH_MOCK",
    )
    return parser.parse_args()


def run(command: list[str], environment: dict[str, str] | None = None) -> None:
    """Run a component and mirror its exact logs."""
    LOGGER.info("COMMAND: %s", " ".join(command))
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
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


def ensure_mock_route(database: Path) -> None:
    """Register and route the safe channel used by the default test."""
    run([sys.executable, str(MOCK_PLUGIN), "--register", "--db", str(database)])
    with connect_database(database) as connection:
        connection.execute(
            """
            INSERT INTO event_routes (event_type, target_plugin_name)
            VALUES ('PUBLISH_MOCK', ?)
            ON CONFLICT(event_type) DO UPDATE SET
                target_plugin_name = excluded.target_plugin_name
            """,
            (PLUGIN_NAME,),
        )
        connection.commit()


def create_obsidian_file(publish_channel: str) -> tuple[Path, str]:
    """Create a unique real Markdown document in the outgoing vault."""
    OUTGOING.mkdir(parents=True, exist_ok=True)
    test_token = uuid4().hex
    topic = f"Soevereine Automatisering [{test_token}]"
    source = OUTGOING / f"obsidian_live_test_{test_token}.md"
    source.write_text(
        "---\n"
        f"topic: {json.dumps(topic, ensure_ascii=False)}\n"
        f"publish_channel: {json.dumps(publish_channel)}\n"
        "platform: obsidian-test\n"
        "---\n\n"
        "Dit is een echte file-based end-to-end test vanuit de Obsidian vault.\n",
        encoding="utf-8",
    )
    LOGGER.info("Obsidian test file created: %s", source)
    return source, topic


def find_event(database: Path, topic: str) -> tuple[int, str, dict[str, object]]:
    """Find the event emitted by the watchdog for this unique test."""
    with connect_database(database) as connection:
        row = connection.execute(
            """
            SELECT id, status, payload
              FROM events_queue
             WHERE json_extract(payload, '$.topic') = ?
             ORDER BY id DESC LIMIT 1
            """,
            (topic,),
        ).fetchone()
    if row is None:
        raise RuntimeError("watchdog did not create a matching queue event")
    return int(row[0]), str(row[1]), json.loads(row[2])


def process_until_terminal(database: Path, event_id: int) -> str:
    """Run single worker polls until the selected event reaches a terminal state."""
    for _ in range(20):
        with connect_database(database) as connection:
            row = connection.execute(
                "SELECT status, error_log FROM events_queue WHERE id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"event {event_id} disappeared")
        if row[0] == "COMPLETED":
            return str(row[0])
        if row[0] == "FAILED":
            raise RuntimeError(f"event {event_id} failed: {row[1]}")
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
    """Execute and verify the complete file-based pipeline."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    database = args.db.expanduser().resolve()
    if not database.is_file():
        LOGGER.error("Database not found: %s", database)
        return 2

    source: Path | None = None
    try:
        if args.publish_channel == "PUBLISH_MOCK":
            ensure_mock_route(database)
        source, topic = create_obsidian_file(args.publish_channel)
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
        event_id, initial_status, payload = find_event(database, topic)
        archived = Path(str(payload["draft_file"]))
        LOGGER.info(
            "Watchdog event detected: id=%s status=%s archive=%s",
            event_id,
            initial_status,
            archived,
        )
        status = process_until_terminal(database, event_id)

        if source.exists():
            raise RuntimeError(f"original file still exists: {source}")
        if not archived.is_file() or archived.parent.resolve() != ARCHIVE.resolve():
            raise RuntimeError(f"archived file is missing or misplaced: {archived}")

        LOGGER.info("Verified queue status=%s", status)
        LOGGER.info("Verified original removed: %s", source)
        LOGGER.info("Verified archive: %s", archived)
        print(f"[PASS] Live Obsidian-file test voltooid. Archief: {archived}")
        return 0
    except Exception:
        LOGGER.exception("Live Obsidian-file test FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
