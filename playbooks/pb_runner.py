#!/usr/bin/env python3
"""Sequential queue-based playbook for research, generation, and publication."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Final


LOGGER: Final = logging.getLogger("playbook")
TERMINAL_STATUSES: Final = {"COMPLETED", "FAILED"}
PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent


class PlaybookError(RuntimeError):
    """Raised when an event or playbook step cannot complete successfully."""


def parse_args() -> argparse.Namespace:
    """Parse playbook arguments."""
    parser = argparse.ArgumentParser(
        description="Run the research-to-publication event playbook."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("../db/events.db"),
        help="SQLite database path (default: ../db/events.db).",
    )
    parser.add_argument("--topic", required=True, help="Topic for the content chain.")
    return parser.parse_args()


def dispatch_and_wait(
    db_path: Path, event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Queue one event and wait until its persisted status is terminal."""
    poll_interval = float(os.getenv("PLAYBOOK_POLL_INTERVAL_SECONDS", "0.2"))
    timeout = float(os.getenv("PLAYBOOK_TIMEOUT_SECONDS", "120"))
    if poll_interval <= 0 or timeout <= 0:
        raise ValueError("playbook poll interval and timeout must be positive")

    encoded_payload = json.dumps(payload, ensure_ascii=False)
    with sqlite3.connect(db_path, timeout=30.0) as connection:
        cursor = connection.execute(
            "INSERT INTO events_queue (event_type, payload) VALUES (?, ?)",
            (event_type, encoded_payload),
        )
        event_id = int(cursor.lastrowid)
        connection.commit()

    LOGGER.info("BATON → queued event id=%s type=%s", event_id, event_type)
    deadline = time.monotonic() + timeout
    last_status: str | None = None

    while time.monotonic() < deadline:
        with sqlite3.connect(db_path, timeout=30.0) as connection:
            row = connection.execute(
                "SELECT status, payload, error_log FROM events_queue WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                dead = connection.execute(
                    """
                    SELECT status, payload, error_log, reason_for_death
                      FROM dead_letter_queue WHERE id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if dead is not None:
                    raise PlaybookError(
                        f"event {event_id} entered the dead-letter queue: "
                        f"{dead[3] or dead[2] or 'unknown failure'}"
                    )
                raise PlaybookError(f"event {event_id} disappeared from the queue")

        status, result_payload, error_log = row
        if status != last_status:
            LOGGER.info("Event id=%s status=%s", event_id, status)
            last_status = status
        if status in TERMINAL_STATUSES:
            if status == "FAILED":
                raise PlaybookError(
                    f"event {event_id} failed: {error_log or 'unknown failure'}"
                )
            decoded = json.loads(result_payload)
            if not isinstance(decoded, dict):
                raise PlaybookError(f"event {event_id} returned a non-object payload")
            LOGGER.info("BATON ✓ event id=%s completed", event_id)
            return decoded
        time.sleep(poll_interval)

    raise TimeoutError(f"event {event_id} did not finish within {timeout:g} seconds")


def ensure_generation_skill() -> Path:
    """Create the playbook's deterministic generation skill when absent."""
    skill_path = PROJECT_ROOT / "vault" / "skills" / "playbook_generation.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    if not skill_path.exists():
        skill_path.write_text(
            "---\nrequired_inputs: [topic, research_context]\n---\n\n"
            "Schrijf een helder concept over {{topic}}.\n\n"
            "Gebruik deze lokale onderzoekscontext:\n{{research_context}}\n",
            encoding="utf-8",
        )
    return skill_path


def run_playbook(db_path: Path, topic: str) -> Path:
    """Run the three-stage event relay and return the generated draft path."""
    LOGGER.info("PLAYBOOK START topic=%r", topic)

    LOGGER.info("STAP 1/3 — lokale mock-research ophalen")
    research = dispatch_and_wait(
        db_path,
        "RESEARCH_MOCK",
        {
            "content": (
                f"Lokale researchcontext voor {topic}: focus op event queues, "
                "atomische verwerking, lokale modellen en fouttolerantie."
            )
        },
    )
    research_context = str(research["content"])

    LOGGER.info("STAP 2/3 — AI-generatie starten")
    skill_path = ensure_generation_skill()
    generation = dispatch_and_wait(
        db_path,
        "AI_GENERATION",
        {
            "skill_file": str(skill_path),
            "inputs": {"topic": topic, "research_context": research_context},
        },
    )
    draft_path = Path(str(generation["filepath"])).resolve(strict=True)

    LOGGER.info("STAP 3/3 — draft naar mock-publicatie sturen")
    dispatch_and_wait(
        db_path,
        "PUBLISH_MOCK",
        {"draft_file": str(draft_path)},
    )

    LOGGER.info("PLAYBOOK VOLTOOID draft=%s", draft_path)
    return draft_path


def main() -> int:
    """Execute the configured playbook."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    topic = args.topic.strip()
    if not topic:
        LOGGER.error("--topic must not be empty")
        return 2

    try:
        run_playbook(args.db.expanduser().resolve(strict=True), topic)
    except (OSError, sqlite3.Error, ValueError, KeyError, json.JSONDecodeError,
            PlaybookError, TimeoutError):
        LOGGER.exception("PLAYBOOK MISLUKT")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
