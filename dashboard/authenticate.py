#!/usr/bin/env python3
"""Interactive Playwright login helper that persists secure storage-state."""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import sys
import time
from pathlib import Path
from typing import Final

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


LOGGER: Final = logging.getLogger("dashboard_authenticate")
PROFILES: Final = {
    "linkedin": {
        "url": "https://www.linkedin.com/login",
        "domains": ("linkedin.com",),
        "cookie_names": ("li_at",),
    },
    "substack": {
        "url": "https://substack.com/sign-in",
        "domains": ("substack.com",),
        "cookie_names": ("substack.sid", "connect.sid", "session"),
    },
    "medium": {
        "url": "https://medium.com/m/signin",
        "domains": ("medium.com",),
        "cookie_names": ("sid", "uid", "xsrf"),
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture an authenticated browser session.")
    parser.add_argument("--platform", choices=tuple(PROFILES), required=True)
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args()


def authenticated(cookies: list[dict[str, object]], platform: str) -> bool:
    """Require a known session cookie on the expected platform domain."""
    profile = PROFILES[platform]
    for cookie in cookies:
        name = str(cookie.get("name", ""))
        domain = str(cookie.get("domain", "")).lstrip(".")
        if name in profile["cookie_names"] and any(
            domain == allowed or domain.endswith("." + allowed)
            for allowed in profile["domains"]
        ):
            return True
    return False


def atomic_storage_state(context: object, destination: Path) -> None:
    """Write storage-state atomically with owner-only permissions."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        context.storage_state(path=str(temporary))  # type: ignore[attr-defined]
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        state = json.loads(temporary.read_text(encoding="utf-8"))
        if not isinstance(state.get("cookies"), list):
            raise ValueError("invalid generated storage-state")
        temporary.replace(destination)
        os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    if args.timeout < 30:
        LOGGER.error("--timeout must be at least 30 seconds")
        return 2
    profile = PROFILES[args.platform]
    LOGGER.info("Opening %s login; waiting up to %ss", args.platform, args.timeout)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(profile["url"], wait_until="domcontentloaded")
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                if authenticated(context.cookies(), args.platform):
                    atomic_storage_state(context, args.auth_file.expanduser().resolve())
                    LOGGER.info("Connected; session saved to %s", args.auth_file)
                    browser.close()
                    return 0
                if not context.pages:
                    break
                page.wait_for_timeout(1000)
            browser.close()
    except (OSError, ValueError, PlaywrightError):
        LOGGER.exception("Authentication helper failed")
        return 1
    LOGGER.error("Authentication was not completed before timeout/browser close")
    return 1


if __name__ == "__main__":
    sys.exit(main())
