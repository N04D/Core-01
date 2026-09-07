#!/usr/bin/env python3
"""Firecrawl I/O adapter for the event-driven system."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.event_protocol import emit_result
from core.database import connect_database
from core.paths import DATABASE_PATH

import requests
from dotenv import load_dotenv


PLUGIN_NAME: Final = "Firecrawl Web Scraper"
PLUGIN_TYPE: Final = "io"
PLUGIN_ICON: Final = "🕷️"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE: Final = DATABASE_PATH
RESEARCH_DIRECTORY: Final = PROJECT_ROOT / "vault" / "research"
LOGGER: Final = logging.getLogger("crawl_firecrawl")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments and enforce an operating mode."""
    parser = argparse.ArgumentParser(
        description="Register or execute the Firecrawl web-scraper plugin."
    )
    parser.add_argument(
        "--register",
        action="store_true",
        help="Register this plugin in the SQLite plugin registry.",
    )
    parser.add_argument(
        "--event_id",
        type=int,
        help="ID of the queue event to process.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DATABASE,
        help="Path to the SQLite database.",
    )
    args = parser.parse_args()

    if not args.register and args.event_id is None:
        parser.error("either --register or --event_id is required")
    if args.event_id is not None and args.event_id < 1:
        parser.error("--event_id must be a positive integer")
    return args


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    return connect_database(database_path)


def register_plugin(connection: sqlite3.Connection) -> None:
    """Idempotently register this executable in the plugin registry."""
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
            (PLUGIN_NAME, PLUGIN_TYPE, str(Path(__file__).resolve()), PLUGIN_ICON),
        )
    LOGGER.info("Registered plugin %s", PLUGIN_NAME)


def load_event(connection: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    """Load and validate a JSON event payload."""
    row = connection.execute(
        "SELECT payload FROM events_queue WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"event {event_id} does not exist")

    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("event payload must contain a non-empty 'url'")

    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError("payload URL must be an absolute HTTP(S) URL")
    payload["url"] = url.strip()
    return payload


def scrape_markdown(url: str) -> str:
    """Request Markdown content from the configured Firecrawl service."""
    api_url = os.getenv("FIRECRAWL_API_URL", "http://localhost:3002").rstrip("/")
    api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    timeout = float(os.getenv("FIRECRAWL_TIMEOUT_SECONDS", "120"))
    if timeout <= 0:
        raise ValueError("FIRECRAWL_TIMEOUT_SECONDS must be positive")

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = requests.post(
        f"{api_url}/v1/scrape",
        headers=headers,
        json={"url": url, "formats": ["markdown"]},
        timeout=timeout,
    )
    response.raise_for_status()
    response_body = response.json()
    if not isinstance(response_body, dict):
        raise ValueError("Firecrawl returned an invalid JSON response")

    data = response_body.get("data", response_body)
    markdown = data.get("markdown") if isinstance(data, dict) else None
    if not isinstance(markdown, str) or not markdown.strip():
        message = response_body.get("error") or "response contains no Markdown"
        raise ValueError(f"Firecrawl scrape failed: {message}")
    return markdown


def create_output_path(source_url: str) -> Path:
    """Construct a collision-resistant, filesystem-safe research filename."""
    hostname = urlparse(source_url).hostname or "onbekend"
    safe_domain = re.sub(r"[^a-zA-Z0-9.-]+", "_", hostname).strip("._")
    safe_domain = safe_domain[:80] or "onbekend"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return RESEARCH_DIRECTORY / (
        f"bron_{safe_domain}_{timestamp}_{uuid4().hex[:8]}.md"
    )


def save_markdown(source_url: str, markdown: str) -> Path:
    """Atomically persist scraped Markdown in the research vault."""
    RESEARCH_DIRECTORY.mkdir(parents=True, exist_ok=True)
    output_path = create_output_path(source_url)
    header = (
        "---\n"
        f"source: {json.dumps(source_url, ensure_ascii=False)}\n"
        f"scraped_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        "adapter: firecrawl\n"
        "---\n\n"
    )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=RESEARCH_DIRECTORY,
            prefix=".firecrawl_",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(header)
            temporary_file.write(markdown.rstrip())
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(output_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return output_path


def process_event(connection: sqlite3.Connection, event_id: int) -> Path:
    """Scrape one event URL and return the resulting vault path."""
    payload = load_event(connection, event_id)
    markdown = scrape_markdown(payload["url"])
    output_path = save_markdown(payload["url"], markdown)
    LOGGER.info("Event %s completed; output=%s", event_id, output_path)
    return output_path


def main() -> int:
    """Register the plugin and/or process one event."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    database_path = args.db.expanduser().resolve()

    try:
        with connect(database_path) as connection:
            if args.register:
                register_plugin(connection)
            if args.event_id is not None:
                output_path = process_event(connection, args.event_id)
                emit_result("COMPLETED", result={"filepath": str(output_path)}, payload_patch={"filepath": str(output_path)})
    except Exception as exc:
        if args.event_id is not None:
            emit_result("FAILED", error=f"{type(exc).__name__}: {exc}", retryable=True)
        LOGGER.exception("Firecrawl plugin failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
