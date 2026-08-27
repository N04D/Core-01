#!/usr/bin/env python3
"""Periodically verify Playwright storage-state for active channel plugins."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database import connect_database

import requests
from dotenv import load_dotenv


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
LOGGER: Final = logging.getLogger("session_health")


@dataclass(frozen=True)
class PlatformProfile:
    platform: str
    plugin_names: tuple[str, ...]
    auth_file: Path
    check_url: str
    authenticated_selector: str


PROFILES: Final = (
    PlatformProfile(
        "linkedin",
        ("LinkedIn Pro Publisher & Analytics", "LinkedIn Publisher (Productie)"),
        PROJECT_ROOT / "config" / "linkedin_auth.json",
        "https://www.linkedin.com/feed/",
        'a[href*="/in/"], button[aria-label*="Start a post"], button:has-text("Een bijdrage starten")',
    ),
    PlatformProfile(
        "substack",
        ("Substack Pro Publisher & Analytics", "Substack Publisher"),
        PROJECT_ROOT / "config" / "substack_auth.json",
        "https://substack.com/home",
        'a[href*="/publish"], a:has-text("Dashboard"), button:has-text("Dashboard")',
    ),
    PlatformProfile(
        "medium",
        ("Medium Publisher",),
        PROJECT_ROOT / "config" / "medium_auth.json",
        "https://medium.com/me/settings",
        'a[href*="/@"], button[aria-label*="user" i], img[alt*="profile" i]',
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check active publisher sessions.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--interval", type=float, default=900.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval <= 0 or args.timeout <= 0:
        parser.error("--interval and --timeout must be positive")
    return args


def connect(database: Path) -> sqlite3.Connection:
    return connect_database(database)


def active_profiles(connection: sqlite3.Connection) -> list[tuple[PlatformProfile, str]]:
    active = {
        str(row[0])
        for row in connection.execute(
            "SELECT plugin_name FROM plugin_registry WHERE type='channel' AND is_active=1"
        )
    }
    selected = []
    for profile in PROFILES:
        plugin_name = next((name for name in profile.plugin_names if name in active), None)
        if plugin_name:
            selected.append((profile, plugin_name))
    return selected


def validate_storage_state(auth_file: Path) -> tuple[bool, str]:
    if not auth_file.is_file():
        return False, "Sessiebestand ontbreekt"
    mode = stat.S_IMODE(auth_file.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return False, f"Onveilige auth-rechten {mode:o}; verwacht 600"
    try:
        state = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Ongeldige auth-state: {exc}"
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not state["cookies"]:
        return False, "Auth-state bevat geen cookies"
    return True, "Storage-state structureel geldig"


def browser_check(profile: PlatformProfile, timeout: float) -> tuple[str, str]:
    valid, detail = validate_storage_state(profile.auth_file)
    if not valid:
        return "AUTH_REQUIRED", detail
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(storage_state=str(profile.auth_file))
                page = context.new_page()
                page.goto(profile.check_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
                current = page.url.casefold()
                if any(marker in current for marker in ("login", "signin", "authwall", "checkpoint")):
                    return "AUTH_REQUIRED", f"Doorgestuurd naar login: {page.url}"
                try:
                    page.locator(profile.authenticated_selector).first.wait_for(state="visible", timeout=min(int(timeout * 1000), 10000))
                except PlaywrightTimeout:
                    return "AUTH_REQUIRED", "Geen ingelogde accountindicator gevonden"
                return "CONNECTED", f"Headless sessie geldig op {profile.check_url}"
            finally:
                browser.close()
    except ImportError:
        return "ERROR", "Playwright is niet geïnstalleerd"
    except Exception as exc:
        return "ERROR", f"Browsercontrole mislukt: {type(exc).__name__}: {exc}"[:2000]


def telegram_notification(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_NOTIFICATION_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=(5, 15),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        LOGGER.warning("Telegram notification failed: %s", exc)


def persist_result(
    connection: sqlite3.Connection,
    profile: PlatformProfile,
    plugin_name: str,
    status: str,
    detail: str,
) -> bool:
    previous = connection.execute(
        "SELECT status FROM session_health WHERE platform=?", (profile.platform,)
    ).fetchone()
    changed_to_critical = status == "AUTH_REQUIRED" and (
        previous is None or previous["status"] != "AUTH_REQUIRED"
    )
    with connection:
        connection.execute(
            """INSERT INTO session_health(platform,plugin_name,auth_file,status,detail,checked_at)
               VALUES (?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(platform) DO UPDATE SET
               plugin_name=excluded.plugin_name,auth_file=excluded.auth_file,
               status=excluded.status,detail=excluded.detail,checked_at=CURRENT_TIMESTAMP""",
            (profile.platform, plugin_name, str(profile.auth_file), status, detail),
        )
        if changed_to_critical:
            message = f"{profile.platform.title()} sessie vereist opnieuw inloggen: {detail}"
            payload = json.dumps(
                {"platform": profile.platform, "plugin_name": plugin_name, "status": status, "detail": detail},
                ensure_ascii=False,
            )
            connection.execute(
                "INSERT INTO events_queue(event_type,payload,status) VALUES ('SYSTEM_AUTH_REQUIRED',?,'COMPLETED')",
                (payload,),
            )
            connection.execute(
                "INSERT INTO system_notifications(severity,title,message,platform) VALUES ('CRITICAL',?,?,?)",
                (f"{profile.platform.title()} opnieuw verbinden", message, profile.platform),
            )
    return changed_to_critical


def check_once(database: Path, timeout: float) -> list[tuple[str, str, bool]]:
    results = []
    with connect(database) as connection:
        for profile, plugin_name in active_profiles(connection):
            status, detail = browser_check(profile, timeout)
            critical = persist_result(connection, profile, plugin_name, status, detail)
            LOGGER.info("platform=%s status=%s detail=%s", profile.platform, status, detail)
            if critical:
                telegram_notification(f"🚨 Social Engine: {profile.platform.title()} authenticatie vereist. {detail}")
            results.append((profile.platform, status, critical))
    return results


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()
    database = args.db.expanduser().resolve()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            results = check_once(database, args.timeout)
            LOGGER.info("Session scan complete; active_platforms=%s", len(results))
            if args.once:
                return 0
            stop.wait(args.interval)
    except (OSError, sqlite3.Error, ValueError):
        LOGGER.exception("Session health daemon stopped")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
