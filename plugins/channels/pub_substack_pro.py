#!/usr/bin/env python3
"""Substack Pro multimodal publisher, analytics, comments, and profile adapter."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterator
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.event_protocol import begin_submission, emit_result, update_publication
from core.database import connect_database
from core.browser_robustness import (
    capture_sanitized_diagnostic, find_with_accessible_fallbacks,
    verify_submission,
)


PLUGIN_NAME: Final = "Substack Pro Publisher & Analytics"
PLUGIN_TYPE: Final = "channel"
PLUGIN_ICON: Final = "🗞️"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE: Final = Path("../db/events.db")
DEFAULT_AUTH: Final = PROJECT_ROOT / "config" / "substack_auth.json"
ANALYTICS_DIR: Final = PROJECT_ROOT / "vault" / "analytics"
RESEARCH_DIR: Final = PROJECT_ROOT / "vault" / "research"
SCREENSHOT_DIR: Final = PROJECT_ROOT / "vault" / "logs" / "screenshots"
LOGGER: Final = logging.getLogger("pub_substack_pro")


class SubstackProError(RuntimeError):
    """Raised when a Substack Pro operation cannot safely complete."""


def parse_args() -> argparse.Namespace:
    """Parse event-bus, content, media, and operation arguments."""
    parser = argparse.ArgumentParser(description="Substack Pro multimodal adapter.")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--post-article", action="store_true")
    modes.add_argument("--post-note", action="store_true")
    modes.add_argument("--analytics", action="store_true")
    modes.add_argument("--read-comments", action="store_true")
    modes.add_argument("--fetch-profile", action="store_true")
    parser.add_argument("--text", help="Inline body/note text; overrides payload content.")
    parser.add_argument("--title", help="Article title; overrides payload title.")
    parser.add_argument("--subheader", help="Optional article subtitle/subheader.")
    parser.add_argument("--image-path", type=Path, help="JPG/PNG attachment.")
    parser.add_argument("--video-path", type=Path, help="Video attachment.")
    parser.add_argument("--publication-url", help="HTTPS Substack publication URL.")
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--headless", action=argparse.BooleanOptionalAction, default=None
    )
    args = parser.parse_args()
    if not any(
        (
            args.register,
            args.event_id is not None,
            args.post_article,
            args.post_note,
            args.analytics,
            args.read_comments,
            args.fetch_profile,
        )
    ):
        parser.error("choose an operation, --event_id, or --register")
    if args.event_id is not None and args.event_id < 1:
        parser.error("--event_id must be positive")
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    return args


def connect(database: Path) -> sqlite3.Connection:
    return connect_database(database)


def register_plugin(connection: sqlite3.Connection) -> None:
    """Idempotently register the Substack Pro plugin."""
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
    LOGGER.info("Plugin registered: %s", PLUGIN_NAME)


def load_payload(connection: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT payload FROM events_queue WHERE id=?", (event_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"event {event_id} does not exist")
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    return payload


def operation(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    """Resolve CLI mode first, then event payload mode, then article default."""
    flags = {
        "post_article": args.post_article,
        "post_note": args.post_note,
        "analytics": args.analytics,
        "read_comments": args.read_comments,
        "fetch_profile": args.fetch_profile,
    }
    selected = next((name for name, enabled in flags.items() if enabled), None)
    raw = selected or payload.get("operation", "post_article")
    if raw not in flags:
        raise ValueError(f"unsupported operation: {raw}")
    return str(raw)


def apply_cli_payload(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    """Apply explicit CLI values as payload overrides."""
    mapping = {
        "content": args.text,
        "title": args.title,
        "subheader": args.subheader,
        "image_path": str(args.image_path) if args.image_path else None,
        "video_path": str(args.video_path) if args.video_path else None,
        "publication_url": args.publication_url,
    }
    payload.update({key: value for key, value in mapping.items() if value is not None})


def content(payload: dict[str, Any]) -> str:
    """Resolve inline text or a Markdown draft body."""
    inline = payload.get("content")
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    raw_path = payload.get("draft_file", payload.get("filepath"))
    if isinstance(raw_path, str) and raw_path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        text = path.resolve(strict=True).read_text(encoding="utf-8")
        match = re.fullmatch(r"---\s*\n.*?\n---\s*\n?(.*)", text, re.DOTALL)
        return (match.group(1) if match else text).strip()
    return ""


def media_manifest(payload: dict[str, Any], strict: bool) -> list[dict[str, Any]]:
    """Normalize supported image/video attachments."""
    manifest = []
    rules = {
        "image_path": ("image", {".jpg", ".jpeg", ".png"}),
        "video_path": ("video", {".mp4", ".mov", ".m4v", ".webm"}),
    }
    for field, (kind, extensions) in rules.items():
        raw = payload.get(field)
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{field} must be a non-empty path")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if path.suffix.lower() not in extensions:
            raise ValueError(f"unsupported {field} extension: {path.suffix}")
        exists = path.is_file()
        if strict and not exists:
            raise FileNotFoundError(f"media file missing: {path}")
        manifest.append(
            {
                "kind": kind,
                "path": str(path.resolve()) if exists else str(path.resolve(strict=False)),
                "exists": exists,
            }
        )
    return manifest


def validated_auth(path: Path) -> Path | None:
    auth = path.expanduser().resolve()
    if not auth.is_file():
        LOGGER.warning(
            "SAFE MOCK: auth state missing (%s); Substack is not contacted", auth
        )
        return None
    mode = stat.S_IMODE(auth.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"auth state must be mode 600, found {mode:o}")
    state = json.loads(auth.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or "cookies" not in state:
        raise ValueError("invalid Playwright storage-state")
    return auth


def publication_url(payload: dict[str, Any]) -> str:
    raw = payload.get("publication_url") or os.getenv("SUBSTACK_PUBLICATION_URL")
    if not isinstance(raw, str) or not raw:
        raise ValueError("publication_url or SUBSTACK_PUBLICATION_URL is required")
    url = raw.rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or not (
        parsed.hostname == "substack.com" or parsed.hostname.endswith(".substack.com")
    ):
        raise ValueError("publication URL must be HTTPS on substack.com")
    return url


def boolean_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be boolean")


@contextmanager
def browser_page(auth: Path, headless: bool) -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SubstackProError("Playwright is not installed") from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(auth))
        page = context.new_page()
        page.set_default_timeout(int(os.getenv("SUBSTACK_TIMEOUT_MS", "30000")))
        try:
            yield page
        finally:
            context.close()
            browser.close()


def first_visible(page: Any, selectors: list[str]) -> Any:
    try:
        return find_with_accessible_fallbacks(page, selectors, timeout=30000)
    except LookupError as exc:
        raise SubstackProError("expected Substack control not found") from exc


def screenshot(page: Any, mode: str) -> Path | None:
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = SCREENSHOT_DIR / f"substack_pro_{mode}_{stamp}_{uuid4().hex[:6]}.png"
        artifacts = capture_sanitized_diagnostic(
            page, SCREENSHOT_DIR, "substack_pro", mode
        )
        return Path(artifacts["screenshot"])
    except Exception:
        LOGGER.exception("Unable to save screenshot")
        return None


def upload_media(page: Any, media: list[dict[str, Any]]) -> None:
    """Upload image/video assets through editor file inputs."""
    for item in media:
        selectors = (
            ["input[type=file][accept*='image']", "input[type=file]"]
            if item["kind"] == "image"
            else ["input[type=file][accept*='video']", "input[type=file]"]
        )
        for selector in selectors:
            locator = page.locator(selector).last
            try:
                if locator.count():
                    locator.set_input_files(item["path"])
                    break
            except Exception:
                continue
        else:
            raise SubstackProError(f"no upload input found for {item['kind']}")


def post_article(page: Any, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    body = content(payload)
    title = str(payload.get("title") or payload.get("topic") or "Nieuwe publicatie")
    if not body:
        raise ValueError("article body is empty")
    media = media_manifest(payload, strict=True)
    page.goto(publication_url(payload) + "/publish/post", wait_until="domcontentloaded")
    first_visible(page, ["textarea[placeholder*='title' i]", "input[placeholder*='title' i]"]).fill(title)
    subheader = payload.get("subheader")
    if isinstance(subheader, str) and subheader:
        first_visible(page, ["textarea[placeholder*='subtitle' i]", "input[placeholder*='subtitle' i]"]).fill(subheader)
    first_visible(page, ["div.ProseMirror[contenteditable=true]", "div[contenteditable=true][role=textbox]"]).fill(body)
    upload_media(page, media)
    return finish_publication(page, "article", dry_run, media)


def post_note(page: Any, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    text = content(payload)
    media = media_manifest(payload, strict=True)
    if not text and not media:
        raise ValueError("note needs text or media")
    page.goto("https://substack.com/notes", wait_until="domcontentloaded")
    first_visible(page, ["button:has-text('New note')", "button:has-text('Create note')"]).click()
    first_visible(page, ["div[contenteditable=true][role=textbox]", "textarea[placeholder*='note' i]"]).fill(text)
    upload_media(page, media)
    return finish_publication(page, "note", dry_run, media)


def finish_publication(
    page: Any, mode: str, dry_run: bool, media: list[dict[str, Any]]
) -> dict[str, Any]:
    if dry_run:
        artifact = screenshot(page, mode + "_dry_run")
        return {"published": False, "dry_run": True, "media": media, "screenshot": str(artifact) if artifact else None}
    button = first_visible(
        page,
        ["button:has-text('Publish')", "button:has-text('Post')", "button:has-text('Send to everyone')"],
    )
    if not button.is_enabled():
        raise SubstackProError("publication control is disabled")
    before_url = str(page.url)
    button.click()
    confirmed_url = verify_submission(
        page, before_url,
        indicators=("[role='status']", "[data-testid*='success']"),
    )
    return {"published": True, "dry_run": False, "media": media, "platform_url": confirmed_url}


def text_or_empty(locator: Any) -> str:
    try:
        return locator.first.inner_text(timeout=1200).strip()
    except Exception:
        return ""


def scrape_analytics(page: Any, payload: dict[str, Any], limit: int) -> dict[str, Any]:
    page.goto(publication_url(payload) + "/publish/stats", wait_until="domcontentloaded")
    rows = page.locator("table tbody tr")
    posts = []
    for index in range(min(rows.count(), limit)):
        row = rows.nth(index)
        cells = row.locator("td")
        posts.append(
            {
                "title": text_or_empty(cells.nth(0)),
                "views": text_or_empty(cells.nth(1)),
                "engagement": text_or_empty(cells.nth(2)),
            }
        )
    return {
        "subscriber_growth": text_or_empty(page.locator("text=/subscriber|abonnee/i")),
        "posts": posts,
    }


def read_comments(page: Any, payload: dict[str, Any], limit: int) -> list[dict[str, str]]:
    target = payload.get("comments_url") or publication_url(payload) + "/publish/comments"
    page.goto(str(target), wait_until="domcontentloaded")
    nodes = page.locator("article, [data-testid*=comment], .comment")
    comments = []
    for index in range(min(nodes.count(), limit)):
        node = nodes.nth(index)
        comments.append(
            {
                "author": text_or_empty(node.locator("a, [class*=author]")),
                "text": text_or_empty(node.locator("p, [class*=body]")),
            }
        )
    return comments


def fetch_profile(page: Any, payload: dict[str, Any]) -> dict[str, Any]:
    page.goto(publication_url(payload) + "/about", wait_until="domcontentloaded")
    return {
        "url": page.url,
        "name": text_or_empty(page.locator("h1")),
        "description": text_or_empty(page.locator("main p, [class*=description]")),
        "subscriber_text": text_or_empty(page.locator("text=/subscriber|abonnee/i")),
    }


def save_artifacts(directory: Path, prefix: str, data: Any) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = uuid4().hex[:8]
    json_path = directory / f"{prefix}_{stamp}_{token}.json"
    md_path = directory / f"{prefix}_{stamp}_{token}.md"
    rendered = json.dumps(data, ensure_ascii=False, indent=2)
    json_path.write_text(rendered + "\n", encoding="utf-8")
    md_path.write_text(
        f"---\ntype: {prefix}\ncreated_at: {datetime.now(timezone.utc).isoformat()}\n---\n\n```json\n{rendered}\n```\n",
        encoding="utf-8",
    )
    return {"json_path": str(json_path), "markdown_path": str(md_path)}


def mock_result(mode: str, payload: dict[str, Any]) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": mode,
        "mock": True,
        "status": "AUTH_REQUIRED",
        "substack_contacted": False,
    }
    if mode in {"post_article", "post_note"}:
        base.update(
            {
                "published": False,
                "text": content(payload),
                "media": media_manifest(payload, strict=False),
            }
        )
    elif mode == "analytics":
        base.update({"subscriber_growth": 0, "posts": [{"views": 0, "engagement": 0}]})
    elif mode == "read_comments":
        base["comments"] = [{"author": "Mock reader", "text": "Auth required; no network request."}]
    elif mode == "fetch_profile":
        base.update({"name": "Mock Substack profile", "description": "Auth required", "subscriber_text": "0"})
    return base


def execute(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    mode = operation(args, payload)
    auth = validated_auth(args.auth)
    if auth is None:
        result = mock_result(mode, payload)
    else:
        headless = args.headless if args.headless is not None else boolean_env("SUBSTACK_HEADLESS", True)
        with browser_page(auth, headless) as page:
            try:
                if mode == "post_article":
                    result = post_article(page, payload, args.dry_run)
                elif mode == "post_note":
                    result = post_note(page, payload, args.dry_run)
                elif mode == "analytics":
                    result = scrape_analytics(page, payload, args.limit)
                elif mode == "read_comments":
                    result = {"comments": read_comments(page, payload, args.limit)}
                else:
                    result = fetch_profile(page, payload)
            except Exception as exc:
                artifact = screenshot(page, mode + "_error")
                raise SubstackProError(f"{mode} failed; screenshot={artifact}: {exc}") from exc

    if mode in {"analytics", "read_comments"}:
        result.update(save_artifacts(ANALYTICS_DIR, f"substack_{mode}", result))
    elif mode == "fetch_profile":
        result.update(save_artifacts(RESEARCH_DIR, "substack_profile", result))
    return result


def main() -> int:
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
            should_execute = args.event_id is not None or any(
                (args.post_article, args.post_note, args.analytics, args.read_comments, args.fetch_profile)
            )
            if should_execute:
                if args.event_id is not None:
                    payload = load_payload(connection, args.event_id)
                apply_cli_payload(args, payload)
                mode = operation(args, payload)
                is_publish = mode in {"post_article", "post_note"}
                if args.event_id is not None and is_publish and validated_auth(args.auth) is not None and not args.dry_run:
                    ledger = begin_submission(
                        connection, event_id=args.event_id, channel="SUBSTACK",
                        payload=payload, target=publication_url(payload),
                    )
                    if ledger["status"] == "CONFIRMED":
                        emit_result("COMPLETED", result={"idempotent_replay": True, "platform_url": ledger["platform_url"]})
                        return 0
                    ledger_started = True
                try:
                    result = execute(args, payload)
                except Exception as exc:
                    if ledger_started and args.event_id is not None:
                        update_publication(connection, args.event_id, "SUBSTACK", "UNKNOWN", detail=f"{type(exc).__name__}: {exc}")
                    raise
                if ledger_started and args.event_id is not None:
                    update_publication(
                        connection, args.event_id, "SUBSTACK", "CONFIRMED",
                        platform_id=result.get("platform_id"), platform_url=result.get("platform_url"),
                    )
                LOGGER.info("Operation result: %s", json.dumps(result, ensure_ascii=False))
                if args.event_id is not None:
                    outcome = "BLOCKED_AUTH" if result.get("status") == "AUTH_REQUIRED" else "SIMULATED" if result.get("dry_run") else "COMPLETED"
                    emit_result(outcome, result=result, payload_patch={"substack_pro": result})
    except Exception as exc:
        if args.event_id is not None:
            emit_result("UNKNOWN" if ledger_started else "FAILED", error=f"{type(exc).__name__}: {exc}", retryable=False)
        LOGGER.exception("Substack Pro operation failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
