#!/usr/bin/env python3
"""Dispatch due scheduled events atomically onto the primary event queue."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Final


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
LOGGER: Final = logging.getLogger("event_scheduler")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch due scheduled events.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval <= 0 or not 1 <= args.batch_size <= 1000:
        parser.error("interval must be positive and batch-size between 1 and 1000")
    return args


def connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def dispatch_due(database: Path, batch_size: int) -> list[tuple[int, int, str]]:
    """Claim due schedules and enqueue them in one immediate transaction."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dispatched: list[tuple[int, int, str]] = []
    with connect(database) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id,event_type,payload
                  FROM scheduled_events
                 WHERE status='PENDING' AND scheduled_time<=?
                 ORDER BY scheduled_time,id
                 LIMIT ?
                """,
                (now, batch_size),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    "INSERT INTO events_queue(event_type,payload) VALUES (?,?)",
                    (row["event_type"], row["payload"]),
                )
                connection.execute(
                    "UPDATE scheduled_events SET status='DISPATCHED' WHERE id=? AND status='PENDING'",
                    (row["id"],),
                )
                dispatched.append((int(row["id"]), int(cursor.lastrowid), row["event_type"]))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return dispatched


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    database = args.db.expanduser().resolve()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            rows = dispatch_due(database, args.batch_size)
            for schedule_id, event_id, event_type in rows:
                LOGGER.info(
                    "DISPATCHED schedule_id=%s event_id=%s type=%s",
                    schedule_id,
                    event_id,
                    event_type,
                )
            if args.once:
                LOGGER.info("Scheduler scan complete; dispatched=%s", len(rows))
                return 0
            stop.wait(args.interval)
    except (OSError, sqlite3.Error):
        LOGGER.exception("Scheduler stopped due to a fatal error")
        return 1
    LOGGER.info("Scheduler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
