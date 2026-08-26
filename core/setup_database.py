#!/usr/bin/env python3
"""Initialize the SQLite foundation for the event-driven system."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Final


LOGGER: Final = logging.getLogger("setup_database")

SCHEMA: Final[dict[str, str]] = {
    "events_queue": """
        CREATE TABLE IF NOT EXISTS events_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload JSON NOT NULL CHECK (json_valid(payload)),
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')),
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
            error_log TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "dead_letter_queue": """
        CREATE TABLE IF NOT EXISTS dead_letter_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload JSON NOT NULL CHECK (json_valid(payload)),
            status TEXT NOT NULL DEFAULT 'FAILED'
                CHECK (status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')),
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
            error_log TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reason_for_death TEXT NOT NULL
        )
    """,
    "event_routes": """
        CREATE TABLE IF NOT EXISTS event_routes (
            event_type TEXT PRIMARY KEY,
            target_plugin_name TEXT NOT NULL
        )
    """,
    "plugin_registry": """
        CREATE TABLE IF NOT EXISTS plugin_registry (
            plugin_name TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            executable_path TEXT NOT NULL,
            icon TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
        )
    """,
}


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Initialize the event-driven system's SQLite database."
    )
    parser.add_argument(
        "--database",
        "-d",
        type=Path,
        default=Path("../db/events.db"),
        help="SQLite database path (default: ../db/events.db).",
    )
    return parser.parse_args()


def initialize_database(database_path: Path) -> None:
    """Create the database and required tables in a single transaction."""
    resolved_path = database_path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(resolved_path, timeout=30.0) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")

        for table_name, statement in SCHEMA.items():
            connection.execute(statement)
            LOGGER.info("Table '%s' checked or created successfully.", table_name)

        connection.commit()


def main() -> int:
    """Run database initialization."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()

    try:
        initialize_database(args.database)
    except (OSError, sqlite3.Error):
        LOGGER.exception("Database initialization failed.")
        return 1

    LOGGER.info("Database initialization completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
