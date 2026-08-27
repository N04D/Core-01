#!/usr/bin/env python3
"""Substack channel publisher with Playwright and a safe auth-less fallback."""

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
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.event_protocol import begin_submission, emit_result, update_publication


PLUGIN_NAME: Final = "Substack Publisher"
PLUGIN_TYPE: Final = "channel"
PLUGIN_ICON: Final = "📰"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE: Final = Path("../db/events.db")
DEFAULT_AUTH: Final = PROJECT_ROOT / "config" / "substack_auth.json"
SCREENSHOT_DIR: Final = PROJECT_ROOT / "vault" / "logs" / "screenshots"
FRONTMATTER_PATTERN: Final = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*\r?\n?(?P<body>.*)\Z",
    re.DOTALL,
)
LOGGER: Final = logging.getLogger("pub_substack")


class SubstackPublisherError(RuntimeError):
    """Raised when Substack publication cannot safely complete."""


def parse_args() -> argparse.Namespace:
    """Parse event-bus and publication arguments."""
    parser = argparse.ArgumentParser(
        description="Register or execute the Substack publisher."
    )
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose and save a screenshot without publishing.",
    )
    args = parser.parse_args()
    if not args.register and args.event_id is None:
        parser.error("either --register or --event_id is required")
    if args.event_id is not None and args.event_id < 1:
        parser.error("--event_id must be positive")
    return args


def connect(database: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    connection = sqlite3.connect(database, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def register_plugin(connection: sqlite3.Connection) -> None:
    """Idempotently register this plugin."""
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
    LOGGER.info("Plugin registered: %s", PLUGIN_NAME)


def load_payload(connection: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    """Read and validate a JSON event payload."""
    row = connection.execute(
        "SELECT payload FROM events_queue WHERE id=?", (event_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"event {event_id} does not exist")
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    return payload


def read_content(payload: dict[str, Any]) -> tuple[str, str]:
    """Resolve title and Markdown body from inline data or a draft file."""
    title = payload.get("title") or payload.get("topic") or "Nieuwe publicatie"
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title/topic must be a non-empty string")

    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        return title.strip(), content.strip()

    raw_path = payload.get("draft_file", payload.get("filepath"))
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("payload requires content, draft_file, or filepath")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    text = path.resolve(strict=True).read_text(encoding="utf-8")
    match = FRONTMATTER_PATTERN.fullmatch(text)
    body = match.group("body") if match else text
    if not body.strip():
        raise ValueError("draft contains no publishable content")
    return title.strip(), body.strip()


def resolve_auth() -> Path | None:
    """Return secure storage-state or select the non-network mock fallback."""
    auth = Path(os.getenv("SUBSTACK_AUTH_PATH", str(DEFAULT_AUTH))).expanduser().resolve()
    if not auth.is_file():
        LOGGER.warning(
            "SAFE MOCK: auth state missing (%s); Substack is not contacted", auth
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


def editor_url(payload: dict[str, Any]) -> str:
    """Resolve and validate the publication-specific Substack editor URL."""
    explicit = payload.get("editor_url") or os.getenv("SUBSTACK_EDITOR_URL")
    if explicit:
        url = str(explicit)
    else:
        publication = payload.get("publication_url") or os.getenv(
            "SUBSTACK_PUBLICATION_URL"
        )
        if not publication:
            raise ValueError(
                "set payload publication_url/editor_url or SUBSTACK_PUBLICATION_URL"
            )
        url = str(publication).rstrip("/") + "/publish/post"
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or not (
        parsed.hostname == "substack.com" or parsed.hostname.endswith(".substack.com")
    ):
        raise ValueError("Substack editor URL must be HTTPS on substack.com")
    return url


def first_visible(page: Any, selectors: list[str], timeout_ms: int) -> Any:
    """Find the first visible control from resilient selector alternatives."""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(
                state="visible", timeout=max(1000, timeout_ms // len(selectors))
            )
            return locator
        except Exception:
            continue
    raise SubstackPublisherError("no expected selector visible: " + " | ".join(selectors))


def screenshot(page: Any, event_id: int, label: str) -> Path | None:
    """Capture a full-page diagnostic screenshot."""
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = SCREENSHOT_DIR / (
            f"substack_{event_id}_{label}_{stamp}_{uuid4().hex[:6]}.png"
        )
        page.screenshot(path=str(path), full_page=True)
        return path
    except Exception:
        LOGGER.exception("Could not save Substack screenshot")
        return None


def publish(
    payload: dict[str, Any], event_id: int, dry_run: bool, auth: Path
) -> dict[str, Any]:
    """Fill the Substack editor and optionally click its publish controls."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SubstackPublisherError(
            "Playwright missing; install it and run 'playwright install chromium'"
        ) from exc

    title, content = read_content(payload)
    timeout_ms = int(os.getenv("SUBSTACK_TIMEOUT_MS", "30000"))
    headless = boolean_env("SUBSTACK_HEADLESS", True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(auth))
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            page.goto(editor_url(payload), wait_until="domcontentloaded")
            if "/sign-in" in page.url or "/login" in page.url:
                raise SubstackPublisherError("stored Substack session has expired")

            first_visible(
                page,
                [
                    "textarea[placeholder*='title' i]",
                    "input[placeholder*='title' i]",
                    "textarea[name='title']",
                ],
                timeout_ms,
            ).fill(title)
            first_visible(
                page,
                [
                    "div[contenteditable='true'].ProseMirror",
                    "div[contenteditable='true'][role='textbox']",
                    "div[contenteditable='true']",
                ],
                timeout_ms,
            ).fill(content)

            if dry_run:
                artifact = screenshot(page, event_id, "dry_run")
                LOGGER.info("Dry-run complete; no Substack publish control was clicked")
                return {
                    "mock": False,
                    "dry_run": True,
                    "published": False,
                    "screenshot": str(artifact) if artifact else None,
                }

            continue_button = first_visible(
                page,
                [
                    "button:has-text('Continue')",
                    "button:has-text('Doorgaan')",
                    "button:has-text('Publish')",
                ],
                timeout_ms,
            )
            continue_button.click()
            publish_button = first_visible(
                page,
                [
                    "button:has-text('Send to everyone now')",
                    "button:has-text('Publish now')",
                    "button:has-text('Publish')",
                ],
                timeout_ms,
            )
            if not publish_button.is_enabled():
                raise SubstackPublisherError("Substack publish button is disabled")
            publish_button.click()
            LOGGER.info("Substack publication submitted")
            return {"mock": False, "dry_run": False, "published": True}
        except Exception as exc:
            artifact = screenshot(page, event_id, "error")
            raise SubstackPublisherError(
                f"browser automation failed; screenshot={artifact}: {exc}"
            ) from exc
        finally:
            context.close()
            browser.close()


def process_event(
    connection: sqlite3.Connection, event_id: int, dry_run: bool
) -> dict[str, Any]:
    """Execute a real publication or an explicit safe mock completion."""
    payload = load_payload(connection, event_id)
    title, content = read_content(payload)
    auth = resolve_auth()
    if auth is None:
        result: dict[str, Any] = {
            "status": "AUTH_REQUIRED",
            "mock": True,
            "substack_contacted": False,
            "published": False,
            "dry_run": dry_run,
            "title": title,
            "content_length": len(content),
        }
    else:
        result = publish(payload, event_id, dry_run, auth)
    return result


def main() -> int:
    """Register and/or process one Substack queue event."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    database = args.db.expanduser().resolve()
    ledger_started = False
    try:
        with connect(database) as connection:
            if args.register:
                register_plugin(connection)
            if args.event_id is not None:
                payload = load_payload(connection, args.event_id)
                effective_dry_run = args.dry_run or payload.get("dry_run") is True
                try:
                    auth = resolve_auth()
                    if auth is None and not effective_dry_run:
                        emit_result("BLOCKED_AUTH", result={"status": "AUTH_REQUIRED", "published": False})
                        return 0
                    if auth is not None and not effective_dry_run:
                        ledger = begin_submission(
                            connection, event_id=args.event_id, channel="SUBSTACK",
                            payload=payload, target=str(payload.get("publication_url") or "substack-publication"),
                        )
                        if ledger["status"] == "CONFIRMED":
                            emit_result("COMPLETED", result={"idempotent_replay": True, "platform_url": ledger["platform_url"]})
                            return 0
                        ledger_started = True
                    result = process_event(connection, args.event_id, args.dry_run)
                    if ledger_started:
                        update_publication(connection, args.event_id, "SUBSTACK", "CONFIRMED")
                    outcome = "BLOCKED_AUTH" if result.get("status") == "AUTH_REQUIRED" else "SIMULATED" if result.get("dry_run") else "COMPLETED"
                    emit_result(outcome, result=result, payload_patch={"substack": result})
                except Exception as exc:
                    if ledger_started:
                        update_publication(connection, args.event_id, "SUBSTACK", "UNKNOWN", detail=f"{type(exc).__name__}: {exc}")
                    emit_result("UNKNOWN" if ledger_started else "FAILED", error=f"{type(exc).__name__}: {exc}", retryable=False)
                    raise
                LOGGER.info("Event %s completed: %s", args.event_id, json.dumps(result))
    except Exception:
        LOGGER.exception("Substack publisher failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
