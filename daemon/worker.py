#!/usr/bin/env python3
"""Main worker daemon for processing queued events through registered plugins."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - production bootstrap installs python3-dotenv
    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        """Continue without .env loading when the optional dependency is absent."""
        return False


LOGGER: Final = logging.getLogger("event_worker")
MAX_RETRIES: Final = 3


@dataclass(frozen=True)
class Event:
    """A claimed event from the queue."""

    id: int
    event_type: str
    payload: str
    retry_count: int


def parse_args() -> argparse.Namespace:
    """Parse worker configuration from command-line arguments."""
    parser = argparse.ArgumentParser(description="Run the event queue worker daemon.")
    parser.add_argument(
        "--database",
        "-d",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "db" / "events.db",
        help="Path to the SQLite database.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / ".env",
        help="Path to the dotenv file.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Seconds to wait when the queue is empty.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one event and exit.",
    )
    return parser.parse_args()


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    connection = sqlite3.connect(database_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def claim_event(connection: sqlite3.Connection) -> Event | None:
    """Atomically claim the oldest pending event."""
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            UPDATE events_queue
               SET status = 'PROCESSING',
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = (
                 SELECT id
                   FROM events_queue
                  WHERE status = 'PENDING'
                  ORDER BY created_at ASC, id ASC
                  LIMIT 1
             )
            RETURNING id, event_type, payload, retry_count
            """
        ).fetchone()
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    if row is None:
        return None
    return Event(
        id=row["id"],
        event_type=row["event_type"],
        payload=row["payload"],
        retry_count=row["retry_count"],
    )


def resolve_plugin(connection: sqlite3.Connection, event_type: str) -> Path:
    """Resolve an active plugin executable for an event type."""
    row = connection.execute(
        """
        SELECT pr.executable_path
          FROM event_routes AS er
          JOIN plugin_registry AS pr
            ON pr.plugin_name = er.target_plugin_name
         WHERE er.event_type = ?
           AND pr.is_active = 1
        """,
        (event_type,),
    ).fetchone()

    if row is None:
        raise LookupError(f"No active plugin route for event type {event_type!r}")

    executable = Path(row["executable_path"]).expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"Plugin executable does not exist: {executable}")
    if not os.access(executable, os.X_OK):
        raise PermissionError(f"Plugin is not executable: {executable}")
    return executable


def execute_plugin(executable: Path, event_id: int, database_path: Path) -> None:
    """Execute a plugin without a shell using the shared event interface."""
    timeout = float(os.getenv("PLUGIN_TIMEOUT_SECONDS", "300"))
    result = subprocess.run(
        [
            str(executable),
            "--event_id",
            str(event_id),
            "--db",
            str(database_path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=os.environ.copy(),
        cwd=executable.parent,
        start_new_session=True,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Plugin exited with status {result.returncode}: {details[:4000]}"
        )


def mark_completed(connection: sqlite3.Connection, event_id: int) -> None:
    """Mark an event as completed."""
    with connection:
        connection.execute(
            """
            UPDATE events_queue
               SET status = 'COMPLETED', error_log = NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = ? AND status IN ('PROCESSING', 'COMPLETED')
            """,
            (event_id,),
        )


def record_failure(
    connection: sqlite3.Connection, event: Event, error_message: str
) -> None:
    """Requeue a failed event or atomically move it to the dead-letter queue."""
    retry_count = event.retry_count + 1
    bounded_error = error_message[:8000]

    with connection:
        if retry_count < MAX_RETRIES:
            connection.execute(
                """
                UPDATE events_queue
                   SET status = 'PENDING', retry_count = ?, error_log = ?,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ? AND status IN ('PROCESSING', 'FAILED')
                """,
                (retry_count, bounded_error, event.id),
            )
            return

        connection.execute(
            """
            INSERT INTO dead_letter_queue (
                id, event_type, payload, status, retry_count, error_log,
                created_at, updated_at, reason_for_death
            )
            SELECT id, event_type, payload, 'FAILED', ?, ?, created_at,
                   CURRENT_TIMESTAMP, ?
              FROM events_queue
             WHERE id = ? AND status IN ('PROCESSING', 'FAILED')
            """,
            (retry_count, bounded_error, bounded_error, event.id),
        )
        connection.execute(
            """DELETE FROM events_queue
                WHERE id = ? AND status IN ('PROCESSING', 'FAILED')""",
            (event.id,),
        )


def process_one(connection: sqlite3.Connection, database_path: Path) -> bool:
    """Claim and process one event, returning whether an event was claimed."""
    event = claim_event(connection)
    if event is None:
        return False

    LOGGER.info("Claimed event id=%s type=%s", event.id, event.event_type)
    try:
        plugin = resolve_plugin(connection, event.event_type)
        execute_plugin(plugin, event.id, database_path)
    except (LookupError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        LOGGER.exception("Event id=%s failed", event.id)
        record_failure(connection, event, str(exc))
    else:
        mark_completed(connection, event.id)
        LOGGER.info("Completed event id=%s", event.id)
    return True


def main() -> int:
    """Run the worker until interrupted or until one iteration completes."""
    args = parse_args()
    if args.poll_interval < 0:
        raise ValueError("--poll-interval must be non-negative")

    load_dotenv(args.env_file, override=False)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database_path = args.database.expanduser().resolve()
    try:
        with connect(database_path) as connection:
            while True:
                processed = process_one(connection, database_path)
                if args.once:
                    return 0
                if not processed:
                    time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        LOGGER.info("Worker stopped")
        return 0
    except (OSError, sqlite3.Error, ValueError):
        LOGGER.exception("Worker terminated due to a fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
