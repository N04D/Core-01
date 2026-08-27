#!/usr/bin/env python3
"""Monitor the outgoing vault and convert Markdown files into queue events."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database import connect_database

import yaml

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    FileSystemEvent = Any  # type: ignore[misc,assignment]
    FileSystemEventHandler = object  # type: ignore[assignment]
    Observer = None  # type: ignore[assignment]
    WATCHDOG_AVAILABLE = False


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
DEFAULT_WATCH_DIRECTORY: Final = PROJECT_ROOT / "vault" / "uitgaand"
DEFAULT_ARCHIVE_DIRECTORY: Final = PROJECT_ROOT / "vault" / "gepubliceerd"
FRONTMATTER_PATTERN: Final = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*\r?\n?(?P<body>.*)\Z",
    re.DOTALL,
)
EVENT_TYPE_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
LOGGER: Final = logging.getLogger("folder_watchdog")


@dataclass(frozen=True)
class MarkdownDocument:
    """Parsed Markdown document and publication metadata."""

    frontmatter: dict[str, Any]
    body: str


def parse_args() -> argparse.Namespace:
    """Parse daemon configuration."""
    parser = argparse.ArgumentParser(
        description="Watch vault/uitgaand and enqueue Markdown publications."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--watch-directory",
        "--watch-dir",
        dest="watch_directory",
        type=Path,
        default=DEFAULT_WATCH_DIRECTORY,
    )
    parser.add_argument(
        "--archive-directory",
        type=Path,
        default=DEFAULT_ARCHIVE_DIRECTORY,
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process currently present Markdown files and exit.",
    )
    return parser.parse_args()


def parse_markdown(path: Path) -> MarkdownDocument:
    """Read a stable UTF-8 Markdown file and parse optional YAML frontmatter."""
    raw_text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_PATTERN.fullmatch(raw_text)
    if match is None:
        return MarkdownDocument(frontmatter={}, body=raw_text)
    try:
        frontmatter = yaml.safe_load(match.group("header")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter in {path.name}: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise ValueError(f"frontmatter in {path.name} must be a mapping")
    return MarkdownDocument(frontmatter=frontmatter, body=match.group("body"))


def unique_archive_path(archive_directory: Path, source: Path) -> Path:
    """Create a collision-resistant archive destination."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.stem).strip("._")
    return archive_directory / (
        f"{safe_stem or 'document'}_{timestamp}_{uuid4().hex[:8]}.md"
    )


def enqueue_document(
    database: Path,
    archived_path: Path,
    document: MarkdownDocument,
) -> int:
    """Insert a publication or playbook event and return its queue ID."""
    raw_event_type = document.frontmatter.get(
        "event_type",
        document.frontmatter.get("publish_channel", "PUBLISH_MOCK"),
    )
    if not isinstance(raw_event_type, str) or not EVENT_TYPE_PATTERN.fullmatch(
        raw_event_type
    ):
        raise ValueError("frontmatter event_type is invalid")

    payload: dict[str, Any] = {
        "draft_file": str(archived_path),
        "content": document.body.strip(),
        "source": "folder_watchdog",
    }
    for field in ("platform", "topic", "publish_channel"):
        value = document.frontmatter.get(field)
        if value is not None:
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"frontmatter field {field!r} must be scalar")
            payload[field] = value

    with connect_database(database) as connection:
        cursor = connection.execute(
            "INSERT INTO events_queue (event_type, payload) VALUES (?, ?)",
            (raw_event_type, json.dumps(payload, ensure_ascii=False)),
        )
        event_id = int(cursor.lastrowid)
        connection.commit()
    return event_id


def process_file(
    source: Path,
    database: Path,
    watch_directory: Path,
    archive_directory: Path,
) -> bool:
    """Claim, parse, archive, and enqueue one Markdown file exactly once."""
    try:
        resolved_source = source.resolve(strict=True)
        if resolved_source.parent != watch_directory or resolved_source.suffix.lower() != ".md":
            return False

        document = parse_markdown(resolved_source)
        archive_directory.mkdir(parents=True, exist_ok=True)
        destination = unique_archive_path(archive_directory, resolved_source)
        shutil.move(str(resolved_source), destination)
        try:
            event_id = enqueue_document(database, destination, document)
        except Exception:
            shutil.move(str(destination), resolved_source)
            raise

        LOGGER.info(
            "Detected %s → queued event id=%s → archived as %s",
            resolved_source.name,
            event_id,
            destination,
        )
        return True
    except FileNotFoundError:
        return False
    except Exception:
        LOGGER.exception("Could not process Markdown file: %s", source)
        return False


def scan_once(
    database: Path, watch_directory: Path, archive_directory: Path
) -> int:
    """Process all Markdown files currently in the watched directory."""
    processed = 0
    for source in sorted(watch_directory.glob("*.md")):
        processed += process_file(
            source, database, watch_directory, archive_directory
        )
    return processed


class MarkdownEventHandler(FileSystemEventHandler):
    """Forward watchdog create/move events to the serialized work queue."""

    def __init__(self, paths: "queue.Queue[Path]") -> None:
        super().__init__()
        self.paths = paths

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.paths.put(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.paths.put(Path(event.dest_path))


def run_watchdog_backend(
    database: Path,
    watch_directory: Path,
    archive_directory: Path,
    stop_event: threading.Event,
) -> None:
    """Run native filesystem notifications with serialized processing."""
    paths: queue.Queue[Path] = queue.Queue()
    observer = Observer()
    observer.schedule(MarkdownEventHandler(paths), str(watch_directory), recursive=False)
    observer.start()
    LOGGER.info("Backend=watchdog directory=%s", watch_directory)
    try:
        scan_once(database, watch_directory, archive_directory)
        while not stop_event.is_set():
            try:
                path = paths.get(timeout=0.5)
            except queue.Empty:
                continue
            process_file(path, database, watch_directory, archive_directory)
    finally:
        observer.stop()
        observer.join(timeout=5.0)


def run_polling_backend(
    database: Path,
    watch_directory: Path,
    archive_directory: Path,
    interval: float,
    stop_event: threading.Event,
) -> None:
    """Use a dependency-free polling loop when watchdog is unavailable."""
    LOGGER.info("Backend=polling directory=%s interval=%ss", watch_directory, interval)
    while not stop_event.is_set():
        scan_once(database, watch_directory, archive_directory)
        stop_event.wait(interval)


def main() -> int:
    """Run one scan or continuously monitor the outgoing vault."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    if args.poll_interval <= 0:
        LOGGER.error("--poll-interval must be positive")
        return 2

    database = args.db.expanduser().resolve()
    watch_directory = args.watch_directory.expanduser().resolve()
    archive_directory = args.archive_directory.expanduser().resolve()
    watch_directory.mkdir(parents=True, exist_ok=True)
    archive_directory.mkdir(parents=True, exist_ok=True)

    if args.once:
        LOGGER.info("Backend=single-scan directory=%s", watch_directory)
        processed = scan_once(database, watch_directory, archive_directory)
        LOGGER.info("Single scan completed; processed=%s", processed)
        return 0

    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_args: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_args: stop_event.set())
    try:
        if WATCHDOG_AVAILABLE:
            run_watchdog_backend(
                database, watch_directory, archive_directory, stop_event
            )
        else:
            run_polling_backend(
                database,
                watch_directory,
                archive_directory,
                args.poll_interval,
                stop_event,
            )
    except (OSError, sqlite3.Error):
        LOGGER.exception("Folder watchdog stopped due to a fatal error")
        return 1
    LOGGER.info("Folder watchdog stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
