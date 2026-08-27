#!/usr/bin/env python3
"""Complete generation, multi-channel syndication, and insight test suite."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database import connect_database


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.evergreen import analyze as analyze_evergreen  # noqa: E402
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
WORKER: Final = PROJECT_ROOT / "daemon" / "worker.py"
SKILL: Final = PROJECT_ROOT / "vault" / "skills" / "master_syndication_suite.md"
ARCHIVE_DIR: Final = PROJECT_ROOT / "vault" / "gepubliceerd"
ANALYTICS_DIR: Final = PROJECT_ROOT / "vault" / "analytics"
RESEARCH_DIR: Final = PROJECT_ROOT / "vault" / "research"
DEFAULT_CHANNELS: Final = (
    "PUBLISH_LINKEDIN_PRO",
    "PUBLISH_SUBSTACK_PRO",
    "PUBLISH_MEDIUM",
)
VARIANT_SKILLS: Final = {
    "linkedin": PROJECT_ROOT / "vault" / "skills" / "variant_linkedin.md",
    "substack": PROJECT_ROOT / "vault" / "skills" / "variant_substack.md",
    "medium": PROJECT_ROOT / "vault" / "skills" / "variant_medium.md",
}
INSIGHT_COMMANDS: Final = {
    "LinkedIn Pro Publisher & Analytics": (
        ("analytics", "--analytics"),
        ("profile", "--fetch-bio"),
    ),
    "Substack Pro Publisher & Analytics": (
        ("analytics", "--analytics"),
        ("comments", "--read-comments"),
        ("profile", "--fetch-profile"),
    ),
}
LOGGER: Final = logging.getLogger("master_syndication_suite")


class SuiteError(RuntimeError):
    """Raised when a suite stage cannot complete safely."""


@dataclass
class EventRun:
    event_id: int
    event_type: str
    plugin_name: str
    status: str = "PENDING"
    archive_path: Path | None = None
    error: str | None = None


@dataclass
class InsightRun:
    plugin_name: str
    capability: str
    status: str
    artifacts: list[Path] = field(default_factory=list)
    error: str | None = None


def parse_args() -> argparse.Namespace:
    """Parse suite topic, database, channels, media, and safety mode."""
    parser = argparse.ArgumentParser(
        description="Run the complete AI OS syndication and analytics suite."
    )
    parser.add_argument("--topic", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--channels",
        nargs="+",
        default=list(DEFAULT_CHANNELS),
        help="Event types to include; inactive/unrouted channels are skipped.",
    )
    parser.add_argument("--image-path", type=Path)
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the local LLM mock and safe platform auth fallbacks.",
    )
    parser.add_argument(
        "--collect-insights",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run analytics/profile capabilities (default: enabled in mock mode).",
    )
    args = parser.parse_args()
    args.channels = list(dict.fromkeys(args.channels))
    return args


def connect(database: Path) -> sqlite3.Connection:
    return connect_database(database)


def active_channels(database: Path, requested: list[str]) -> list[tuple[str, str, Path]]:
    """Resolve only event routes backed by active executable plugins."""
    resolved = []
    with connect(database) as connection:
        for event_type in requested:
            row = connection.execute(
                """
                SELECT pr.plugin_name,pr.executable_path
                  FROM event_routes er
                  JOIN plugin_registry pr ON pr.plugin_name=er.target_plugin_name
                 WHERE er.event_type=? AND pr.is_active=1
                """,
                (event_type,),
            ).fetchone()
            if row is None:
                LOGGER.warning("SKIP channel=%s reason=no active route", event_type)
                continue
            executable = Path(row["executable_path"]).expanduser().resolve()
            if not executable.is_file() or not os.access(executable, os.X_OK):
                LOGGER.warning("SKIP channel=%s reason=executable unavailable", event_type)
                continue
            resolved.append((event_type, str(row["plugin_name"]), executable))
            LOGGER.info("ACTIVE ✓ %s → %s", event_type, row["plugin_name"])
    if not resolved:
        raise SuiteError("none of the requested channels has an active route")
    return resolved


def ensure_skill() -> Path:
    """Create the central long-form generation skill if absent."""
    SKILL.parent.mkdir(parents=True, exist_ok=True)
    if not SKILL.exists():
        SKILL.write_text(
            "---\nrequired_inputs: [topic]\n---\n\n"
            "Schrijf een diepgaand, kanaalonafhankelijk Markdown-essay over "
            "{{topic}}. Gebruik een sterke titel, een heldere inleiding, minimaal "
            "drie inhoudelijke secties, concrete voorbeelden en een genuanceerde "
            "conclusie. Vermijd onbevestigde feiten.\n",
            encoding="utf-8",
        )
    return SKILL.resolve()


def insert_event(database: Path, event_type: str, payload: dict[str, Any]) -> int:
    with connect(database) as connection:
        cursor = connection.execute(
            "INSERT INTO events_queue(event_type,payload) VALUES (?,?)",
            (event_type, json.dumps(payload, ensure_ascii=False)),
        )
        event_id = int(cursor.lastrowid)
        connection.commit()
    LOGGER.info("DISPATCH → id=%s type=%s", event_id, event_type)
    return event_id


def worker_once(database: Path, mock: bool) -> None:
    """Run one bounded worker poll and mirror its logs."""
    environment = os.environ.copy()
    environment["PATH"] = str(PROJECT_ROOT / "venv" / "bin") + os.pathsep + environment.get("PATH", "")
    if mock:
        environment["LOCAL_LLM_MOCK"] = "1"
    command = [
        sys.executable,
        str(WORKER),
        "--database",
        str(database),
        "--once",
    ]
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=360,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode != 0:
        raise SuiteError(f"worker exited with status {result.returncode}")


def read_event(database: Path, event_id: int) -> sqlite3.Row:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT status,payload,retry_count,error_log FROM events_queue WHERE id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            dead = connection.execute(
                "SELECT reason_for_death FROM dead_letter_queue WHERE id=?",
                (event_id,),
            ).fetchone()
            reason = dead[0] if dead else "event disappeared"
            raise SuiteError(f"event {event_id} unavailable: {reason}")
    return row


def drive_event(database: Path, run: EventRun, mock: bool) -> dict[str, Any]:
    """Drive worker cycles until one prerequisite event is terminal."""
    for _ in range(30):
        row = read_event(database, run.event_id)
        run.status = str(row["status"])
        LOGGER.info(
            "MONITOR id=%s type=%s status=%s retries=%s",
            run.event_id,
            run.event_type,
            run.status,
            row["retry_count"],
        )
        if run.status in {"COMPLETED", "SIMULATED"}:
            result = json.loads(row["payload"])
            if not isinstance(result, dict):
                raise SuiteError(f"event {run.event_id} returned invalid payload")
            return result
        if run.status in {"FAILED", "BLOCKED_AUTH"}:
            run.error = row["error_log"] or "unknown failure"
            raise SuiteError(f"event {run.event_id} failed: {run.error}")
        worker_once(database, mock)
        time.sleep(0.05)
    raise SuiteError(f"event {run.event_id} timed out")


def generate(database: Path, topic: str, mock: bool) -> tuple[EventRun, Path]:
    """Generate and verify the central Markdown essay."""
    event_id = insert_event(
        database,
        "AI_GENERATION",
        {"skill_file": str(ensure_skill()), "inputs": {"topic": topic}},
    )
    run = EventRun(event_id, "AI_GENERATION", "Lokale RTX 3090 Generator")
    payload = drive_event(database, run, mock)
    raw_path = payload.get("filepath")
    if not isinstance(raw_path, str):
        raise SuiteError("AI generation returned no filepath")
    essay = Path(raw_path).expanduser().resolve(strict=True)
    if not essay.read_text(encoding="utf-8").strip():
        raise SuiteError("generated essay is empty")
    LOGGER.info("GENERATION ✓ essay=%s", essay)
    return run, essay


def variant_channel(event_type: str) -> str:
    lowered = event_type.casefold()
    for channel in VARIANT_SKILLS:
        if channel in lowered:
            return channel
    raise SuiteError(f"no generation variant configured for {event_type}")


def generate_variants(
    database: Path,
    topic: str,
    essay: Path,
    channels: list[tuple[str, str, Path]],
    mock: bool,
) -> dict[str, Path]:
    """Generate and persist one editorially approved artifact per channel."""
    source_text = essay.read_text(encoding="utf-8").strip()
    maximum = int(os.getenv("VARIANT_MAX_SOURCE_CHARS", "60000"))
    variants: dict[str, Path] = {}
    for event_type, _plugin_name, _executable in channels:
        channel = variant_channel(event_type)
        if channel in variants:
            continue
        event_id = insert_event(
            database,
            "AI_GENERATION",
            {
                "skill_file": str(VARIANT_SKILLS[channel]),
                "inputs": {"topic": topic, "source_essay": source_text[:maximum]},
                "variant_channel": channel,
                "source_essay": str(essay),
            },
        )
        run = EventRun(event_id, "AI_GENERATION", "Lokale RTX 3090 Generator")
        payload = drive_event(database, run, mock)
        raw_path = payload.get("filepath")
        if not isinstance(raw_path, str):
            raise SuiteError(f"{channel} variant returned no filepath")
        variant = Path(raw_path).expanduser().resolve(strict=True)
        if not variant.read_text(encoding="utf-8").strip():
            raise SuiteError(f"{channel} variant is empty")
        with connect(database) as connection:
            connection.execute(
                """INSERT INTO content_variants(source_essay,channel,variant_path,generation_event_id)
                   VALUES (?,?,?,?)""",
                (str(essay), channel, str(variant), event_id),
            )
            connection.commit()
        variants[channel] = variant
        LOGGER.info("VARIANT ✓ channel=%s event_id=%s path=%s", channel, event_id, variant)
    return variants


def validate_image(path: Path | None, mock: bool) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve(strict=False)
    if resolved.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise SuiteError("--image-path must be JPG or PNG")
    if not resolved.is_file() and not mock:
        raise SuiteError(f"image does not exist: {resolved}")
    return resolved


def archive_copy(essay: Path, event_type: str) -> Path:
    """Create a unique per-channel immutable syndication artifact."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9]+", "_", event_type.lower()).strip("_")
    destination = ARCHIVE_DIR / f"{essay.stem}_{slug}_{stamp}_{uuid4().hex[:8]}.md"
    shutil.copy2(essay, destination)
    return destination


def dispatch_publications(
    database: Path,
    channels: list[tuple[str, str, Path]],
    essay: Path,
    variants: dict[str, Path],
    topic: str,
    image: Path | None,
    mock: bool,
) -> list[EventRun]:
    runs = []
    for event_type, plugin_name, _executable in channels:
        channel = variant_channel(event_type)
        variant = variants[channel]
        archive = archive_copy(variant, event_type)
        payload: dict[str, Any] = {
            "title": topic,
            "topic": topic,
            "draft_file": str(archive),
            "syndication_archive": str(archive),
            "source_essay": str(essay),
            "content_variant": channel,
            "tags": ["Local AI", "Automation"],
        }
        if image is not None:
            payload["image_path"] = str(image)
        if mock:
            payload.update({"dry_run": True, "mock_auth_fallback": True})
        event_id = insert_event(database, event_type, payload)
        runs.append(
            EventRun(event_id, event_type, plugin_name, archive_path=archive)
        )
    return runs


def drive_publications(database: Path, runs: list[EventRun], mock: bool) -> None:
    """Drive all channel events until every one completes."""
    remaining = {run.event_id: run for run in runs}
    for _ in range(100):
        for event_id, run in list(remaining.items()):
            row = read_event(database, event_id)
            status = str(row["status"])
            if status != run.status:
                run.status = status
                LOGGER.info("MONITOR id=%s channel=%s status=%s", event_id, run.event_type, status)
            if status in {"COMPLETED", "SIMULATED"}:
                remaining.pop(event_id)
            elif status in {"FAILED", "BLOCKED_AUTH"}:
                run.error = row["error_log"] or "unknown failure"
                raise SuiteError(f"{run.event_type} failed: {run.error}")
        if not remaining:
            return
        worker_once(database, mock)
        time.sleep(0.05)
    raise SuiteError("publication events timed out")


def artifact_snapshot() -> set[Path]:
    return {
        path.resolve()
        for directory in (ANALYTICS_DIR, RESEARCH_DIR)
        for path in directory.glob("*")
        if path.is_file()
    }


def collect_insights(
    database: Path, channels: list[tuple[str, str, Path]]
) -> list[InsightRun]:
    """Run supported analytics/profile capabilities for active channel plugins."""
    insights = []
    for _event_type, plugin_name, executable in channels:
        for capability, flag in INSIGHT_COMMANDS.get(plugin_name, ()):
            before = artifact_snapshot()
            command = [sys.executable, str(executable), flag, "--db", str(database)]
            LOGGER.info("INSIGHT → plugin=%s capability=%s", plugin_name, capability)
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=360,
            )
            if result.stdout:
                print(result.stdout.rstrip())
            if result.stderr:
                print(result.stderr.rstrip(), file=sys.stderr)
            after = artifact_snapshot()
            created = sorted(after - before)
            status = "COMPLETED" if result.returncode == 0 and created else "FAILED"
            insight = InsightRun(
                plugin_name,
                capability,
                status,
                created,
                None if status == "COMPLETED" else "no artifact or non-zero exit",
            )
            insights.append(insight)
            if status != "COMPLETED":
                raise SuiteError(f"insight failed: {plugin_name}/{capability}")
    return insights


def report(
    topic: str,
    generation: EventRun,
    essay: Path,
    variants: dict[str, Path],
    publications: list[EventRun],
    insights: list[InsightRun],
) -> None:
    print("\n=== MASTER SYNDICATION SUITE RAPPORT ===")
    print(f"Topic: {topic}")
    print(
        f"Generation: status={generation.status} event_id={generation.event_id} essay={essay}"
    )
    print("Variants:")
    for channel, path in variants.items():
        print(f"- {channel}: {path}")
    print("Publications:")
    for run in publications:
        print(
            f"- {run.event_type}: plugin={run.plugin_name} status={run.status} "
            f"event_id={run.event_id} archive={run.archive_path}"
        )
    print("Insights:")
    if not insights:
        print("- overgeslagen")
    for insight in insights:
        artifacts = ", ".join(str(path) for path in insight.artifacts)
        print(
            f"- {insight.plugin_name}/{insight.capability}: "
            f"status={insight.status} artifacts={artifacts}"
        )
    print("=== EINDE RAPPORT ===")


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    database = args.db.expanduser().resolve()
    topic = args.topic.strip()
    if not topic or not database.is_file():
        LOGGER.error("valid --topic and existing --db are required")
        return 2
    collect = args.collect_insights if args.collect_insights is not None else args.mock
    generation: EventRun | None = None
    essay: Path | None = None
    publications: list[EventRun] = []
    variants: dict[str, Path] = {}
    insights: list[InsightRun] = []
    try:
        channels = active_channels(database, args.channels)
        image = validate_image(args.image_path, args.mock)
        LOGGER.info(
            "SUITE START topic=%r channels=%s mock=%s insights=%s",
            topic,
            [channel[0] for channel in channels],
            args.mock,
            collect,
        )
        generation, essay = generate(database, topic, args.mock)
        variants = generate_variants(database, topic, essay, channels, args.mock)
        publications = dispatch_publications(
            database, channels, essay, variants, topic, image, args.mock
        )
        drive_publications(database, publications, args.mock)
        if collect:
            insights = collect_insights(database, channels)
        processed, evergreen = analyze_evergreen(
            database,
            ANALYTICS_DIR,
            float(os.getenv("EVERGREEN_SCORE_THRESHOLD", "25")),
            int(os.getenv("EVERGREEN_REPURPOSE_DAYS", "90")),
        )
        LOGGER.info("EVERGREEN analytics_processed=%s flagged=%s", processed, evergreen)
        report(topic, generation, essay, variants, publications, insights)
    except (SuiteError, OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("SUITE ABORTED: %s", exc)
        if generation is not None and essay is not None:
            report(topic, generation, essay, variants, publications, insights)
        return 1
    LOGGER.info("SUITE COMPLETE channels=%s insights=%s", len(publications), len(insights))
    return 0


if __name__ == "__main__":
    sys.exit(main())
