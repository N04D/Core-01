#!/usr/bin/env python3
"""LinkedIn Pro publisher, analytics collector, and profile-context reader."""

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


PLUGIN_NAME: Final = "LinkedIn Pro Publisher & Analytics"
PLUGIN_TYPE: Final = "channel"
PLUGIN_ICON: Final = "🔗"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE: Final = Path("../db/events.db")
DEFAULT_AUTH: Final = PROJECT_ROOT / "config" / "linkedin_auth.json"
ANALYTICS_DIR: Final = PROJECT_ROOT / "vault" / "analytics"
RESEARCH_DIR: Final = PROJECT_ROOT / "vault" / "research"
SCREENSHOT_DIR: Final = PROJECT_ROOT / "vault" / "logs" / "screenshots"
LOGGER: Final = logging.getLogger("pub_linkedin_pro")


class LinkedInProError(RuntimeError):
    """Raised when a LinkedIn Pro operation cannot safely complete."""


def parse_args() -> argparse.Namespace:
    """Parse mutually compatible event-bus and standalone operation options."""
    parser = argparse.ArgumentParser(
        description="LinkedIn Pro publishing, analytics, and profile context plugin."
    )
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--analytics", action="store_true")
    modes.add_argument("--fetch-bio", action="store_true")
    parser.add_argument("--company-id", help="LinkedIn company/page identifier.")
    parser.add_argument("--newsletter-id", help="LinkedIn newsletter identifier.")
    parser.add_argument("--text", help="Inline post/article text (overrides payload content).")
    parser.add_argument("--image-path", type=Path, help="JPG/PNG media attachment.")
    parser.add_argument("--video-path", type=Path, help="Video media attachment.")
    parser.add_argument("--profile-url", help="Explicit LinkedIn profile URL for bio mode.")
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override LINKEDIN_HEADLESS (default: true).",
    )
    args = parser.parse_args()
    if not any(
        (
            args.register,
            args.event_id is not None,
            args.analytics,
            args.fetch_bio,
            args.text is not None,
            args.image_path is not None,
            args.video_path is not None,
        )
    ):
        parser.error(
            "choose --register, --event_id, --analytics, --fetch-bio, or content/media"
        )
    if args.event_id is not None and args.event_id < 1:
        parser.error("--event_id must be positive")
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.newsletter_id and args.company_id:
        parser.error("--newsletter-id and --company-id are mutually exclusive targets")
    return args


def connect(database: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    connection = sqlite3.connect(database, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def register_plugin(connection: sqlite3.Connection) -> None:
    """Idempotently register the Pro plugin."""
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
    """Load a JSON event payload."""
    row = connection.execute(
        "SELECT payload FROM events_queue WHERE id=?", (event_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"event {event_id} does not exist")
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    return payload


def validated_auth(path: Path) -> Path | None:
    """Return secure Playwright storage-state or None for safe mock fallback."""
    auth = path.expanduser().resolve()
    if not auth.is_file():
        LOGGER.warning("SAFE MOCK: auth state missing (%s); LinkedIn is not contacted", auth)
        return None
    mode = stat.S_IMODE(auth.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"auth state must be mode 600, found {mode:o}: {auth}")
    state = json.loads(auth.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or "cookies" not in state:
        raise ValueError("auth state is not a Playwright storage-state document")
    return auth


def boolean_env(name: str, default: bool) -> bool:
    """Parse a strict boolean environment variable."""
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.lower().strip()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@contextmanager
def browser_page(auth: Path, headless: bool) -> Iterator[Any]:
    """Yield an authenticated Playwright page and close all resources reliably."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LinkedInProError(
            "Playwright missing; install it and run 'playwright install chromium'"
        ) from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(auth), locale="nl-NL")
        page = context.new_page()
        page.set_default_timeout(int(os.getenv("LINKEDIN_TIMEOUT_MS", "30000")))
        try:
            yield page
        finally:
            context.close()
            browser.close()


def first_visible(page: Any, selectors: list[str], timeout: int = 30000) -> Any:
    """Resolve the first visible selector from a resilient fallback set."""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=max(1000, timeout // len(selectors)))
            return locator
        except Exception:
            continue
    raise LinkedInProError("expected LinkedIn control was not found: " + " | ".join(selectors))


def capture_error(page: Any, operation: str) -> Path | None:
    """Save a full-page diagnostic screenshot."""
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = SCREENSHOT_DIR / f"linkedin_pro_{operation}_{stamp}_{uuid4().hex[:6]}.png"
        page.screenshot(path=str(path), full_page=True)
        return path
    except Exception:
        LOGGER.exception("Unable to save diagnostic screenshot")
        return None


def content_from_payload(payload: dict[str, Any]) -> str:
    """Resolve publishable text from inline content or a Markdown file."""
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    raw_path = payload.get("draft_file", payload.get("filepath"))
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("payload needs content, draft_file, or filepath")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    text = path.resolve(strict=True).read_text(encoding="utf-8")
    match = re.fullmatch(r"---\s*\n.*?\n---\s*\n?(.*)", text, re.DOTALL)
    return (match.group(1) if match else text).strip()


def media_manifest(payload: dict[str, Any], strict: bool) -> list[dict[str, Any]]:
    """Validate image/video payload paths and return normalized media metadata."""
    manifest: list[dict[str, Any]] = []
    rules = {
        "image_path": {".jpg", ".jpeg", ".png"},
        "video_path": {".mp4", ".mov", ".m4v", ".webm"},
    }
    for field, extensions in rules.items():
        raw = payload.get(field)
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{field} must be a non-empty path")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        exists = path.is_file()
        suffix = path.suffix.lower()
        if suffix not in extensions:
            raise ValueError(f"unsupported {field} extension: {suffix}")
        if strict and not exists:
            raise FileNotFoundError(f"media file missing: {path}")
        manifest.append(
            {
                "kind": "image" if field == "image_path" else "video",
                "path": str(path.resolve()) if exists else str(path.resolve(strict=False)),
                "exists": exists,
            }
        )
    return manifest


def upload_media(page: Any, manifest: list[dict[str, Any]]) -> None:
    """Upload normalized media through LinkedIn's hidden file inputs."""
    for media in manifest:
        selectors = (
            ["input[type='file'][accept*='image']", "input[type='file']"]
            if media["kind"] == "image"
            else ["input[type='file'][accept*='video']", "input[type='file']"]
        )
        uploaded = False
        for selector in selectors:
            locator = page.locator(selector).last
            try:
                if locator.count():
                    locator.set_input_files(media["path"])
                    uploaded = True
                    break
            except Exception:
                continue
        if not uploaded:
            raise LinkedInProError(f"no upload input found for {media['kind']}")


def make_teaser(payload: dict[str, Any], content: str) -> str:
    """Generate a deterministic teaser for article or link publications."""
    explicit = payload.get("teaser")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    title = payload.get("title") or payload.get("topic") or "Nieuwe publicatie"
    link = payload.get("article_url") or payload.get("url")
    summary = " ".join(content.split())[:240].rstrip()
    teaser = f"{title}\n\n{summary}"
    if isinstance(link, str) and urlparse(link).scheme in {"http", "https"}:
        teaser += f"…\n\nLees verder: {link}"
    return teaser


def publish_post(
    page: Any,
    payload: dict[str, Any],
    company_id: str | None,
    newsletter_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Compose a profile/company post or newsletter article."""
    content = content_from_payload(payload)
    teaser = make_teaser(payload, content)
    media = media_manifest(payload, strict=True)
    if newsletter_id:
        page.goto(
            f"https://www.linkedin.com/newsletters/{newsletter_id}/new/",
            wait_until="domcontentloaded",
        )
        title = str(payload.get("title") or payload.get("topic") or "Nieuw artikel")
        first_visible(page, ["input[placeholder*='title' i]", "input[name='title']"]).fill(title)
        first_visible(
            page,
            ["div[contenteditable='true'][role='textbox']", "div.ProseMirror"],
        ).fill(content)
        upload_media(page, media)
    else:
        target = (
            f"https://www.linkedin.com/company/{company_id}/admin/page-posts/published/"
            if company_id
            else "https://www.linkedin.com/feed/"
        )
        page.goto(target, wait_until="domcontentloaded")
        first_visible(
            page,
            [
                "button:has-text('Start a post')",
                "button:has-text('Een bijdrage beginnen')",
                "button.share-box-feed-entry__trigger",
            ],
        ).click()
        first_visible(
            page,
            [
                "div[role='dialog'] div[contenteditable='true'][role='textbox']",
                "div[role='dialog'] div.ql-editor",
            ],
        ).fill(teaser)
        upload_media(page, media)

    if dry_run:
        artifact = capture_error(page, "dry_run")
        return {"published": False, "dry_run": True, "media": media, "screenshot": str(artifact) if artifact else None}
    button = first_visible(
        page,
        [
            "button:has-text('Publish')",
            "button:has-text('Publiceren')",
            "button:has-text('Post')",
            "button:has-text('Plaatsen')",
        ],
    )
    if not button.is_enabled():
        raise LinkedInProError("publication button is disabled")
    button.click()
    return {"published": True, "dry_run": False, "media": media}


def text_or_empty(locator: Any) -> str:
    """Safely normalize locator text."""
    try:
        return locator.first.inner_text(timeout=1500).strip()
    except Exception:
        return ""


def scrape_analytics(page: Any, company_id: str | None, limit: int) -> list[dict[str, Any]]:
    """Collect recent post text, visible metrics, and loaded comments."""
    url = (
        f"https://www.linkedin.com/company/{company_id}/posts/?feedView=all"
        if company_id
        else "https://www.linkedin.com/in/me/recent-activity/all/"
    )
    page.goto(url, wait_until="domcontentloaded")
    page.locator("article, div.feed-shared-update-v2").first.wait_for(state="visible")
    posts: list[dict[str, Any]] = []
    cards = page.locator("article, div.feed-shared-update-v2")
    for index in range(min(cards.count(), limit)):
        card = cards.nth(index)
        for label in ("Load more comments", "Meer opmerkingen laden", "Show all comments"):
            try:
                button = card.get_by_text(label, exact=False).first
                if button.is_visible(timeout=300):
                    button.click()
            except Exception:
                pass
        comments = []
        comment_nodes = card.locator("article.comments-comment-item, div.comments-comment-item")
        for comment_index in range(comment_nodes.count()):
            comment = comment_nodes.nth(comment_index)
            comments.append(
                {
                    "author": text_or_empty(comment.locator(".comments-post-meta__name-text, .hoverable-link-text")),
                    "text": text_or_empty(comment.locator(".comments-comment-item__main-content, .comments-comment-item-content-body")),
                }
            )
        posts.append(
            {
                "content": text_or_empty(card.locator(".feed-shared-update-v2__description, .update-components-text")),
                "likes": text_or_empty(card.locator(".social-details-social-counts__reactions-count")),
                "views": text_or_empty(card.locator("text=/[0-9.,]+ (views|weergaven)/i")),
                "comments": comments,
            }
        )
    return posts


def fetch_profile(page: Any, profile_url: str | None, company_id: str | None) -> dict[str, Any]:
    """Extract visible profile/company headline, bio, and experience text."""
    url = profile_url or (
        f"https://www.linkedin.com/company/{company_id}/about/"
        if company_id
        else "https://www.linkedin.com/in/me/"
    )
    if urlparse(url).hostname not in {"linkedin.com", "www.linkedin.com"}:
        raise ValueError("profile URL must use linkedin.com")
    page.goto(url, wait_until="domcontentloaded")
    page.locator("main").wait_for(state="visible")
    return {
        "url": page.url,
        "name": text_or_empty(page.locator("main h1")),
        "headline": text_or_empty(page.locator("main .text-body-medium, main .org-top-card-summary__tagline")),
        "bio": text_or_empty(page.locator("section:has(#about) .inline-show-more-text, main section.artdeco-card p")),
        "experience": text_or_empty(page.locator("section:has(#experience), section:has-text('Experience'), section:has-text('Ervaring')")),
    }


def save_artifact(directory: Path, prefix: str, data: Any) -> tuple[Path, Path]:
    """Persist structured JSON and readable Markdown artifacts."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = uuid4().hex[:8]
    json_path = directory / f"{prefix}_{stamp}_{token}.json"
    md_path = directory / f"{prefix}_{stamp}_{token}.md"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(
        f"---\ntype: {prefix}\ncreated_at: {datetime.now(timezone.utc).isoformat()}\n---\n\n"
        f"```json\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```\n",
        encoding="utf-8",
    )
    return json_path, md_path


def mock_result(mode: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an explicit non-network fallback result."""
    base: dict[str, Any] = {
        "mode": mode,
        "mock": True,
        "linkedin_contacted": False,
        "status": "AUTH_REQUIRED",
    }
    if mode == "analytics":
        base["posts"] = [
            {
                "content": "Veilige LinkedIn Pro mock-publicatie",
                "likes": 0,
                "views": 0,
                "comments": [
                    {
                        "author": "Mock gebruiker",
                        "text": "Mock engagement; LinkedIn is niet benaderd.",
                    }
                ],
            }
        ]
    elif mode == "fetch_bio":
        base.update(
            {
                "name": "Mock LinkedIn-profiel",
                "headline": "AUTH_REQUIRED — veilige lokale fallback",
                "bio": "Geen profieldata opgehaald; LinkedIn is niet benaderd.",
                "experience": [],
            }
        )
    elif mode == "publish":
        publish_payload = payload or {}
        base.update(
            {
                "published": False,
                "text": publish_payload.get("content", ""),
                "media": media_manifest(publish_payload, strict=False),
            }
        )
    return base


def execute(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute the selected operation or safe mock fallback."""
    auth = validated_auth(args.auth)
    mode = "analytics" if args.analytics else "fetch_bio" if args.fetch_bio else "publish"
    if auth is None:
        result = mock_result(mode, payload)
        directory = ANALYTICS_DIR if mode == "analytics" else RESEARCH_DIR
        json_path, md_path = save_artifact(directory, f"linkedin_{mode}_mock", result)
        result.update({"json_path": str(json_path), "markdown_path": str(md_path)})
        return result

    headless = args.headless if args.headless is not None else boolean_env("LINKEDIN_HEADLESS", True)
    with browser_page(auth, headless) as page:
        try:
            if args.analytics:
                data = scrape_analytics(page, args.company_id, args.limit)
                json_path, md_path = save_artifact(ANALYTICS_DIR, "linkedin_analytics", data)
                return {"posts": len(data), "json_path": str(json_path), "markdown_path": str(md_path)}
            if args.fetch_bio:
                data = fetch_profile(page, args.profile_url, args.company_id)
                json_path, md_path = save_artifact(RESEARCH_DIR, "linkedin_profile", data)
                return {**data, "json_path": str(json_path), "markdown_path": str(md_path)}
            return publish_post(page, payload, args.company_id, args.newsletter_id, args.dry_run)
        except Exception as exc:
            screenshot = capture_error(page, mode)
            raise LinkedInProError(f"{mode} failed; screenshot={screenshot}: {exc}") from exc


def main() -> int:
    """Register and/or run the selected LinkedIn Pro capability."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    database = args.db.expanduser().resolve()
    event_payload: dict[str, Any] = {}
    ledger_started = False
    try:
        with connect(database) as connection:
            if args.register:
                register_plugin(connection)
            should_execute = (
                args.event_id is not None
                or args.analytics
                or args.fetch_bio
                or args.text is not None
                or args.image_path is not None
                or args.video_path is not None
            )
            if should_execute:
                if args.event_id is not None:
                    event_payload = load_payload(connection, args.event_id)
                if args.text is not None:
                    event_payload["content"] = args.text
                if args.image_path is not None:
                    event_payload["image_path"] = str(args.image_path)
                if args.video_path is not None:
                    event_payload["video_path"] = str(args.video_path)
                mode = "analytics" if args.analytics else "fetch_bio" if args.fetch_bio else "publish"
                if args.event_id is not None and mode == "publish" and validated_auth(args.auth) is not None and not args.dry_run:
                    target = args.newsletter_id or args.company_id or "personal-profile"
                    ledger = begin_submission(
                        connection, event_id=args.event_id, channel="LINKEDIN",
                        payload=event_payload, target=str(target),
                    )
                    if ledger["status"] == "CONFIRMED":
                        emit_result("COMPLETED", result={"idempotent_replay": True, "platform_url": ledger["platform_url"]})
                        return 0
                    ledger_started = True
                try:
                    result = execute(args, event_payload)
                except Exception as exc:
                    if ledger_started and args.event_id is not None:
                        update_publication(connection, args.event_id, "LINKEDIN", "UNKNOWN", detail=f"{type(exc).__name__}: {exc}")
                    raise
                if ledger_started and args.event_id is not None:
                    update_publication(
                        connection, args.event_id, "LINKEDIN", "CONFIRMED",
                        platform_id=result.get("platform_id"), platform_url=result.get("platform_url"),
                    )
                LOGGER.info("Operation result: %s", json.dumps(result, ensure_ascii=False))
                if args.event_id is not None:
                    outcome = "BLOCKED_AUTH" if result.get("status") == "AUTH_REQUIRED" else "SIMULATED" if result.get("dry_run") else "COMPLETED"
                    emit_result(outcome, result=result, payload_patch={"linkedin_pro": result})
    except Exception as exc:
        if args.event_id is not None:
            emit_result("UNKNOWN" if ledger_started else "FAILED", error=f"{type(exc).__name__}: {exc}", retryable=False)
        LOGGER.exception("LinkedIn Pro operation failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
