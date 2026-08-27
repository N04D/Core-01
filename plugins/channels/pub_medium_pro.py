#!/usr/bin/env python3
"""Medium Pro channel publisher with Playwright and safe mock fallback."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.event_protocol import begin_submission, emit_result, update_publication
from core.database import connect_database
from core.browser_robustness import (
    capture_sanitized_diagnostic, find_with_accessible_fallbacks,
    verify_submission,
)


PLUGIN_NAME: Final = "Medium Publisher"
PLUGIN_TYPE: Final = "channel"
PLUGIN_ICON: Final = "✍️"
EVENT_TYPE: Final = "PUBLISH_MEDIUM"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE: Final = Path("../db/events.db")
DEFAULT_AUTH: Final = PROJECT_ROOT / "config" / "medium_auth.json"
SCREENSHOT_DIR: Final = PROJECT_ROOT / "vault" / "logs" / "screenshots"
LOGGER: Final = logging.getLogger("pub_medium_pro")


class MediumPublisherError(RuntimeError):
    """Raised when Medium publication cannot safely complete."""


def parse_args() -> argparse.Namespace:
    """Parse event-bus, content, and browser options."""
    parser = argparse.ArgumentParser(
        description="Register or execute the Medium Pro publisher."
    )
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--text", help="Inline story body; overrides payload content.")
    parser.add_argument("--image-path", type=Path, help="JPG/PNG header image.")
    parser.add_argument(
        "--tags",
        nargs="*",
        help="Publication tags, separated by spaces and/or commas (maximum five).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override MEDIUM_HEADLESS (default: true).",
    )
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH)
    args = parser.parse_args()
    if not any((args.register, args.event_id is not None, args.text is not None)):
        parser.error("choose --register, --event_id, or --text")
    if args.event_id is not None and args.event_id < 1:
        parser.error("--event_id must be positive")
    return args


def connect(database: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    return connect_database(database)


def register_plugin(connection: sqlite3.Connection) -> None:
    """Register the plugin and its canonical event route atomically."""
    with connection:
        connection.execute(
            """
            INSERT INTO plugin_registry
                (plugin_name,type,executable_path,icon,is_active)
            VALUES (?,?,?,?,1)
            ON CONFLICT(plugin_name) DO UPDATE SET
                type=excluded.type, executable_path=excluded.executable_path,
                icon=excluded.icon, is_active=1
            """,
            (PLUGIN_NAME, PLUGIN_TYPE, str(Path(__file__).resolve()), PLUGIN_ICON),
        )
        connection.execute(
            """
            INSERT INTO event_routes(event_type,target_plugin_name)
            VALUES (?,?)
            ON CONFLICT(event_type) DO UPDATE SET
                target_plugin_name=excluded.target_plugin_name
            """,
            (EVENT_TYPE, PLUGIN_NAME),
        )
    LOGGER.info("Plugin registered: %s; route=%s", PLUGIN_NAME, EVENT_TYPE)


def load_payload(connection: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    """Load and validate one queue payload."""
    row = connection.execute(
        "SELECT payload FROM events_queue WHERE id=?", (event_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"event {event_id} does not exist")
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    return payload


def apply_cli_overrides(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    """Apply only explicitly supplied CLI values."""
    if args.text is not None:
        payload["content"] = args.text
    if args.image_path is not None:
        payload["image_path"] = str(args.image_path)
    if args.tags is not None:
        payload["tags"] = args.tags


def read_story(payload: dict[str, Any]) -> tuple[str, str]:
    """Resolve title and body from inline content or a Markdown file."""
    raw_content = payload.get("content")
    if isinstance(raw_content, str) and raw_content.strip():
        body = raw_content.strip()
    else:
        raw_path = payload.get("draft_file", payload.get("filepath"))
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("payload requires content, draft_file, or filepath")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        text = path.resolve(strict=True).read_text(encoding="utf-8")
        frontmatter = re.fullmatch(
            r"---\s*\n.*?\n---\s*\n?(.*)", text, re.DOTALL
        )
        body = (frontmatter.group(1) if frontmatter else text).strip()
    if not body:
        raise ValueError("story body is empty")

    explicit_title = payload.get("title") or payload.get("topic")
    if isinstance(explicit_title, str) and explicit_title.strip():
        title = explicit_title.strip()
    else:
        heading = re.match(r"^#\s+(.+)$", body, re.MULTILINE)
        title = heading.group(1).strip() if heading else "Nieuwe publicatie"
    body = re.sub(r"^#\s+.+\n+", "", body, count=1).strip()
    return title, body


def normalize_tags(raw: Any) -> list[str]:
    """Normalize, deduplicate, and bound Medium publication tags."""
    values: list[str] = []
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        values = [part for item in raw for part in item.split(",")]
    elif raw is not None:
        raise ValueError("tags must be a string or list of strings")
    tags = []
    for value in values:
        tag = " ".join(value.split()).strip()
        if tag and tag.casefold() not in {item.casefold() for item in tags}:
            tags.append(tag)
    if len(tags) > 5:
        raise ValueError("Medium supports at most five tags")
    return tags


def image_manifest(payload: dict[str, Any], strict: bool) -> dict[str, Any] | None:
    """Validate an optional JPG/PNG header image."""
    raw = payload.get("image_path")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("image_path must be a non-empty path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise ValueError("Medium header image must be JPG or PNG")
    exists = path.is_file()
    if strict and not exists:
        raise FileNotFoundError(f"header image missing: {path}")
    return {
        "path": str(path.resolve()) if exists else str(path.resolve(strict=False)),
        "exists": exists,
    }


def validated_auth(path: Path) -> Path | None:
    """Validate Playwright storage-state or select safe mock mode."""
    auth = path.expanduser().resolve()
    if not auth.is_file():
        LOGGER.warning(
            "SAFE MOCK: auth state missing (%s); Medium is not contacted", auth
        )
        return None
    mode = stat.S_IMODE(auth.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"auth state must be mode 600, found {mode:o}: {auth}")
    state = json.loads(auth.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or "cookies" not in state:
        raise ValueError("auth file is not a Playwright storage-state document")
    return auth


def boolean_env(name: str, default: bool) -> bool:
    """Parse a strict boolean environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be boolean")


def first_visible(page: Any, selectors: list[str]) -> Any:
    """Return the first visible selector from stable semantic alternatives."""
    try:
        return find_with_accessible_fallbacks(page, selectors, timeout=30000)
    except LookupError as exc:
        raise MediumPublisherError("expected Medium control not found") from exc


def screenshot(page: Any, event_id: int, label: str) -> Path | None:
    """Capture a timestamped full-page diagnostic screenshot."""
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = SCREENSHOT_DIR / (
            f"medium_{event_id}_{label}_{stamp}_{uuid4().hex[:6]}.png"
        )
        artifacts = capture_sanitized_diagnostic(
            page, SCREENSHOT_DIR, "medium", f"{event_id}_{label}"
        )
        return Path(artifacts["screenshot"])
    except Exception:
        LOGGER.exception("Unable to save Medium screenshot")
        return None


def publish(
    payload: dict[str, Any], event_id: int, dry_run: bool, auth: Path, headless: bool
) -> dict[str, Any]:
    """Compose a Medium story and optionally publish it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise MediumPublisherError("Playwright is not installed") from exc

    title, body = read_story(payload)
    tags = normalize_tags(payload.get("tags"))
    image = image_manifest(payload, strict=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(auth))
        page = context.new_page()
        page.set_default_timeout(int(os.getenv("MEDIUM_TIMEOUT_MS", "30000")))
        try:
            page.goto("https://medium.com/new-story", wait_until="domcontentloaded")
            if "/m/signin" in page.url or "/login" in page.url:
                raise MediumPublisherError("stored Medium session has expired")
            first_visible(
                page,
                [
                    "h3[contenteditable=true][data-testid*=title]",
                    "div[contenteditable=true][data-placeholder*='Title' i]",
                    "h3[contenteditable=true]",
                ],
            ).fill(title)
            first_visible(
                page,
                [
                    "article div[contenteditable=true]",
                    "div[contenteditable=true][data-placeholder*='story' i]",
                    "div[contenteditable=true][role=textbox]",
                ],
            ).fill(body)
            if image is not None:
                upload = page.locator("input[type=file][accept*='image'], input[type=file]").last
                if not upload.count():
                    raise MediumPublisherError("header-image file input not found")
                upload.set_input_files(image["path"])

            if dry_run:
                artifact = screenshot(page, event_id, "dry_run")
                LOGGER.info("Dry-run complete; Medium Publish was not clicked")
                return {
                    "published": False,
                    "dry_run": True,
                    "title": title,
                    "tags": tags,
                    "image": image,
                    "screenshot": str(artifact) if artifact else None,
                }

            first_visible(
                page,
                ["button:has-text('Publish')", "button[data-action='show-prepublish']"],
            ).click()
            if tags:
                tag_input = first_visible(
                    page,
                    ["input[placeholder*='tag' i]", "input[data-testid*=tag]"],
                )
                for tag in tags:
                    tag_input.fill(tag)
                    tag_input.press("Enter")
            final_button = first_visible(
                page,
                ["button:has-text('Publish now')", "button:has-text('Publish')"],
            )
            if not final_button.is_enabled():
                raise MediumPublisherError("final Publish button is disabled")
            before_url = str(page.url)
            final_button.click()
            confirmed_url = verify_submission(
                page, before_url,
                indicators=("[role='alert']", "[data-testid*='publishSuccess']"),
            )
            LOGGER.info("Medium story submitted")
            return {
                "published": True,
                "dry_run": False,
                "title": title,
                "tags": tags,
                "image": image,
                "platform_url": confirmed_url,
            }
        except Exception as exc:
            artifact = screenshot(page, event_id, "error")
            raise MediumPublisherError(
                f"browser automation failed; screenshot={artifact}: {exc}"
            ) from exc
        finally:
            context.close()
            browser.close()


def process_event(
    connection: sqlite3.Connection,
    event_id: int,
    payload: dict[str, Any],
    dry_run: bool,
    auth_path: Path,
    headless: bool,
) -> dict[str, Any]:
    """Execute real publication or explicit non-network mock completion."""
    title, body = read_story(payload)
    tags = normalize_tags(payload.get("tags"))
    auth = validated_auth(auth_path)
    if auth is None:
        result: dict[str, Any] = {
            "status": "AUTH_REQUIRED",
            "mock": True,
            "medium_contacted": False,
            "published": False,
            "dry_run": dry_run,
            "title": title,
            "content_length": len(body),
            "tags": tags,
            "image": image_manifest(payload, strict=False),
        }
    else:
        result = publish(payload, event_id, dry_run, auth, headless)
        result["mock"] = False
        result["medium_contacted"] = True
    return result


def main() -> int:
    """Register and/or process Medium content."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    database = args.db.expanduser().resolve()
    payload: dict[str, Any] = {}
    ledger_started = False
    try:
        with connect(database) as connection:
            if args.register:
                register_plugin(connection)
            should_execute = args.event_id is not None or args.text is not None
            if should_execute:
                if args.event_id is not None:
                    payload = load_payload(connection, args.event_id)
                apply_cli_overrides(args, payload)
                headless = (
                    args.headless
                    if args.headless is not None
                    else boolean_env("MEDIUM_HEADLESS", True)
                )
                auth_available = validated_auth(args.auth) is not None
                if args.event_id is not None and auth_available and not args.dry_run:
                    ledger = begin_submission(
                        connection, event_id=args.event_id, channel="MEDIUM",
                        payload=payload, target="medium-story",
                    )
                    if ledger["status"] == "CONFIRMED":
                        emit_result("COMPLETED", result={"idempotent_replay": True, "platform_url": ledger["platform_url"]})
                        return 0
                    ledger_started = True
                try:
                    result = process_event(
                        connection, args.event_id or 0, payload, args.dry_run,
                        args.auth, headless,
                    )
                except Exception as exc:
                    if ledger_started and args.event_id is not None:
                        update_publication(connection, args.event_id, "MEDIUM", "UNKNOWN", detail=f"{type(exc).__name__}: {exc}")
                    raise
                if ledger_started and args.event_id is not None:
                    update_publication(
                        connection, args.event_id, "MEDIUM", "CONFIRMED",
                        platform_id=result.get("platform_id"), platform_url=result.get("platform_url"),
                    )
                LOGGER.info("Operation result: %s", json.dumps(result, ensure_ascii=False))
                if args.event_id is not None:
                    outcome = "BLOCKED_AUTH" if result.get("status") == "AUTH_REQUIRED" else "SIMULATED" if result.get("dry_run") else "COMPLETED"
                    emit_result(outcome, result=result, payload_patch={"medium": result})
    except Exception as exc:
        if args.event_id is not None:
            emit_result("UNKNOWN" if ledger_started else "FAILED", error=f"{type(exc).__name__}: {exc}", retryable=False)
        LOGGER.exception("Medium publisher failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
