#!/usr/bin/env python3
"""Safe end-to-end dry-run test for the production LinkedIn publisher."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Final


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = Path("../db/events.db")
PLUGIN: Final = PROJECT_ROOT / "plugins" / "channels" / "pub_linkedin.py"
SCREENSHOT_DIRECTORY: Final = PROJECT_ROOT / "vault" / "logs" / "screenshots"
PLUGIN_NAME: Final = "LinkedIn Publisher (Productie)"
EVENT_TYPE: Final = "PUBLISH_LINKEDIN"
TEST_CONTENT: Final = (
    "Dit is een geautomatiseerde Playwright dry-run test voor LinkedIn "
    "vanuit ons lokale AI OS."
)
LOGGER: Final = logging.getLogger("test_linkedin_dryrun")


def parse_args() -> argparse.Namespace:
    """Parse test configuration."""
    parser = argparse.ArgumentParser(
        description="Run a safe visual LinkedIn publisher dry-run."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    return parser.parse_args()


def run(command: list[str], environment: dict[str, str] | None = None) -> None:
    """Run a subprocess, mirror its output, and fail on non-zero status."""
    LOGGER.info("COMMAND: %s", " ".join(command))
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed with exit status {result.returncode}: {' '.join(command)}"
        )


def register_and_route(database: Path) -> None:
    """Register the plugin and configure its event route idempotently."""
    run([sys.executable, str(PLUGIN), "--register", "--db", str(database)])
    with sqlite3.connect(database, timeout=30.0) as connection:
        connection.execute(
            """
            INSERT INTO event_routes (event_type, target_plugin_name)
            VALUES (?, ?)
            ON CONFLICT(event_type) DO UPDATE SET
                target_plugin_name = excluded.target_plugin_name
            """,
            (EVENT_TYPE, PLUGIN_NAME),
        )
        connection.commit()
    LOGGER.info("Route configured: %s → %s", EVENT_TYPE, PLUGIN_NAME)


def inject_event(database: Path) -> int:
    """Insert the exact LinkedIn dry-run event payload."""
    payload = {"content": TEST_CONTENT, "dry_run": True}
    with sqlite3.connect(database, timeout=30.0) as connection:
        cursor = connection.execute(
            """
            INSERT INTO events_queue (event_type, payload, status)
            VALUES (?, ?, 'PENDING')
            """,
            (EVENT_TYPE, json.dumps(payload, ensure_ascii=False)),
        )
        event_id = int(cursor.lastrowid)
        connection.commit()
    LOGGER.info("Injected PENDING dry-run event id=%s", event_id)
    return event_id


def visual_command(event_id: int, database: Path) -> tuple[list[str], dict[str, str]]:
    """Build a headed Playwright command, optionally under virtual X11."""
    command = [
        sys.executable,
        str(PLUGIN),
        "--event_id",
        str(event_id),
        "--db",
        str(database),
        "--dry-run",
    ]
    environment = os.environ.copy()
    environment["LINKEDIN_HEADLESS"] = "false"

    auth_candidates = (
        PROJECT_ROOT / "config" / "linkedin_auth.json",
        PROJECT_ROOT / "linkedin_auth.json",
        PROJECT_ROOT / "auth.json",
    )
    if not any(path.is_file() for path in auth_candidates):
        LOGGER.warning(
            "config/linkedin_auth.json ontbreekt; veilige lokale Playwright-mock actief"
        )
        command.append("--mock-auth-fallback")

    if environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY"):
        LOGGER.info("Visual backend: existing desktop session")
        return command, environment
    xvfb_run = shutil.which("xvfb-run")
    if xvfb_run:
        LOGGER.info("Visual backend: xvfb-run fallback")
        return [xvfb_run, "-a", *command], environment
    raise RuntimeError(
        "no desktop DISPLAY/WAYLAND_DISPLAY and xvfb-run is unavailable; "
        "install xvfb or run this test from a desktop session"
    )


def verify_result(database: Path, event_id: int, previous: set[Path]) -> Path:
    """Verify terminal state and identify the newly generated dry-run screenshot."""
    with sqlite3.connect(database, timeout=30.0) as connection:
        row = connection.execute(
            "SELECT status, payload, error_log FROM events_queue WHERE id = ?",
            (event_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"event {event_id} disappeared")
    status, raw_payload, error_log = row
    if status != "COMPLETED":
        raise RuntimeError(
            f"event {event_id} did not complete: status={status}, error={error_log}"
        )

    payload = json.loads(raw_payload)
    declared = payload.get("linkedin_screenshot")
    candidates = set(SCREENSHOT_DIRECTORY.glob("linkedin_*_dry_run_*.png")) - previous
    screenshot = Path(declared) if isinstance(declared, str) else None
    if screenshot is None or screenshot not in candidates or not screenshot.is_file():
        raise RuntimeError("no new LinkedIn dry-run screenshot was persisted")
    if payload.get("linkedin_published") is not False:
        raise RuntimeError("dry-run safety assertion failed: published flag is not false")

    LOGGER.info("Verified event status=COMPLETED and published=false")
    LOGGER.info("Screenshot: %s", screenshot)
    return screenshot


def main() -> int:
    """Execute the safe visual dry-run integration test."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database = parse_args().db.expanduser().resolve()
    if not database.is_file():
        LOGGER.error("Database not found: %s", database)
        return 2

    SCREENSHOT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    previous = set(SCREENSHOT_DIRECTORY.glob("linkedin_*_dry_run_*.png"))
    try:
        register_and_route(database)
        event_id = inject_event(database)
        command, environment = visual_command(event_id, database)
        run(command, environment)
        screenshot = verify_result(database, event_id, previous)
    except Exception:
        LOGGER.exception("LinkedIn dry-run test FAILED safely; no post was published")
        return 1

    print(f"[PASS] LinkedIn dry-run voltooid. Screenshot: {screenshot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
