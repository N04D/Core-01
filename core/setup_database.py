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
    "scheduled_events": """
        CREATE TABLE IF NOT EXISTS scheduled_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload JSON NOT NULL CHECK (json_valid(payload)),
            scheduled_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'DISPATCHED', 'CANCELLED', 'FAILED'))
        )
    """,
    "media_sources": """
        CREATE TABLE IF NOT EXISTS media_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            provider TEXT NOT NULL,
            root_path TEXT,
            config JSON NOT NULL DEFAULT '{}' CHECK (json_valid(config)),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            last_indexed_at TEXT
        )
    """,
    "media_assets": """
        CREATE TABLE IF NOT EXISTS media_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES media_sources(id) ON DELETE CASCADE,
            external_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            thumbnail_path TEXT,
            media_type TEXT NOT NULL CHECK (media_type IN ('image', 'video')),
            mime_type TEXT,
            file_size INTEGER NOT NULL DEFAULT 0,
            modified_at TEXT,
            prompt TEXT,
            metadata JSON NOT NULL DEFAULT '{}' CHECK (json_valid(metadata)),
            is_available INTEGER NOT NULL DEFAULT 1 CHECK (is_available IN (0, 1)),
            indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_id, external_id)
        )
    """,
    "media_links": """
        CREATE TABLE IF NOT EXISTS media_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            target_type TEXT NOT NULL CHECK (target_type IN ('DRAFT', 'SCHEDULE')),
            target_ref TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(asset_id, target_type, target_ref)
        )
    """,
    "rag_documents": """
        CREATE TABLE IF NOT EXISTS rag_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL UNIQUE,
            content_hash TEXT NOT NULL,
            modified_at TEXT NOT NULL,
            indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "rag_chunks": """
        CREATE TABLE IF NOT EXISTS rag_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            vector JSON NOT NULL CHECK (json_valid(vector)),
            token_count INTEGER NOT NULL,
            UNIQUE(document_id, chunk_index)
        )
    """,
    "session_health": """
        CREATE TABLE IF NOT EXISTS session_health (
            platform TEXT PRIMARY KEY,
            plugin_name TEXT NOT NULL,
            auth_file TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('CONNECTED', 'AUTH_REQUIRED', 'ERROR')),
            detail TEXT,
            checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "system_notifications": """
        CREATE TABLE IF NOT EXISTS system_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL')),
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            platform TEXT,
            is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "content_variants": """
        CREATE TABLE IF NOT EXISTS content_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_essay TEXT NOT NULL,
            channel TEXT NOT NULL,
            variant_path TEXT NOT NULL,
            generation_event_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_essay, channel, variant_path)
        )
    """,
    "evergreen_posts": """
        CREATE TABLE IF NOT EXISTS evergreen_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_key TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL,
            source_path TEXT,
            analytics_file TEXT NOT NULL,
            title TEXT,
            content TEXT,
            published_at TEXT NOT NULL,
            metrics JSON NOT NULL CHECK (json_valid(metrics)),
            engagement_score REAL NOT NULL DEFAULT 0,
            is_evergreen INTEGER NOT NULL DEFAULT 0 CHECK (is_evergreen IN (0, 1)),
            eligible_after TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "evergreen_proposals": """
        CREATE TABLE IF NOT EXISTS evergreen_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evergreen_post_id INTEGER NOT NULL REFERENCES evergreen_posts(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'PROPOSED'
                CHECK (status IN ('PROPOSED', 'ACCEPTED', 'DISMISSED', 'SCHEDULED')),
            proposed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            rewrite_payload JSON NOT NULL CHECK (json_valid(rewrite_payload)),
            UNIQUE(evergreen_post_id, status)
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
