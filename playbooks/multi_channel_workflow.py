#!/usr/bin/env python3
"""Fan-out/fan-in master playbook for concurrent multi-channel syndication."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
SKILL_FILE: Final = PROJECT_ROOT / "vault" / "skills" / "multi_channel_essay.md"
CONCEPT_DIR: Final = PROJECT_ROOT / "vault" / "concepten"
ARCHIVE_DIR: Final = PROJECT_ROOT / "vault" / "gepubliceerd"
SUPPORTED_CHANNELS: Final = ("PUBLISH_LINKEDIN", "PUBLISH_SUBSTACK")
LOGGER: Final = logging.getLogger("multi_channel_workflow")


class WorkflowError(RuntimeError):
    """Raised when a generation or syndication event cannot complete."""


@dataclass
class ChannelRun:
    """Runtime state for one independently dispatched channel."""

    channel: str
    event_id: int
    archive_path: Path
    status: str = "PENDING"
    error: str | None = None


def parse_args() -> argparse.Namespace:
    """Parse workflow inputs and channel selection."""
    parser = argparse.ArgumentParser(
        description="Generate once and syndicate concurrently to multiple channels."
    )
    parser.add_argument("--topic", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--channels",
        nargs="+",
        choices=SUPPORTED_CHANNELS,
        default=list(SUPPORTED_CHANNELS),
    )
    parser.add_argument(
        "--skip-generation",
        action="store_true",
        help="Create a deterministic local essay instead of dispatching AI_GENERATION.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force safe non-publishing channel behavior for integration tests.",
    )
    args = parser.parse_args()
    args.channels = list(dict.fromkeys(args.channels))
    return args


def connect(database: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    connection = sqlite3.connect(database, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def validate_route(database: Path, event_type: str) -> None:
    """Require an active route with an executable target."""
    with connect(database) as connection:
        row = connection.execute(
            """
            SELECT pr.plugin_name, pr.executable_path
              FROM event_routes er
              JOIN plugin_registry pr ON pr.plugin_name=er.target_plugin_name
             WHERE er.event_type=? AND pr.is_active=1
            """,
            (event_type,),
        ).fetchone()
    if row is None:
        raise WorkflowError(f"no active route for {event_type}")
    executable = Path(row["executable_path"]).expanduser()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise WorkflowError(f"plugin executable unavailable: {executable}")
    LOGGER.info("ROUTE ✓ %s → %s", event_type, row["plugin_name"])


def insert_event(database: Path, event_type: str, payload: dict[str, Any]) -> int:
    """Insert one PENDING event and return its ID."""
    validate_route(database, event_type)
    with connect(database) as connection:
        cursor = connection.execute(
            "INSERT INTO events_queue(event_type,payload) VALUES (?,?)",
            (event_type, json.dumps(payload, ensure_ascii=False)),
        )
        event_id = int(cursor.lastrowid)
        connection.commit()
    LOGGER.info("DISPATCH → id=%s channel=%s", event_id, event_type)
    return event_id


def wait_for_one(database: Path, event_id: int, event_type: str) -> dict[str, Any]:
    """Wait for one prerequisite event to complete."""
    timeout = float(os.getenv("MULTI_CHANNEL_TIMEOUT_SECONDS", "180"))
    interval = float(os.getenv("MULTI_CHANNEL_POLL_SECONDS", "0.2"))
    deadline = time.monotonic() + timeout
    last_status: str | None = None
    while time.monotonic() < deadline:
        with connect(database) as connection:
            row = connection.execute(
                "SELECT status,payload,error_log FROM events_queue WHERE id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise WorkflowError(f"event {event_id} disappeared")
        if row["status"] != last_status:
            LOGGER.info("MONITOR id=%s type=%s status=%s", event_id, event_type, row["status"])
            last_status = row["status"]
        if row["status"] == "COMPLETED":
            result = json.loads(row["payload"])
            if not isinstance(result, dict):
                raise WorkflowError(f"event {event_id} returned invalid payload")
            return result
        if row["status"] == "FAILED":
            raise WorkflowError(f"event {event_id} ({event_type}) failed: {row['error_log']}")
        time.sleep(interval)
    raise WorkflowError(f"event {event_id} ({event_type}) timed out")


def ensure_skill() -> Path:
    """Create the central essay skill template when absent."""
    SKILL_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SKILL_FILE.exists():
        SKILL_FILE.write_text(
            "---\nrequired_inputs: [topic]\n---\n\n"
            "Schrijf een kanaalonafhankelijk essay over {{topic}}. "
            "Gebruik een heldere titel, compacte paragrafen en een concrete conclusie.\n",
            encoding="utf-8",
        )
    return SKILL_FILE.resolve()


def generate_essay(database: Path, topic: str, skip: bool) -> Path:
    """Generate the central essay through AI or a deterministic local fallback."""
    if skip:
        CONCEPT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = CONCEPT_DIR / f"multi_channel_local_{stamp}_{uuid4().hex[:8]}.md"
        path.write_text(
            f"# {topic}\n\nLokaal testessay voor veilige multi-channel syndicatie.\n",
            encoding="utf-8",
        )
        LOGGER.info("GENERATION skipped; local essay=%s", path)
        return path

    event_id = insert_event(
        database,
        "AI_GENERATION",
        {"skill_file": str(ensure_skill()), "inputs": {"topic": topic}},
    )
    result = wait_for_one(database, event_id, "AI_GENERATION")
    raw_path = result.get("filepath")
    if not isinstance(raw_path, str):
        raise WorkflowError("AI_GENERATION returned no filepath")
    path = Path(raw_path).expanduser().resolve(strict=True)
    LOGGER.info("GENERATION ✓ event=%s essay=%s", event_id, path)
    return path


def archive_for_channel(essay: Path, channel: str) -> Path:
    """Create a unique immutable syndication artifact for one channel."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9]+", "_", channel.lower()).strip("_")
    destination = ARCHIVE_DIR / (
        f"{essay.stem}_{slug}_{stamp}_{uuid4().hex[:8]}.md"
    )
    shutil.copy2(essay, destination)
    return destination


def dispatch_channels(
    database: Path, channels: list[str], essay: Path, topic: str, mock: bool
) -> list[ChannelRun]:
    """Dispatch all selected channels before entering the shared monitor loop."""
    runs: list[ChannelRun] = []
    for channel in channels:
        archive = archive_for_channel(essay, channel)
        payload: dict[str, Any] = {
            "topic": topic,
            "title": topic,
            "draft_file": str(archive),
            "syndication_archive": str(archive),
        }
        if mock:
            payload.update({"dry_run": True, "mock_auth_fallback": True})
        event_id = insert_event(database, channel, payload)
        runs.append(ChannelRun(channel, event_id, archive))
    return runs


def monitor_all(database: Path, runs: list[ChannelRun]) -> None:
    """Poll every active event together until all complete or one fails."""
    timeout = float(os.getenv("MULTI_CHANNEL_TIMEOUT_SECONDS", "180"))
    interval = float(os.getenv("MULTI_CHANNEL_POLL_SECONDS", "0.2"))
    deadline = time.monotonic() + timeout
    pending = {run.event_id: run for run in runs}
    while pending and time.monotonic() < deadline:
        placeholders = ",".join("?" for _ in pending)
        with connect(database) as connection:
            rows = connection.execute(
                f"SELECT id,status,error_log FROM events_queue WHERE id IN ({placeholders})",
                tuple(pending),
            ).fetchall()
        found = {int(row["id"]): row for row in rows}
        for event_id, run in list(pending.items()):
            row = found.get(event_id)
            if row is None:
                run.status = "MISSING"
                run.error = "event disappeared"
                raise WorkflowError(f"{run.channel} event {event_id} disappeared")
            status = str(row["status"])
            if status != run.status:
                run.status = status
                LOGGER.info("MONITOR id=%s channel=%s status=%s", event_id, run.channel, status)
            if status == "COMPLETED":
                pending.pop(event_id)
                LOGGER.info("CHANNEL COMPLETE ✓ %s event=%s", run.channel, event_id)
            elif status == "FAILED":
                run.error = row["error_log"] or "unknown failure"
                raise WorkflowError(
                    f"{run.channel} event {event_id} failed: {run.error}"
                )
        if pending:
            time.sleep(interval)
    if pending:
        channels = ", ".join(run.channel for run in pending.values())
        raise WorkflowError(f"channel events timed out: {channels}")


def print_report(topic: str, essay: Path, runs: list[ChannelRun]) -> None:
    """Print the final per-channel report and archive paths."""
    print("\n=== MULTI-CHANNEL EINDRAPPORT ===")
    print(f"Onderwerp: {topic}")
    print(f"Centraal essay: {essay}")
    for run in runs:
        print(
            f"- {run.channel}: status={run.status} event_id={run.event_id} "
            f"archief={run.archive_path}"
        )
    print("=== EINDE RAPPORT ===")


def main() -> int:
    """Execute generation, fan-out, concurrent monitoring, and reporting."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    topic = args.topic.strip()
    database = args.db.expanduser().resolve()
    if not topic:
        LOGGER.error("--topic must not be empty")
        return 2
    if not database.is_file():
        LOGGER.error("database not found: %s", database)
        return 2

    runs: list[ChannelRun] = []
    try:
        LOGGER.info("MULTI START topic=%r channels=%s mock=%s", topic, args.channels, args.mock)
        essay = generate_essay(database, topic, args.skip_generation)
        runs = dispatch_channels(database, args.channels, essay, topic, args.mock)
        monitor_all(database, runs)
        print_report(topic, essay, runs)
    except (WorkflowError, OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("MULTI ABORTED: %s", exc)
        if runs:
            print_report(topic, essay, runs)
        return 1
    LOGGER.info("MULTI COMPLETE channels=%s", len(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
