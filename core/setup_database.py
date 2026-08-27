#!/usr/bin/env python3
"""Initialize the SQLite foundation for the event-driven system."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database import connect_database


LOGGER: Final = logging.getLogger("setup_database")

SCHEMA: Final[dict[str, str]] = {
    "events_queue": """
        CREATE TABLE IF NOT EXISTS events_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload JSON NOT NULL CHECK (json_valid(payload)),
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN (
                    'PENDING', 'PROCESSING', 'COMPLETED', 'FAILED',
                    'BLOCKED_AUTH', 'SIMULATED'
                )),
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
            error_log TEXT,
            claimed_by TEXT,
            lease_until TEXT,
            claim_token TEXT,
            next_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                (status = 'PROCESSING' AND claimed_by IS NOT NULL
                    AND lease_until IS NOT NULL AND claim_token IS NOT NULL)
                OR
                (status != 'PROCESSING' AND claimed_by IS NULL
                    AND lease_until IS NULL AND claim_token IS NULL)
            )
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
    "publication_attempts": """
        CREATE TABLE IF NOT EXISTS publication_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            target TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 1 CHECK (attempt_number > 0),
            status TEXT NOT NULL CHECK (
                status IN ('PREPARED', 'SUBMITTED', 'CONFIRMED', 'UNKNOWN', 'FAILED')
            ),
            platform_id TEXT,
            platform_url TEXT,
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(event_id, channel)
        )
    """,
    "telegram_updates": """
        CREATE TABLE IF NOT EXISTS telegram_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            update_id INTEGER NOT NULL UNIQUE,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            concept_path TEXT,
            inbound_event_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, message_id)
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

INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_events_queue_ready "
    "ON events_queue(status, next_attempt_at, created_at, id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_queue_claim_token "
    "ON events_queue(claim_token) WHERE claim_token IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_events_queue_lease "
    "ON events_queue(status, lease_until) WHERE status='PROCESSING'",
    "CREATE INDEX IF NOT EXISTS idx_scheduled_events_due "
    "ON scheduled_events(status, scheduled_time, id)",
    "CREATE INDEX IF NOT EXISTS idx_publication_attempts_status "
    "ON publication_attempts(status, updated_at)",
)


def migrate_events_queue(connection: sqlite3.Connection) -> None:
    """Upgrade the queue CHECK constraint and lease columns without data loss."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events_queue'"
    ).fetchone()
    if row is None:
        return
    sql = str(row[0] or "")
    columns = {
        str(item[1]) for item in connection.execute("PRAGMA table_info(events_queue)")
    }
    if {"claimed_by", "lease_until", "claim_token", "next_attempt_at"}.issubset(columns) and all(
        status in sql for status in ("BLOCKED_AUTH", "SIMULATED")
    ):
        return

    LOGGER.info("Migrating 'events_queue' to lease-aware event semantics.")
    connection.execute("ALTER TABLE events_queue RENAME TO events_queue_legacy")
    connection.execute(SCHEMA["events_queue"])
    connection.execute(
        """
        INSERT INTO events_queue (
            id, event_type, payload, status, retry_count, error_log,
            next_attempt_at, created_at, updated_at
        )
        SELECT id, event_type, payload,
               CASE WHEN status='PROCESSING' THEN 'PENDING' ELSE status END,
               retry_count,
               CASE WHEN status='PROCESSING'
                    THEN COALESCE(error_log || '; ', '') ||
                         'Recovered during lease migration'
                    ELSE error_log END,
               created_at, created_at, updated_at
          FROM events_queue_legacy
        """
    )
    connection.execute("DROP TABLE events_queue_legacy")


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

    with connect_database(resolved_path) as connection:

        migrate_events_queue(connection)
        for table_name, statement in SCHEMA.items():
            connection.execute(statement)
            LOGGER.info("Table '%s' checked or created successfully.", table_name)

        for statement in INDEXES:
            connection.execute(statement)
        LOGGER.info("Database indexes checked or created successfully.")

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
