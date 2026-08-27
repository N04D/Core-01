#!/usr/bin/env python3
"""Main worker daemon for processing queued events through registered plugins."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.event_protocol import PluginResult, parse_result

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
    claim_token: str


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
    parser.add_argument("--lease-seconds", type=float, default=90.0)
    parser.add_argument("--heartbeat-interval", type=float, default=20.0)
    parser.add_argument("--reap-interval", type=float, default=30.0)
    return parser.parse_args()


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    connection = sqlite3.connect(database_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def utc_sql_after(seconds: float) -> str:
    """Return a canonical UTC timestamp compatible with SQLite CURRENT_TIMESTAMP."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def claim_event(
    connection: sqlite3.Connection, claimed_by: str, lease_seconds: float
) -> Event | None:
    """Atomically claim the oldest pending event."""
    try:
        connection.execute("BEGIN IMMEDIATE")
        token = uuid4().hex
        row = connection.execute(
            """
            UPDATE events_queue
               SET status = 'PROCESSING',
                   claimed_by = ?, lease_until = ?, claim_token = ?,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = (
                 SELECT id
                   FROM events_queue
                  WHERE status = 'PENDING'
                  ORDER BY created_at ASC, id ASC
                  LIMIT 1
             )
            RETURNING id, event_type, payload, retry_count, claim_token
            """,
            (claimed_by, utc_sql_after(lease_seconds), token),
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
        claim_token=row["claim_token"],
    )


def renew_lease(database_path: Path, event: Event, lease_seconds: float) -> bool:
    """Renew a lease only while the caller still owns the exact claim token."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE events_queue
               SET lease_until=?, updated_at=CURRENT_TIMESTAMP
             WHERE id=? AND status='PROCESSING' AND claim_token=?
            """,
            (utc_sql_after(lease_seconds), event.id, event.claim_token),
        )
        connection.commit()
        return cursor.rowcount == 1


def reap_expired_leases(connection: sqlite3.Connection) -> int:
    """Safely release expired claims so a healthy worker can reclaim them."""
    with connection:
        cursor = connection.execute(
            """
            UPDATE events_queue
               SET status='PENDING', claimed_by=NULL, lease_until=NULL,
                   claim_token=NULL,
                   error_log=CASE
                       WHEN error_log IS NULL OR error_log='' THEN 'Expired worker lease recovered'
                       ELSE substr(error_log || '; Expired worker lease recovered', 1, 8000)
                   END,
                   updated_at=CURRENT_TIMESTAMP
             WHERE status='PROCESSING' AND lease_until <= CURRENT_TIMESTAMP
            """
        )
    return cursor.rowcount


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


def execute_plugin(
    executable: Path,
    event: Event,
    database_path: Path,
    lease_seconds: float,
    heartbeat_interval: float,
) -> PluginResult:
    """Execute a plugin and maintain its lease until a result envelope arrives."""
    timeout = float(os.getenv("PLUGIN_TIMEOUT_SECONDS", "300"))
    process = subprocess.Popen(
        [
            str(executable),
            "--event_id",
            str(event.id),
            "--db",
            str(database_path),
        ],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=os.environ.copy(),
        cwd=executable.parent,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(str(executable), timeout, stdout, stderr)
        try:
            stdout, stderr = process.communicate(timeout=min(heartbeat_interval, remaining))
            break
        except subprocess.TimeoutExpired:
            if not renew_lease(database_path, event, lease_seconds):
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
                raise RuntimeError("event lease ownership was lost during plugin execution")
    try:
        plugin_result = parse_result(stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        details = stderr.strip() or stdout.strip()
        raise RuntimeError(f"Invalid plugin result envelope: {exc}; output={details[:4000]}") from exc
    if process.returncode != 0 and plugin_result.outcome not in {"FAILED", "UNKNOWN"}:
        details = stderr.strip() or stdout.strip()
        raise RuntimeError(f"Plugin exited with status {process.returncode}: {details[:4000]}")
    return plugin_result


def finalize_event(connection: sqlite3.Connection, event: Event, result: PluginResult) -> None:
    """Apply a plugin result while enforcing claim-token ownership."""
    queue_status = result.outcome if result.outcome in {"COMPLETED", "SIMULATED", "BLOCKED_AUTH"} else "FAILED"
    with connection:
        row = connection.execute("SELECT payload FROM events_queue WHERE id=?", (event.id,)).fetchone()
        if row is None:
            raise LookupError(f"event {event.id} disappeared")
        payload = json.loads(row["payload"])
        payload.update(result.payload_patch)
        payload["plugin_result"] = result.result
        cursor = connection.execute(
            """
            UPDATE events_queue
               SET status=?, payload=?, error_log=?, claimed_by=NULL,
                   lease_until=NULL, claim_token=NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id=? AND status='PROCESSING' AND claim_token=?
            """,
            (queue_status, json.dumps(payload, ensure_ascii=False), result.error, event.id, event.claim_token),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"claim ownership lost for event {event.id}")


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
                       claimed_by=NULL, lease_until=NULL, claim_token=NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ? AND status='PROCESSING' AND claim_token=?
                """,
                (retry_count, bounded_error, event.id, event.claim_token),
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
             WHERE id = ? AND status='PROCESSING' AND claim_token=?
            """,
            (retry_count, bounded_error, bounded_error, event.id, event.claim_token),
        )
        connection.execute(
            """DELETE FROM events_queue
                WHERE id = ? AND status='PROCESSING' AND claim_token=?""",
            (event.id, event.claim_token),
        )


def process_one(
    connection: sqlite3.Connection,
    database_path: Path,
    worker_id: str,
    lease_seconds: float,
    heartbeat_interval: float,
) -> bool:
    """Claim and process one event, returning whether an event was claimed."""
    event = claim_event(connection, worker_id, lease_seconds)
    if event is None:
        return False

    LOGGER.info("Claimed event id=%s type=%s", event.id, event.event_type)
    try:
        plugin = resolve_plugin(connection, event.event_type)
        result = execute_plugin(plugin, event, database_path, lease_seconds, heartbeat_interval)
    except (LookupError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        LOGGER.exception("Event id=%s failed", event.id)
        record_failure(connection, event, str(exc))
    else:
        if result.outcome == "FAILED" and result.retryable:
            record_failure(connection, event, result.error or "Plugin requested retry")
        else:
            finalize_event(connection, event, result)
        LOGGER.info("Finalized event id=%s outcome=%s", event.id, result.outcome)
    return True


def main() -> int:
    """Run the worker until interrupted or until one iteration completes."""
    args = parse_args()
    if args.poll_interval < 0 or args.lease_seconds <= 0 or args.heartbeat_interval <= 0 or args.reap_interval <= 0:
        raise ValueError("poll and lease intervals must be positive (poll may be zero)")
    if args.heartbeat_interval >= args.lease_seconds:
        raise ValueError("--heartbeat-interval must be shorter than --lease-seconds")

    load_dotenv(args.env_file, override=False)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database_path = args.database.expanduser().resolve()
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    last_reap = 0.0
    try:
        with connect(database_path) as connection:
            while True:
                if time.monotonic() - last_reap >= args.reap_interval:
                    reaped = reap_expired_leases(connection)
                    if reaped:
                        LOGGER.warning("Recovered %s expired event lease(s)", reaped)
                    last_reap = time.monotonic()
                processed = process_one(
                    connection, database_path, worker_id,
                    args.lease_seconds, args.heartbeat_interval,
                )
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
