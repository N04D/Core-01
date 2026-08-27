#!/usr/bin/env python3
"""Master workflow orchestrator for research, generation, and syndication."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database import connect_database


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
DEFAULT_SKILL: Final = PROJECT_ROOT / "vault" / "skills" / "master_workflow.md"
LOGGER: Final = logging.getLogger("master_workflow")
TERMINAL_STATUSES: Final = {"COMPLETED", "SIMULATED", "BLOCKED_AUTH", "FAILED"}
SUCCESS_STATUSES: Final = {"COMPLETED", "SIMULATED"}
SUPPORTED_CHANNELS: Final = ("PUBLISH_MOCK", "PUBLISH_LINKEDIN")


class WorkflowError(RuntimeError):
    """Base error for an aborted master workflow."""


class EventFailed(WorkflowError):
    """A queue event reached a failed terminal state."""

    def __init__(self, event_id: int, event_type: str, reason: str) -> None:
        self.event_id = event_id
        self.event_type = event_type
        self.reason = reason
        super().__init__(f"event {event_id} ({event_type}) failed: {reason}")


@dataclass(frozen=True)
class EventResult:
    """Completed queue event and its resulting payload."""

    event_id: int
    event_type: str
    payload: dict[str, Any]


def parse_args() -> argparse.Namespace:
    """Parse master workflow inputs."""
    parser = argparse.ArgumentParser(
        description="Orchestrate crawl, generation, and publication events."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--url", help="Optional HTTP(S) source URL for Firecrawl.")
    parser.add_argument(
        "--publish-channel",
        choices=SUPPORTED_CHANNELS,
        default="PUBLISH_MOCK",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Use PUBLISH_LINKEDIN when config/linkedin_auth.json exists; "
            "otherwise fall back safely to PUBLISH_MOCK."
        ),
    )
    return parser.parse_args()


def resolve_publish_channel(requested: str, live: bool) -> str:
    """Resolve the publication channel with an auth-gated live-mode switch."""
    if not live:
        return requested

    auth_file = PROJECT_ROOT / "config" / "linkedin_auth.json"
    if auth_file.is_file():
        LOGGER.info("LIVE MODE enabled; LinkedIn auth state found: %s", auth_file)
        return "PUBLISH_LINKEDIN"

    LOGGER.warning(
        "LIVE MODE requested, but %s is missing; falling back to PUBLISH_MOCK",
        auth_file,
    )
    return "PUBLISH_MOCK"


def connect(database: Path) -> sqlite3.Connection:
    """Open a configured database connection."""
    return connect_database(database)


def validate_route(database: Path, event_type: str) -> None:
    """Ensure an event type resolves to an active executable plugin."""
    with connect(database) as connection:
        row = connection.execute(
            """
            SELECT pr.plugin_name, pr.executable_path
              FROM event_routes AS er
              JOIN plugin_registry AS pr
                ON pr.plugin_name = er.target_plugin_name
             WHERE er.event_type = ? AND pr.is_active = 1
            """,
            (event_type,),
        ).fetchone()
    if row is None:
        raise WorkflowError(f"no active plugin route for {event_type}")
    executable = Path(row["executable_path"]).expanduser()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise WorkflowError(
            f"plugin {row['plugin_name']!r} is not executable: {executable}"
        )
    LOGGER.info("ROUTE ✓ %s → %s", event_type, row["plugin_name"])


def dispatch_and_wait(
    database: Path, event_type: str, payload: dict[str, Any]
) -> EventResult:
    """Dispatch one event and monitor it until completion, failure, or timeout."""
    validate_route(database, event_type)
    poll_interval = float(os.getenv("MASTER_POLL_INTERVAL_SECONDS", "0.2"))
    timeout = float(os.getenv("MASTER_EVENT_TIMEOUT_SECONDS", "180"))
    if poll_interval <= 0 or timeout <= 0:
        raise WorkflowError("poll interval and event timeout must be positive")

    with connect(database) as connection:
        cursor = connection.execute(
            "INSERT INTO events_queue (event_type, payload) VALUES (?, ?)",
            (event_type, json.dumps(payload, ensure_ascii=False)),
        )
        event_id = int(cursor.lastrowid)
        connection.commit()

    LOGGER.info("DISPATCH → id=%s type=%s", event_id, event_type)
    last_status: str | None = None
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        with connect(database) as connection:
            row = connection.execute(
                """
                SELECT status, payload, retry_count, error_log
                  FROM events_queue WHERE id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                dead = connection.execute(
                    """
                    SELECT reason_for_death, error_log
                      FROM dead_letter_queue WHERE id = ?
                    """,
                    (event_id,),
                ).fetchone()
                reason = (
                    dead["reason_for_death"] or dead["error_log"]
                    if dead is not None
                    else "event disappeared from both queues"
                )
                raise EventFailed(event_id, event_type, reason)

        status = row["status"]
        if status != last_status:
            LOGGER.info(
                "MONITOR id=%s type=%s status=%s retries=%s",
                event_id,
                event_type,
                status,
                row["retry_count"],
            )
            last_status = status

        if status in SUCCESS_STATUSES:
            try:
                decoded = json.loads(row["payload"])
            except json.JSONDecodeError as exc:
                raise EventFailed(event_id, event_type, "invalid result payload") from exc
            if not isinstance(decoded, dict):
                raise EventFailed(event_id, event_type, "result payload is not an object")
            LOGGER.info("TERMINAL ✓ id=%s type=%s status=%s", event_id, event_type, status)
            return EventResult(event_id, event_type, decoded)
        if status in {"FAILED", "BLOCKED_AUTH"}:
            raise EventFailed(
                event_id, event_type, row["error_log"] or "unknown plugin failure"
            )
        time.sleep(poll_interval)

    raise EventFailed(event_id, event_type, f"timeout after {timeout:g} seconds")


def ensure_skill_template() -> Path:
    """Create the stable master-workflow skill template when absent."""
    DEFAULT_SKILL.parent.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_SKILL.exists():
        DEFAULT_SKILL.write_text(
            "---\n"
            "required_inputs: [topic, research_context]\n"
            "---\n\n"
            "Schrijf een publiceerbaar, helder concept over {{topic}}.\n\n"
            "Gebruik de volgende onderzoekscontext en verzin geen ontbrekende feiten:\n\n"
            "{{research_context}}\n",
            encoding="utf-8",
        )
    return DEFAULT_SKILL.resolve()


def read_research(result: EventResult) -> str:
    """Read Firecrawl Markdown output from its enriched event payload."""
    raw_path = result.payload.get("filepath")
    if not isinstance(raw_path, str) or not raw_path:
        raise EventFailed(
            result.event_id, result.event_type, "crawler returned no filepath"
        )
    path = Path(raw_path).expanduser().resolve(strict=True)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise EventFailed(result.event_id, result.event_type, "research file is empty")
    maximum = int(os.getenv("MASTER_MAX_RESEARCH_CHARS", "50000"))
    return text[:maximum]


def run_workflow(
    database: Path, topic: str, url: str | None, publish_channel: str
) -> EventResult:
    """Execute the complete baton relay."""
    LOGGER.info(
        "MASTER START topic=%r url=%r publish_channel=%s",
        topic,
        url,
        publish_channel,
    )

    if url is not None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise WorkflowError("--url must be an absolute HTTP(S) URL")
        LOGGER.info("STAP 1/3 — Firecrawl research")
        crawl_result = dispatch_and_wait(database, "CRAWL_URL", {"url": url})
        research_context = read_research(crawl_result)
        LOGGER.info(
            "BATON research → generation (%s characters)", len(research_context)
        )
    else:
        LOGGER.info("STAP 1/3 — overgeslagen; geen --url opgegeven")
        research_context = (
            f"Lokale briefing voor {topic}: behandel event-driven architectuur, "
            "datasoevereiniteit, fouttolerantie en lokaal uitgevoerde AI-modellen."
        )

    LOGGER.info("STAP 2/3 — lokale AI-generatie")
    generation_result = dispatch_and_wait(
        database,
        "AI_GENERATION",
        {
            "skill_file": str(ensure_skill_template()),
            "inputs": {
                "topic": topic,
                "research_context": research_context,
            },
        },
    )
    draft = generation_result.payload.get("filepath")
    if not isinstance(draft, str) or not Path(draft).is_file():
        raise EventFailed(
            generation_result.event_id,
            generation_result.event_type,
            "generator returned no readable draft filepath",
        )
    LOGGER.info("BATON draft → publication (%s)", draft)

    LOGGER.info("STAP 3/3 — syndicatie via %s", publish_channel)
    publication_result = dispatch_and_wait(
        database,
        publish_channel,
        {"draft_file": draft, "topic": topic},
    )
    LOGGER.info(
        "MASTER COMPLETE publication_event_id=%s draft=%s",
        publication_result.event_id,
        draft,
    )
    return publication_result


def main() -> int:
    """Run the configured master workflow with concise failure reporting."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    topic = args.topic.strip()
    if not topic:
        LOGGER.error("MASTER ABORTED: --topic must not be empty")
        return 2
    database = args.db.expanduser().resolve()
    if not database.is_file():
        LOGGER.error("MASTER ABORTED: database not found: %s", database)
        return 2

    try:
        publish_channel = resolve_publish_channel(args.publish_channel, args.live)
        run_workflow(database, topic, args.url, publish_channel)
    except EventFailed as exc:
        LOGGER.error(
            "MASTER ABORTED at event id=%s type=%s: %s",
            exc.event_id,
            exc.event_type,
            exc.reason,
        )
        return 1
    except (WorkflowError, OSError, sqlite3.Error, ValueError, KeyError) as exc:
        LOGGER.error("MASTER ABORTED before completion: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
