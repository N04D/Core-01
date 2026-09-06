#!/usr/bin/env python3
"""Interactive Playwright login helper that persists secure storage-state."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Final

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LOGGER: Final = logging.getLogger("dashboard_authenticate")
from core.keyring_store import get_secret
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
    parser.add_argument("--cdp-url", help="Reuse an already-running Chrome via CDP.")
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


def autofill_linkedin(page: object) -> None:
    """Fill direct LinkedIn credentials from keyring when available.

    This deliberately does not handle Google OAuth; the user completes MFA or
    any additional security challenge in the visible browser.
    """
    username = get_secret("linkedin.username")
    password = get_secret("linkedin.password")
    if not username or not password:
        return
    # LinkedIn's current React login form uses generated ids and omits the
    # historical ``name=session_key`` attribute.  Keep the selectors scoped to
    # editable controls and include the semantic input type as the stable
    # fallback.
    email_candidates = page.locator(
        "#username, input[name='session_key'], input[type='email'], "
        "input[autocomplete='username'], input[autocomplete='email']"
    )  # type: ignore[attr-defined]
    secret_candidates = page.locator(
        "#password, input[name='session_password'], input[type='password']"
    )  # type: ignore[attr-defined]

    def first_visible(candidates: object) -> object | None:
        for index in range(candidates.count()):  # type: ignore[attr-defined]
            candidate = candidates.nth(index)  # type: ignore[attr-defined]
            if candidate.is_visible():  # type: ignore[attr-defined]
                return candidate
        return None

    email = first_visible(email_candidates)
    secret = first_visible(secret_candidates)
    try:
        if email is None or secret is None:
            raise LookupError("visible LinkedIn credential fields not found")
        email.wait_for(state="visible", timeout=5_000)  # type: ignore[attr-defined]
        secret.wait_for(state="visible", timeout=5_000)  # type: ignore[attr-defined]
    except Exception:
        LOGGER.info(
            "LinkedIn loginvelden zijn niet beschikbaar (url=%s); handmatige login vereist",
            getattr(page, "url", " onbekend"),
        )
        return
    if email is not None and secret is not None:
        email.fill(username)  # type: ignore[attr-defined]
        secret.fill(password)  # type: ignore[attr-defined]
        # The label is localized (Aanmelden/Sign in) and can be rendered as a
        # button or submit input depending on the experiment bucket.
        # Exact name deliberately excludes "Aanmelden met Google/Apple".
        submit = page.get_by_role("button", name=re.compile(r"^(Sign in|Aanmelden)$"))  # type: ignore[attr-defined]
        if not submit.count():
            submit = page.locator("button[type='submit'], input[type='submit']")  # type: ignore[attr-defined]
        submitted = False
        for index in range(submit.count()):
            candidate = submit.nth(index)
            if not candidate.is_visible():
                continue
            try:
                candidate.click(timeout=5_000)
                submitted = True
                break
            except Exception as exc:
                LOGGER.debug("LinkedIn submit-click fallback: %s", exc)
        if not submitted:
            # React occasionally intercepts the pointer event.  requestSubmit
            # still invokes the page's normal validation and submit handler.
            form = page.locator("form:has(input[type='password'])")
            if not form.count():
                form = page.locator("form").first
            form.evaluate("form => form.requestSubmit()")
            submitted = True
        LOGGER.info(
            "LinkedIn gebruikersnaam en wachtwoord ingevuld; submit uitgevoerd (url=%s)",
            getattr(page, "url", "onbekend"),
        )


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
            cdp_url = args.cdp_url or os.environ.get("LINKEDIN_CDP_URL") or os.environ.get("BROWSER_CDP_URL")
            external_browser = bool(cdp_url)
            if cdp_url:
                browser = playwright.chromium.connect_over_cdp(cdp_url)
                if not browser.contexts:
                    raise RuntimeError("CDP browser has no contexts")
                context = browser.contexts[0]
                pages = list(context.pages)
                page = next(
                    (candidate for candidate in pages if any(domain in candidate.url for domain in profile["domains"])),
                    pages[0] if pages else context.new_page(),
                )
                LOGGER.info("Reusing existing CDP browser tab: %s", page.url)
                if profile["domains"][0] not in page.url:
                    page.goto(profile["url"], wait_until="domcontentloaded", timeout=30_000)
            else:
                # Prefer the system Chromium when available: it matches the
                # browser profile/cookies users normally export and avoids the
                # generic bundled Playwright fingerprint.
                executable = os.environ.get("BROWSER_EXECUTABLE", "/usr/bin/chromium-browser")
                launch_options: dict[str, object] = {
                    "headless": False,
                    "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                    "ignore_default_args": ["--enable-automation"],
                }
                if Path(executable).exists():
                    launch_options["executable_path"] = executable
                browser = playwright.chromium.launch(**launch_options)
                context = browser.new_context()
                page = context.new_page()
                page.goto(profile["url"], wait_until="domcontentloaded")
            if args.platform == "linkedin":
                try:
                    autofill_linkedin(page)
                except Exception:
                    LOGGER.warning("LinkedIn keyring autofill unavailable; continuing with manual login")
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                if authenticated(context.cookies(), args.platform):
                    atomic_storage_state(context, args.auth_file.expanduser().resolve())
                    LOGGER.info("Connected; session saved to %s", args.auth_file)
                    if not external_browser:
                        browser.close()
                    return 0
                if not context.pages:
                    break
                page.wait_for_timeout(1000)
            if not external_browser:
                browser.close()
    except (OSError, ValueError, PlaywrightError):
        LOGGER.exception("Authentication helper failed")
        return 1
    LOGGER.error("Authentication was not completed before timeout/browser close")
    return 1


if __name__ == "__main__":
    sys.exit(main())
