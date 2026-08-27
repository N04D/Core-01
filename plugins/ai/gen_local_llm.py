#!/usr/bin/env python3
"""Local LLM generator plugin with an isolated deterministic mock mode."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.event_protocol import emit_result
from uuid import uuid4


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from core.markdown_parser import MarkdownTemplateError, render_template  # noqa: E402
from core.editorial_loop import EditorialLoopError, run_editorial_loop  # noqa: E402
from core.rag_index import DEFAULT_ROOTS, refresh_index, search  # noqa: E402


PLUGIN_NAME: Final = "Lokale RTX 3090 Generator"
PLUGIN_TYPE: Final = "ai"
PLUGIN_ICON: Final = "🧠"
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
OUTPUT_DIRECTORY: Final = PROJECT_ROOT / "vault" / "concepten"
LOGGER: Final = logging.getLogger("gen_local_llm")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register or run the local LLM plugin.")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--mock", action="store_true", help="Use the deterministic subprocess mock."
    )
    args = parser.parse_args()
    if not args.register and args.event_id is None:
        parser.error("either --register or --event_id is required")
    if args.event_id is not None and args.event_id < 1:
        parser.error("--event_id must be positive")
    return args


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def register_plugin(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO plugin_registry
                (plugin_name, type, executable_path, icon, is_active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(plugin_name) DO UPDATE SET
                type=excluded.type,
                executable_path=excluded.executable_path,
                icon=excluded.icon,
                is_active=1
            """,
            (PLUGIN_NAME, PLUGIN_TYPE, str(Path(__file__).resolve()), PLUGIN_ICON),
        )


def load_payload(connection: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT payload FROM events_queue WHERE id = ?", (event_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"event {event_id} does not exist")
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    return payload


def resolve_skill(payload: dict[str, Any]) -> Path:
    raw_path = payload.get("skill_file")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("payload must contain a non-empty 'skill_file'")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(strict=True)


def generate(prompt: str, mock: bool, role: str = "SCHRIJVER") -> str:
    timeout = float(os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "300"))
    if timeout <= 0:
        raise ValueError("LOCAL_LLM_TIMEOUT_SECONDS must be positive")

    mock_enabled = mock or os.getenv("LOCAL_LLM_MOCK", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if mock_enabled:
        if role == "FACTCHECKER":
            checked = prompt.rsplit("CONCEPT:\n", 1)[-1].strip()
            return f"{checked}\n\nFACTCHECK_STATUS: APPROVED"
        if role == "REDACTEUR":
            checked = prompt.rsplit("GECONTROLEERD CONCEPT:\n", 1)[-1].strip()
            return checked + "\n\n---\n\n_Redactioneel gecontroleerd._"
        command = [
            sys.executable,
            "-c",
            "import sys; p=sys.stdin.read(); b=p.split('BRIEFING:\\n',1)[-1].split('\\n\\nVAULTCONTEXT:',1)[0]; print('# Mock LLM Result\\n\\n' + b)",
        ]
    else:
        command = shlex.split(os.getenv("LOCAL_LLM_COMMAND", "ollama run llama3.1:8b"))
        if not command:
            raise ValueError("LOCAL_LLM_COMMAND is empty")

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise TimeoutError(f"local LLM exceeded {timeout:g} seconds")
    if process.returncode != 0:
        raise RuntimeError(
            f"local LLM exited with {process.returncode}: {stderr.strip()[:4000]}"
        )
    if not stdout.strip():
        raise RuntimeError("local LLM returned empty output")
    return stdout.strip()


def save_output(event_id: int, output: str) -> Path:
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = OUTPUT_DIRECTORY / (
        f"concept_{event_id}_{timestamp}_{uuid4().hex[:8]}.md"
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=OUTPUT_DIRECTORY,
            prefix=".llm_",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(output.rstrip() + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return destination


def process_event(connection: sqlite3.Connection, event_id: int, mock: bool) -> tuple[Path, dict[str, Any]]:
    payload = load_payload(connection, event_id)
    prompt = render_template(resolve_skill(payload), payload)
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve()
    if os.getenv("RAG_AUTO_INDEX", "1").lower() in {"1", "true", "yes"}:
        indexed, skipped, chunks = refresh_index(database, DEFAULT_ROOTS)
        LOGGER.info(
            "RAG refresh: indexed=%s unchanged=%s chunks=%s",
            indexed,
            skipped,
            chunks,
        )
    inputs = payload.get("inputs", {})
    topic = inputs.get("topic") if isinstance(inputs, dict) else None
    matches = search(
        database,
        str(topic or prompt),
        limit=int(os.getenv("RAG_CONTEXT_CHUNKS", "5")),
    )
    context = "\n\n".join(
        f"[Bron: {Path(item.source_path).name}, chunk {item.chunk_index}, score {item.score:.3f}]\n{item.content}"
        for item in matches
    )
    editorial = run_editorial_loop(
        prompt,
        context,
        lambda role, role_prompt: generate(role_prompt, mock, role),
    )
    output_path = save_output(event_id, editorial.final_text)
    payload["rag_sources"] = [
        {"path": item.source_path, "chunk": item.chunk_index, "score": round(item.score, 4)}
        for item in matches
    ]
    payload["editorial_roles"] = list(editorial.roles)
    payload["editorial_status"] = "APPROVED"
    patch = {
        "filepath": str(output_path),
        "rag_sources": payload["rag_sources"],
        "editorial_roles": payload["editorial_roles"],
        "editorial_status": "APPROVED",
    }
    return output_path, patch


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    try:
        with connect(args.db.expanduser().resolve()) as connection:
            if args.register:
                register_plugin(connection)
                LOGGER.info("Plugin registered: %s", PLUGIN_NAME)
            if args.event_id is not None:
                output_path, patch = process_event(connection, args.event_id, args.mock)
                emit_result("SIMULATED" if args.mock else "COMPLETED", result={"filepath": str(output_path)}, payload_patch=patch)
                LOGGER.info("Event %s completed: %s", args.event_id, output_path)
    except (OSError, sqlite3.Error, ValueError, LookupError, json.JSONDecodeError,
            MarkdownTemplateError, EditorialLoopError,
            subprocess.SubprocessError, TimeoutError):
        if args.event_id is not None:
            emit_result("FAILED", error="Local LLM plugin failed", retryable=True)
        LOGGER.exception("Local LLM plugin failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
