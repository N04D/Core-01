#!/usr/bin/env python3
"""Production LinkedIn channel publisher driven by SQLite queue events."""

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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.event_protocol import begin_submission, emit_result, update_publication


PLUGIN_NAME: Final = "LinkedIn Publisher (Productie)"
PLUGIN_TYPE: Final = "channel"
PLUGIN_ICON: Final = "🔗"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE: Final = Path("../db/events.db")
SCREENSHOT_DIRECTORY: Final = PROJECT_ROOT / "vault" / "logs" / "screenshots"
LINKEDIN_FEED_URL: Final = "https://www.linkedin.com/feed/"
FRONTMATTER_PATTERN: Final = re.compile(
    r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n?(?P<body>.*)\Z",
    re.DOTALL,
)
LOGGER: Final = logging.getLogger("pub_linkedin")


class LinkedInPublisherError(RuntimeError):
    """Raised for actionable LinkedIn publishing failures."""


def parse_args() -> argparse.Namespace:
    """Parse registration and publication options."""
    parser = argparse.ArgumentParser(
        description="Register or execute the production LinkedIn publisher."
    )
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose the post and save a screenshot without clicking Publish.",
    )
    parser.add_argument(
        "--mock-auth-fallback",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if not args.register and args.event_id is None:
        parser.error("either --register or --event_id is required")
    if args.event_id is not None and args.event_id < 1:
        parser.error("--event_id must be a positive integer")
    return args


def connect(database: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    connection = sqlite3.connect(database, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def register_plugin(connection: sqlite3.Connection) -> None:
    """Idempotently register this executable in the plugin registry."""
    with connection:
        connection.execute(
            """
            INSERT INTO plugin_registry
                (plugin_name, type, executable_path, icon, is_active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(plugin_name) DO UPDATE SET
                type = excluded.type,
                executable_path = excluded.executable_path,
                icon = excluded.icon,
                is_active = 1
            """,
            (PLUGIN_NAME, PLUGIN_TYPE, str(Path(__file__).resolve()), PLUGIN_ICON),
        )
    LOGGER.info("Plugin registered: %s", PLUGIN_NAME)


def load_payload(connection: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    """Load and validate an event payload."""
    row = connection.execute(
        "SELECT payload FROM events_queue WHERE id = ?", (event_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"event {event_id} does not exist")
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    return payload


def extract_content(payload: dict[str, Any]) -> str:
    """Resolve inline content or read a Markdown draft from disk."""
    inline = payload.get("content")
    if inline is not None:
        if not isinstance(inline, str) or not inline.strip():
            raise ValueError("payload 'content' must be a non-empty string")
        return inline.strip()

    raw_path = payload.get("draft_file", payload.get("filepath"))
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("payload must contain 'content', 'draft_file', or 'filepath'")
    draft_path = Path(raw_path).expanduser()
    if not draft_path.is_absolute():
        draft_path = PROJECT_ROOT / draft_path
    draft_path = draft_path.resolve(strict=True)
    if not draft_path.is_file():
        raise ValueError(f"draft path is not a file: {draft_path}")
    text = draft_path.read_text(encoding="utf-8")
    frontmatter = FRONTMATTER_PATTERN.fullmatch(text)
    content = frontmatter.group("body") if frontmatter else text
    if not content.strip():
        raise ValueError(f"draft contains no publishable content: {draft_path}")
    return content.strip()


def parse_boolean_environment(name: str, default: bool) -> bool:
    """Parse a strict boolean environment variable."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def resolve_auth_file() -> Path:
    """Locate and permission-check a Playwright storage-state file."""
    configured = os.getenv("LINKEDIN_AUTH_PATH")
    candidates = (
        [Path(configured).expanduser()]
        if configured
        else [
            PROJECT_ROOT / "config" / "linkedin_auth.json",
            PROJECT_ROOT / "linkedin_auth.json",
            PROJECT_ROOT / "auth.json",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            auth_path = candidate.resolve()
            mode = stat.S_IMODE(auth_path.stat().st_mode)
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise PermissionError(
                    f"auth file permissions are too broad ({mode:o}); run: "
                    f"chmod 600 {auth_path}"
                )
            try:
                state = json.loads(auth_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"auth file is not valid JSON: {auth_path}") from exc
            if not isinstance(state, dict) or not any(
                key in state for key in ("cookies", "origins")
            ):
                raise ValueError(
                    "auth file is not a Playwright storage-state document"
                )
            return auth_path
    raise FileNotFoundError(
        "LinkedIn auth state not found; expected config/linkedin_auth.json"
    )


def first_visible(page: Any, selectors: list[str], timeout_ms: int) -> Any:
    """Return the first visible locator from a list of resilient selectors."""
    deadline = timeout_ms / max(1, len(selectors))
    errors: list[str] = []
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=deadline)
            return locator
        except Exception as exc:  # Playwright errors are normalized at this boundary.
            errors.append(f"{selector}: {exc}")
    raise LinkedInPublisherError(
        "none of the expected LinkedIn selectors became visible: "
        + " | ".join(errors)
    )


def screenshot(page: Any, event_id: int, label: str) -> Path | None:
    """Capture a diagnostic screenshot without masking the original error."""
    try:
        SCREENSHOT_DIRECTORY.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = SCREENSHOT_DIRECTORY / f"linkedin_{event_id}_{label}_{timestamp}.png"
        page.screenshot(path=str(path), full_page=True)
        return path
    except Exception:
        LOGGER.exception("Could not capture LinkedIn diagnostic screenshot")
        return None


def publish(
    content: str, event_id: int, dry_run: bool, mock_auth_fallback: bool = False
) -> Path | None:
    """Compose and optionally publish a LinkedIn post using saved auth state."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LinkedInPublisherError(
            "Playwright is not installed; run 'pip install playwright' and "
            "'playwright install chromium'"
        ) from exc

    auth_file: Path | None
    try:
        auth_file = resolve_auth_file()
    except FileNotFoundError:
        if not (dry_run and mock_auth_fallback):
            raise
        auth_file = None
        LOGGER.warning(
            "SAFE MOCK FALLBACK: auth state missing; LinkedIn will not be contacted"
        )
    headless = parse_boolean_environment("LINKEDIN_HEADLESS", True)
    timeout_ms = int(os.getenv("LINKEDIN_TIMEOUT_MS", "30000"))
    if timeout_ms < 1000:
        raise ValueError("LINKEDIN_TIMEOUT_MS must be at least 1000")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = (
            browser.new_context(storage_state=str(auth_file))
            if auth_file is not None
            else browser.new_context()
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            if auth_file is None:
                page.set_content(
                    """
                    <!doctype html><html><body>
                    <main style="max-width:680px;margin:60px auto;font-family:sans-serif">
                      <h1>LinkedIn Publisher — veilige dry-run mock</h1>
                      <button class="share-box-feed-entry__trigger"
                        onclick="document.querySelector('[role=dialog]').hidden=false">
                        Start a post
                      </button>
                      <div role="dialog" hidden style="margin-top:24px;padding:24px;border:1px solid #999">
                        <div role="textbox" contenteditable="true"
                          style="min-height:180px;white-space:pre-wrap"></div>
                        <button class="share-actions__primary-action">Post</button>
                      </div>
                    </main></body></html>
                    """
                )
            else:
                page.goto(
                    LINKEDIN_FEED_URL,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                if "/login" in page.url or "/checkpoint/" in page.url:
                    raise LinkedInPublisherError(
                        "saved LinkedIn session has expired or requires verification"
                    )

            start_button = first_visible(
                page,
                [
                    "button:has-text('Start a post')",
                    "button:has-text('Een bijdrage beginnen')",
                    "button:has-text('Begin a post')",
                    "button.share-box-feed-entry__trigger",
                ],
                timeout_ms,
            )
            start_button.click()

            editor = first_visible(
                page,
                [
                    "div[role='dialog'] div[contenteditable='true'][role='textbox']",
                    "div[role='dialog'] div.ql-editor[contenteditable='true']",
                    "div[contenteditable='true'][data-placeholder]",
                ],
                timeout_ms,
            )
            editor.click()
            editor.fill(content)

            if dry_run:
                path = screenshot(page, event_id, "dry_run")
                LOGGER.info("Dry-run completed; LinkedIn Publish was not clicked")
                return path

            publish_button = first_visible(
                page,
                [
                    "div[role='dialog'] button:has-text('Post')",
                    "div[role='dialog'] button:has-text('Plaatsen')",
                    "div[role='dialog'] button:has-text('Publiceren')",
                    "button.share-actions__primary-action",
                ],
                timeout_ms,
            )
            if not publish_button.is_enabled():
                raise LinkedInPublisherError("LinkedIn Publish button is disabled")
            publish_button.click()
            page.locator("div[role='dialog']").wait_for(
                state="hidden", timeout=timeout_ms
            )
            LOGGER.info("LinkedIn post published successfully")
            return None
        except PlaywrightTimeoutError as exc:
            path = screenshot(page, event_id, "timeout")
            raise LinkedInPublisherError(
                f"LinkedIn timed out; screenshot={path or 'unavailable'}"
            ) from exc
        except Exception as exc:
            path = screenshot(page, event_id, "error")
            if isinstance(exc, LinkedInPublisherError):
                raise
            raise LinkedInPublisherError(
                f"LinkedIn browser automation failed; screenshot={path or 'unavailable'}: {exc}"
            ) from exc
        finally:
            context.close()
            browser.close()


def process_event(
    connection: sqlite3.Connection,
    event_id: int,
    dry_run: bool,
    mock_auth_fallback: bool = False,
) -> dict[str, Any]:
    """Publish one event and return result metadata to the worker."""
    payload = load_payload(connection, event_id)
    content = extract_content(payload)
    effective_dry_run = dry_run or payload.get("dry_run") is True
    effective_mock_fallback = (
        mock_auth_fallback or payload.get("mock_auth_fallback") is True
    )
    artifact = publish(
        content, event_id, effective_dry_run, effective_mock_fallback
    )
    payload["linkedin_dry_run"] = effective_dry_run
    payload["linkedin_published"] = not effective_dry_run
    if artifact is not None:
        payload["linkedin_screenshot"] = str(artifact)
    return {
        "dry_run": effective_dry_run,
        "published": not effective_dry_run,
        "screenshot": str(artifact) if artifact else None,
    }


def main() -> int:
    """Register the publisher and/or process one queue event."""
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
                    if not effective_dry_run:
                        try:
                            resolve_auth_file()
                        except FileNotFoundError as exc:
                            emit_result("BLOCKED_AUTH", result={"status": "AUTH_REQUIRED", "published": False}, error=str(exc))
                            return 0
                        ledger = begin_submission(
                            connection, event_id=args.event_id, channel="LINKEDIN",
                            payload=payload, target="personal-profile",
                        )
                        if ledger["status"] == "CONFIRMED":
                            emit_result("COMPLETED", result={"idempotent_replay": True, "platform_url": ledger["platform_url"]})
                            return 0
                        ledger_started = True
                    result = process_event(
                        connection,
                        args.event_id,
                        args.dry_run,
                        args.mock_auth_fallback,
                    )
                    if ledger_started:
                        update_publication(connection, args.event_id, "LINKEDIN", "CONFIRMED")
                    emit_result("SIMULATED" if effective_dry_run else "COMPLETED", result=result, payload_patch={"linkedin": result})
                except Exception as exc:
                    if ledger_started:
                        update_publication(connection, args.event_id, "LINKEDIN", "UNKNOWN", detail=f"{type(exc).__name__}: {exc}")
                    emit_result("UNKNOWN" if ledger_started else "FAILED", error=f"{type(exc).__name__}: {exc}", retryable=False)
                    raise
                LOGGER.info(
                    "Event %s completed (dry_run=%s)", args.event_id, args.dry_run
                )
    except Exception:
        LOGGER.exception("LinkedIn publisher failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
