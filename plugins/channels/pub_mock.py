#!/usr/bin/env python3
"""Sandbox channel plugin that records publications in the local vault."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.event_protocol import emit_result
from core.database import connect_database
from core.paths import DATABASE_PATH


PLUGIN_NAME: Final = "Mock Publisher (Test Zandbak)"
PLUGIN_TYPE: Final = "channel"
PLUGIN_ICON: Final = "🧪"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE: Final = DATABASE_PATH
DEFAULT_LOG_FILE: Final = PROJECT_ROOT / "vault" / "logs" / "mock_publish_log.md"
LOGGER: Final = logging.getLogger("pub_mock")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Register or run the mock publisher channel plugin."
    )
    parser.add_argument(
        "--register",
        action="store_true",
        help="Register this plugin in the plugin registry.",
    )
    parser.add_argument(
        "--event_id",
        type=int,
        help="ID of the event to publish.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DATABASE,
        help="Path to the SQLite event database.",
    )
    args = parser.parse_args()

    if not args.register and args.event_id is None:
        parser.error("either --register or --event_id is required")
    if args.event_id is not None and args.event_id < 1:
        parser.error("--event_id must be a positive integer")
    return args


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a hardened SQLite connection."""
    return connect_database(database_path)


def register_plugin(connection: sqlite3.Connection) -> None:
    """Register or refresh this plugin without duplicating its record."""
    executable = Path(__file__).resolve()
    with connection:
        connection.execute(
            """
            INSERT INTO plugin_registry (
                plugin_name, type, executable_path, icon, is_active
            ) VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(plugin_name) DO UPDATE SET
                type = excluded.type,
                executable_path = excluded.executable_path,
                icon = excluded.icon,
                is_active = excluded.is_active
            """,
            (PLUGIN_NAME, PLUGIN_TYPE, str(executable), PLUGIN_ICON),
        )
    LOGGER.info("Plugin registered as %s", PLUGIN_NAME)


def load_event(connection: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    """Load an event payload by ID."""
    row = connection.execute(
        "SELECT id, event_type, payload FROM events_queue WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"event {event_id} does not exist")
    return row


def parse_payload(raw_payload: str) -> dict[str, Any]:
    """Decode and validate the event payload."""
    payload = json.loads(raw_payload)
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    return payload


def extract_content(payload: dict[str, Any]) -> tuple[str, str]:
    """Extract publishable content directly or from a referenced draft file."""
    if "content" in payload:
        content = payload["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("payload field 'content' must be a non-empty string")
        return content, "inline content"

    if "draft_file" in payload:
        draft_value = payload["draft_file"]
        if not isinstance(draft_value, str) or not draft_value.strip():
            raise ValueError("payload field 'draft_file' must be a non-empty string")
        draft_path = Path(draft_value).expanduser()
        if not draft_path.is_absolute():
            draft_path = PROJECT_ROOT / draft_path
        draft_path = draft_path.resolve(strict=True)
        if not draft_path.is_file():
            raise ValueError(f"draft path is not a file: {draft_path}")
        return draft_path.read_text(encoding="utf-8"), str(draft_path)

    raise ValueError("payload must contain 'draft_file' or 'content'")


def append_publication(event_id: int, event_type: str, source: str, content: str) -> None:
    """Append a publication entry while holding an exclusive file lock."""
    DEFAULT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = (
        f"\n## Mock publication — event {event_id}\n\n"
        f"- Timestamp (UTC): {timestamp}\n"
        f"- Event type: `{event_type}`\n"
        f"- Source: `{source}`\n\n"
        f"{content.rstrip()}\n"
    )

    with DEFAULT_LOG_FILE.open("a", encoding="utf-8") as log_file:
        fcntl.flock(log_file.fileno(), fcntl.LOCK_EX)
        try:
            log_file.write(entry)
            log_file.flush()
        finally:
            fcntl.flock(log_file.fileno(), fcntl.LOCK_UN)


def process_event(connection: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    """Publish one event and return its artifact metadata to the worker."""
    event = load_event(connection, event_id)
    payload = parse_payload(event["payload"])
    content, source = extract_content(payload)
    append_publication(event["id"], event["event_type"], source, content)

    LOGGER.info("Event %s recorded in %s", event_id, DEFAULT_LOG_FILE)
    return {"log_file": str(DEFAULT_LOG_FILE), "source": source}


def main() -> int:
    """Run registration and/or process a selected event."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    database_path = args.db.expanduser().resolve()

    try:
        with connect(database_path) as connection:
            if args.register:
                register_plugin(connection)
            if args.event_id is not None:
                result = process_event(connection, args.event_id)
                emit_result("SIMULATED", result=result, payload_patch={"mock_publisher": result})
    except (OSError, sqlite3.Error, ValueError, LookupError, json.JSONDecodeError):
        if args.event_id is not None:
            emit_result("FAILED", error="Mock publisher failed", retryable=True)
        LOGGER.exception("Mock publisher failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
