#!/usr/bin/env python3
"""Run one idempotent, Markdown-driven 99 Names image pipeline.

The Markdown source is the only user-facing configuration.  A run selects the
first name that has no valid completed asset, generates a raw NightCafe image,
applies the configured overlay, and registers the final publication asset.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.database import connect_database
from core.setup_database import initialize_database
from core.md_subject_parser import SubjectDocument, load_subject_document
from plugins.media.image_overlay import apply_overlay, register_overlay
from plugins.media.nightcafe_automation import (
    safe_stem,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "db" / "events.db"
DEFAULT_SOURCE = ROOT / "vault" / "prompts" / "nightcafe_99names.md"
DEFAULT_CONTRACT = ROOT / "playbooks" / "contracts" / "nightcafe_99names.contract.json"
RAW_DIR = ROOT / "vault" / "media" / "nightcafe"
FINAL_DIR = ROOT / "vault" / "media" / "publishing"
LOGGER = logging.getLogger("daily_99names_runner")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="Markdown source containing frontmatter rules and subjects.")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--model")
    parser.add_argument("--format", dest="aspect_format")
    parser.add_argument("--negative-prompt", default="")
    parser.add_argument("--steps", type=int)
    parser.add_argument("--live", action="store_true", help="Use the real NightCafe browser flow.")
    parser.add_argument("--simulate", action="store_true", help="Create a deterministic local test asset.")
    parser.add_argument("--overlay-font-size", type=int, default=64)
    parser.add_argument("--llm-timeout", type=int, default=30)
    return parser.parse_args()


def validate_contract(path: Path, db_path: Path) -> dict[str, Any]:
    """Validate manifest shape, required executables/routes, and DB tables."""
    try:
        contract = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read contract {path}: {exc}") from exc
    if not isinstance(contract, dict) or not isinstance(contract.get("required_plugins"), list):
        raise ValueError("contract must contain a required_plugins list")
    required_tables = contract.get("dependencies", {}).get("sqlite_tables", [])
    with connect_database(db_path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing_tables = set(required_tables) - tables
        if missing_tables:
            raise RuntimeError(f"contract database tables missing: {sorted(missing_tables)}")
        for plugin in contract["required_plugins"]:
            name = plugin.get("name")
            row = db.execute("SELECT executable_path,is_active FROM plugin_registry WHERE plugin_name=?", (name,)).fetchone()
            if row is None or not row["is_active"]:
                raise RuntimeError(f"required plugin is not active: {name}")
            if not Path(row["executable_path"]).is_file():
                raise RuntimeError(f"plugin executable is missing: {row['executable_path']}")
            route = plugin.get("route")
            route_row = db.execute("SELECT 1 FROM event_routes WHERE event_type=? AND target_plugin_name=?", (route, name)).fetchone()
            if route_row is None:
                raise RuntimeError(f"missing route {route} -> {name}")
    return contract


def seed_from_document(db_path: Path, document: SubjectDocument) -> None:
    if len(document.subjects) > 99:
        raise ValueError("the 99 Names source may contain at most 99 subjects")
    with connect_database(db_path) as db:
        db.executemany(
            """INSERT INTO nightcafe_names(sequence,arabic_name,transliteration,meaning)
               VALUES (?,?,?,?) ON CONFLICT(sequence) DO UPDATE SET
               arabic_name=excluded.arabic_name, transliteration=excluded.transliteration,
               meaning=excluded.meaning""",
            ((s.sequence, s.title, s.title, s.context or s.title) for s in document.subjects),
        )
        db.commit()


def _valid_output(path_value: str | None) -> bool:
    if not path_value:
        return False
    path = Path(path_value)
    return path.is_file() and path.stat().st_size > 256


def next_open_subject(db_path: Path, document: SubjectDocument):
    """Return the first uncompleted subject, preserving sequence order."""
    with connect_database(db_path) as db:
        rows = db.execute("SELECT name_sequence,status,output_path FROM nightcafe_daily_runs ORDER BY name_sequence").fetchall()
        completed = {int(row["name_sequence"]) for row in rows if row["status"] in {"COMPLETED", "SIMULATED"} and _valid_output(row["output_path"])}
    for subject in document.subjects:
        if subject.sequence not in completed:
            return subject
    return None


def prompt_for(subject, document: SubjectDocument, args: argparse.Namespace) -> str:
    """Build a deterministic prompt from Markdown rules (no Python edits required)."""
    constraints = document.prompt_constraints()
    prompt = (f"A contemplative fine-art visual inspired by {subject.title}, "
              f"{subject.context or subject.title}; symbolic rather than figurative, "
              "luminous sacred geometry, intricate arabesque patterns, celestial atmosphere, "
              "deep indigo and warm gold, volumetric light, museum-quality composition, "
              "respectful Islamic aesthetic, no depiction of Allah, no people, no faces, "
              "no readable text, no watermark, high resolution")
    if constraints:
        prompt += "; " + constraints.replace("\n", "; ")
    if args.negative_prompt:
        prompt += f"; Additional negative prompt: {args.negative_prompt}"
    return prompt


def run_nightcafe(args: argparse.Namespace, subject, prompt: str) -> dict[str, Any]:
    command = [sys.executable, str(ROOT / "plugins/media/nightcafe_automation.py"), "--db", str(args.db),
               "--prompt", prompt, "--name", subject.title, "--sequence", str(subject.sequence),
               "--run-date", args.date, "--output-dir", str(RAW_DIR)]
    command.append("--live" if args.live else "--simulate")
    if args.model: command += ["--model", args.model]
    if args.aspect_format: command += ["--format", args.aspect_format]
    if args.negative_prompt: command += ["--negative-prompt", args.negative_prompt]
    if args.steps is not None: command += ["--steps", str(args.steps)]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=660, check=False)
    if completed.stderr:
        LOGGER.info("NightCafe: %s", completed.stderr.strip().splitlines()[-1])
    if completed.returncode:
        raise RuntimeError(f"NightCafe failed ({completed.returncode}): {completed.stdout[-500:]}")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("NightCafe returned no result")
    result = json.loads(lines[-1])
    if result.get("status") not in {"COMPLETED", "SIMULATED"}:
        raise RuntimeError(f"NightCafe returned {result.get('status')}: {result.get('error', '')}")
    return result


def ensure_simulated_source(path: Path, subject: str) -> None:
    """Replace the tiny automation fixture with a valid editorial-size PNG."""
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (1024, 1024), (24, 32, 72))
    draw = ImageDraw.Draw(image)
    draw.rectangle((48, 48, 976, 976), outline=(183, 155, 104), width=8)
    draw.text((72, 72), subject, fill=(247, 242, 232))
    image.save(path, format="PNG")


def mark_run(db_path: Path, subject, args: argparse.Namespace, *, prompt: str, status: str, output: Path | None = None, asset_id: int | None = None, error: str | None = None) -> None:
    with connect_database(db_path) as db:
        db.execute(
            """INSERT INTO nightcafe_daily_runs(run_date,name_sequence,prompt,status,asset_id,output_path,error_log)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_date) DO UPDATE SET name_sequence=excluded.name_sequence,
               prompt=excluded.prompt,status=excluded.status,asset_id=excluded.asset_id,output_path=excluded.output_path,
               error_log=excluded.error_log,updated_at=CURRENT_TIMESTAMP""",
            (args.date, subject.sequence, prompt, status, asset_id, str(output) if output else None, error),
        )
        db.commit()


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    args.db = args.db.expanduser().resolve()
    source = args.source.expanduser().resolve()
    initialize_database(args.db)
    document = load_subject_document(source)
    validate_contract(args.contract.expanduser().resolve(), args.db)
    seed_from_document(args.db, document)
    subject = next_open_subject(args.db, document)
    if subject is None:
        LOGGER.info("All %d Markdown subjects already have completed assets", len(document.subjects))
        return 0
    prompt = prompt_for(subject, document, args)
    LOGGER.info("Selected %02d/%02d: %s", subject.sequence, len(document.subjects), subject.title)
    mark_run(args.db, subject, args, prompt=prompt, status="PREPARED")
    try:
        raw_result = run_nightcafe(args, subject, prompt)
        raw = Path(raw_result["output_path"]).resolve()
        if not raw.is_file():
            raise FileNotFoundError(f"NightCafe output missing: {raw}")
        if raw_result.get("status") == "SIMULATED":
            ensure_simulated_source(raw, subject.title)
        FINAL_DIR.mkdir(parents=True, exist_ok=True)
        final = FINAL_DIR / f"{args.date}_{subject.sequence:02d}_{safe_stem(subject.title)}.jpg"
        caption = "\n".join((subject.title, subject.title, subject.context or subject.title))
        apply_overlay(raw, final, text=caption, font_size=args.overlay_font_size)
        asset_id = register_overlay(args.db, final, caption)
        status = "SIMULATED" if raw_result.get("status") == "SIMULATED" else "COMPLETED"
        mark_run(args.db, subject, args, prompt=prompt, status=status, output=final, asset_id=asset_id)
        print(json.dumps({"status": status, "sequence": subject.sequence, "name": subject.title, "raw": str(raw), "output": str(final), "asset_id": asset_id}, ensure_ascii=False))
        return 0
    except Exception as exc:
        mark_run(args.db, subject, args, prompt=prompt, status="FAILED", error=str(exc)[:2000])
        LOGGER.exception("Daily 99 Names pipeline failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
