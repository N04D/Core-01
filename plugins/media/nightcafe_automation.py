#!/usr/bin/env python3
"""Generate one NightCafe asset and register it in the local Media Store."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.browser_robustness import capture_page_trace, capture_sanitized_diagnostic, find_control, find_prompt_input
from core.database import connect_database
from core.paths import SESSIONS_DIR, MEDIA_DIR, DATABASE_PATH
from core.event_protocol import emit_result

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DB: Final = DATABASE_PATH
DEFAULT_AUTH: Final = SESSIONS_DIR / "nightcafe_auth.json"
DEFAULT_OUTPUT: Final = MEDIA_DIR / "nightcafe"
SOURCE_KEY: Final = "nightcafe-99-names"
PLUGIN_NAME: Final = "NightCafe Daily Stock Generator"
LOGGER = logging.getLogger("nightcafe_automation")
MOCK_PNG: Final = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+Xw4SAAAAAElFTkSuQmCC"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", action="store_true", help="Register plugin and event route, then exit.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--prompt")
    parser.add_argument("--model", help="NightCafe model name (for example: Flux 2 Klein 9B Fast).")
    parser.add_argument("--format", dest="aspect_format", help="Aspect ratio/format (for example: 16:9).")
    parser.add_argument("--negative-prompt", default="", help="Negative prompt, when supported by the UI.")
    parser.add_argument("--steps", type=int, help="Generation steps, when exposed by the selected model.")
    parser.add_argument("--name")
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--run-date")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--auth", type=Path, default=Path(os.getenv("NIGHTCAFE_AUTH_FILE", str(DEFAULT_AUTH))))
    parser.add_argument("--live", action="store_true", help="Allow a real external generation.")
    parser.add_argument("--simulate", action="store_true", help="Explicit local test mode; never contacts NightCafe.")
    parser.add_argument("--claim-daily", action="store_true", help="Claim an available daily top-up in live mode.")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--cdp-url", default=os.getenv("NIGHTCAFE_CDP_URL"), help="Attach to a user-owned Chrome DevTools endpoint.")
    parser.add_argument("--user-data-dir", type=Path, default=os.getenv("NIGHTCAFE_USER_DATA_DIR"), help="Open a user-owned persistent Chrome profile (headed).")
    parser.add_argument("--cdp-wait-seconds", type=float, default=15.0, help="Seconds to wait for a user-owned CDP endpoint before fallback.")
    parser.add_argument("--timeout", type=int, default=180_000)
    parser.add_argument("--generation-timeout", type=int, default=600_000,
                        help="Maximum render/download wait in milliseconds (default: 600 seconds).")
    args = parser.parse_args()
    if args.event_id is not None and args.event_id < 1:
        parser.error("event_id must be positive")
    if args.sequence is not None and not 1 <= args.sequence <= 99:
        parser.error("sequence must be 1..99")
    if args.timeout < 10_000:
        parser.error("timeout must be at least 10000 ms")
    if args.steps is not None and args.steps < 1:
        parser.error("steps must be positive")
    return args


def safe_stem(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:60] or "name"


def register_plugin(db_path: Path) -> None:
    """Register NightCafe as a media plugin and bind its event-bus route."""
    with connect_database(db_path) as db:
        db.execute(
            """INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active)
               VALUES (?,?,?,?,1) ON CONFLICT(plugin_name) DO UPDATE SET
               executable_path=excluded.executable_path,icon=excluded.icon,is_active=1""",
            (PLUGIN_NAME, "media", str(Path(__file__).resolve()), "🌙"),
        )
        db.execute(
            """INSERT INTO event_routes(event_type,target_plugin_name) VALUES (?,?)
               ON CONFLICT(event_type) DO UPDATE SET target_plugin_name=excluded.target_plugin_name""",
            ("NIGHTCAFE_GENERATE", PLUGIN_NAME),
        )
        db.commit()


def register_asset(db_path: Path, path: Path, prompt: str, metadata: dict[str, object]) -> int:
    external_id = hashlib.sha256(path.read_bytes()).hexdigest()
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with connect_database(db_path) as db:
        db.execute(
            """INSERT INTO media_sources(source_key,display_name,provider,root_path,config,is_active,last_indexed_at)
               VALUES (?,?,?,?,?,1,CURRENT_TIMESTAMP) ON CONFLICT(source_key) DO UPDATE SET
               display_name=excluded.display_name,root_path=excluded.root_path,is_active=1,
               last_indexed_at=CURRENT_TIMESTAMP""",
            (SOURCE_KEY, "NightCafe - 99 Names", "nightcafe", str(path.parent), json.dumps({"managed": True})),
        )
        source_id = int(db.execute("SELECT id FROM media_sources WHERE source_key=?", (SOURCE_KEY,)).fetchone()[0])
        db.execute(
            """INSERT INTO media_assets(source_id,external_id,filename,file_path,thumbnail_path,
                   media_type,mime_type,file_size,modified_at,prompt,metadata,is_available,indexed_at)
               VALUES (?,?,?,?,?,'image',?,?,?,?,?,1,CURRENT_TIMESTAMP)
               ON CONFLICT(source_id,external_id) DO UPDATE SET
                   file_path=excluded.file_path,thumbnail_path=excluded.thumbnail_path,
                   prompt=excluded.prompt,metadata=excluded.metadata,is_available=1,indexed_at=CURRENT_TIMESTAMP""",
            (source_id, external_id, path.name, str(path), str(path), mime_type, path.stat().st_size,
             datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(), prompt,
             json.dumps(metadata, ensure_ascii=False)),
        )
        asset_id = int(db.execute(
            "SELECT id FROM media_assets WHERE source_id=? AND external_id=?", (source_id, external_id)
        ).fetchone()[0])
        db.commit()
        return asset_id


def validated_auth(path: Path) -> Path:
    """Validate a private Playwright storage-state file and its cookie freshness."""
    auth = path.expanduser().resolve()
    if not auth.is_file():
        raise PermissionError(f"NightCafe authentication required: missing {auth}")
    mode = stat.S_IMODE(auth.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"NightCafe auth state must have mode 0600 (found {mode:o})")
    try:
        state = json.loads(auth.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"NightCafe auth state is not valid JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise ValueError("NightCafe auth state must be a JSON object")
    cookies = state.get("cookies")
    origins = state.get("origins", [])
    if not isinstance(cookies, list):
        raise ValueError("NightCafe auth state must contain a cookies array")
    if origins and not isinstance(origins, list):
        raise ValueError("NightCafe auth state origins must be an array when present")
    if not cookies:
        raise PermissionError("NightCafe auth state contains no cookies")
    required_domains = ("nightcafe",)
    if any(not isinstance(cookie, dict) for cookie in cookies):
        raise ValueError("NightCafe auth cookies must be objects")
    if any(not isinstance(cookie.get("name"), str) or not cookie["name"] or not isinstance(cookie.get("value"), str) or not isinstance(cookie.get("domain"), str) for cookie in cookies):
        raise ValueError("NightCafe auth cookies require name, value, and domain strings")
    relevant = [cookie for cookie in cookies if any(domain in cookie["domain"] for domain in required_domains)]
    if not relevant:
        raise PermissionError("NightCafe auth state contains no NightCafe cookie")
    import time
    expiring = [cookie for cookie in relevant if cookie.get("expires") is not None and float(cookie.get("expires", -1)) > 0]
    if expiring and all(float(cookie["expires"]) <= time.time() for cookie in expiring):
        raise PermissionError("NightCafe authentication cookies have expired")
    return auth


def load_event(db_path: Path, event_id: int) -> dict[str, object]:
    with connect_database(db_path) as db:
        row = db.execute("SELECT payload FROM events_queue WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise LookupError(f"event {event_id} does not exist")
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    return payload


def dismiss_overlays(page: object, timeout: int = 2_000) -> int:
    """Dismiss known consent/support widgets without hiding creator dialogs."""
    dismissed = 0
    close_names = re.compile(r"close|dismiss|not now|later|reject optional", re.I)
    try:
        close = find_control(
            page,
            roles=(("button", close_names), ("link", close_names)),
            labels=(close_names,),
            css=("[aria-label*='close' i]", "[data-testid*='close' i]"),
            timeout=timeout,
        )
        close.click()
        dismissed += 1
    except Exception:
        pass
    selectors = (
        "#onetrust-banner-sdk", "[id*='cookie' i]", "[class*='cookie' i]",
        "[id*='community-support' i]", "[class*='community-support' i]",
        "[id*='support-widget' i]", "[class*='support-widget' i]",
        "[id*='intercom' i]", "[class*='intercom' i]",
    )
    try:
        page.evaluate(
            """selectors => selectors.forEach(selector => document.querySelectorAll(selector).forEach(node => {
                node.setAttribute('aria-hidden', 'true');
                node.style.setProperty('display', 'none', 'important');
            }))""",
            list(selectors),
        )
    except Exception:
        pass
    return dismissed


def clear_blocking_overlays(page: object) -> int:
    """Clear modal/onboarding/consent layers before a generation click.

    The creator occasionally mounts an empty ``#modals`` host that still
    intercepts pointer events.  Try the user-visible Escape/close paths first,
    then hide only known blocking containers as a final, scoped fallback.
    """
    dismissed = dismiss_overlays(page)
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            dismissed += 1
        except Exception:
            break
    selectors = [
        "#modals", "[role='dialog'][aria-modal='true']", "[aria-modal='true']",
        "[data-testid*='modal' i]", "[data-testid*='onboarding' i]",
        "[id*='onboarding' i]", "[class*='onboarding' i]",
        "[id*='cookie' i]", "[class*='cookie' i]",
    ]
    try:
        page.evaluate(
            """selectors => selectors.forEach(selector => document.querySelectorAll(selector).forEach(node => {
                if (node instanceof HTMLElement) {
                    node.setAttribute('aria-hidden', 'true');
                    node.style.setProperty('pointer-events', 'none', 'important');
                    node.style.setProperty('display', 'none', 'important');
                }
            }))""",
            selectors,
        )
    except Exception:
        pass
    return dismissed


def configure_generation_options(page: object, args: argparse.Namespace) -> None:
    """Best-effort configuration of optional NightCafe generation controls."""
    if args.model:
        try:
            find_control(page, roles=(("button", re.compile(re.escape(args.model), re.I)),),
                         labels=(re.compile(re.escape(args.model), re.I),), timeout=5_000).click()
            LOGGER.info("Model selected: %s", args.model)
        except Exception as exc:
            LOGGER.warning("Model control not available (%s): %s", args.model, exc)
    if args.aspect_format:
        try:
            find_control(page, roles=(("button", re.compile(re.escape(args.aspect_format), re.I)),),
                         labels=(re.compile(re.escape(args.aspect_format), re.I),), timeout=5_000).click()
            LOGGER.info("Aspect format selected: %s", args.aspect_format)
        except Exception as exc:
            LOGGER.warning("Format control not available (%s): %s", args.aspect_format, exc)
    if args.negative_prompt:
        try:
            field = find_control(page, labels=(re.compile(r"negative\s+prompt", re.I),),
                                 css=("textarea[placeholder*='negative' i]", "input[placeholder*='negative' i]"), timeout=3_000)
            field.fill(args.negative_prompt)
            LOGGER.info("Negative prompt configured")
        except Exception as exc:
            LOGGER.warning("Negative-prompt control not available: %s", exc)
    if args.steps is not None:
        try:
            field = find_control(page, labels=(re.compile(r"steps?", re.I),),
                                 css=("input[name*='step' i]", "input[type='number']"), timeout=3_000)
            field.fill(str(args.steps))
            LOGGER.info("Steps configured: %d", args.steps)
        except Exception as exc:
            LOGGER.warning("Steps control not available: %s", exc)


def click_with_overlay_fallback(control: object) -> None:
    """Escalate a click only on the already-selected Create control."""
    try:
        control.click()
        return
    except Exception as normal_error:
        LOGGER.warning("Create click intercepted; retrying with force click: %s", normal_error)
    try:
        control.click(force=True)
        return
    except Exception as force_error:
        LOGGER.warning("Force click failed; invoking DOM click fallback: %s", force_error)
    try:
        control.evaluate("element => element.click()")
    except Exception as js_error:
        raise RuntimeError(f"Create action remained blocked by an overlay: {js_error}") from js_error


def navigate_to_create_surface(page: object, timeout: int, *, reuse_existing: bool = False) -> None:
    """Open the creator from the authenticated overview before locating its prompt field."""
    home_url = os.getenv("NIGHTCAFE_HOME_URL", "https://creator.nightcafe.studio/")
    create_url = os.getenv("NIGHTCAFE_CREATE_URL", "https://creator.nightcafe.studio/create")
    current_url = str(getattr(page, "url", ""))
    if reuse_existing and current_url.startswith("about:blank"):
        raise RuntimeError("CDP session has no active NightCafe page; open NightCafe in the attached browser first")
    on_create_surface = "/create" in current_url.casefold()
    if not (reuse_existing and "nightcafe" in current_url.casefold() and on_create_surface):
        page.goto(home_url, wait_until="domcontentloaded", timeout=timeout)
    else:
        LOGGER.info("Reusing active NightCafe page without navigating from about:blank: %s", current_url.split("?", 1)[0])
    try:
        title = str(page.title())
        body = str(page.locator("body").inner_text(timeout=2_000))
    except Exception:
        title, body = "", ""
    if "just a moment" in title.casefold() or "security verification" in body.casefold() or "cloudflare" in body.casefold():
        raise RuntimeError("Cloudflare bot verification blocked the NightCafe overview; complete it in a real browser session")
    # A session may already land on the creator surface after authentication.
    try:
        find_prompt_input(page, timeout=min(timeout, 3_000))
        return
    except LookupError:
        pass
    dismiss_overlays(page)
    create_button = find_control(
        page,
        roles=(("button", re.compile(r"^(?:create|start creating|new creation|generate)$", re.I)),
               ("link", re.compile(r"^(?:create|start creating|new creation|generate)$", re.I))),
        labels=(re.compile(r"create|start creating|new creation|generate", re.I),),
        css=("a[href*='/create']", "a[href*='/studio']", "button[data-testid*='create' i]"),
        timeout=timeout,
    )
    before_url = str(getattr(page, "url", ""))
    click_with_overlay_fallback(create_button)
    try:
        page.wait_for_url(lambda url: str(url) != before_url, timeout=max(1_000, timeout // 2))
    except Exception:
        # Some SPAs keep the same URL; the prompt lookup below is the confirmation.
        pass
    try:
        find_prompt_input(page, timeout=max(1_000, timeout // 2))
    except LookupError:
        page.goto(create_url, wait_until="domcontentloaded", timeout=timeout)


def browser_context_options() -> dict[str, object]:
    """Return compatibility options without modifying browser fingerprints or webdriver flags."""
    options: dict[str, object] = {
        "viewport": {
            "width": int(os.getenv("NIGHTCAFE_VIEWPORT_WIDTH", "1365")),
            "height": int(os.getenv("NIGHTCAFE_VIEWPORT_HEIGHT", "768")),
        },
        "locale": os.getenv("NIGHTCAFE_LOCALE", "en-US"),
        "timezone_id": os.getenv("NIGHTCAFE_TIMEZONE", "Europe/Paris"),
    }
    configured_ua = os.getenv("NIGHTCAFE_USER_AGENT", "").strip()
    if configured_ua:
        options["user_agent"] = configured_ua
    return options


def browser_launch_args() -> list[str]:
    """Return managed-browser flags needed on restricted Linux hosts.

    Chromium's sandbox remains enabled by default.  Set NIGHTCAFE_NO_SANDBOX=1
    only for containers/hosts where the kernel denies the sandbox namespace.
    """
    value = os.getenv("NIGHTCAFE_NO_SANDBOX", "0").strip().casefold()
    return ["--no-sandbox"] if value in {"1", "true", "yes", "on"} else []


def has_external_session(args: argparse.Namespace) -> bool:
    """Whether a user-owned browser session supplies the authentication state."""
    return bool(args.cdp_url or args.user_data_dir or os.getenv("NIGHTCAFE_CDP_URL") or os.getenv("NIGHTCAFE_USER_DATA_DIR"))


def select_cdp_page(context: object) -> object:
    """Select the existing creator.nightcafe.studio tab; never use a blank page."""
    pages = list(getattr(context, "pages", ()))
    for index, page in enumerate(pages):
        url = str(getattr(page, "url", ""))
        LOGGER.debug("CDP tab[%d] URL=%s", index, url.split("?", 1)[0])
        if "creator.nightcafe.studio" in url.casefold():
            LOGGER.info("Using existing CDP NightCafe tab[%d]: %s", index, url.split("?", 1)[0])
            return page
    raise RuntimeError(
        "CDP connected, but no existing tab contains creator.nightcafe.studio; "
        "open NightCafe in Chrome first"
    )


def connect_cdp_with_retry(chromium: object, cdp_url: str, wait_seconds: float) -> object:
    """Attach to a user-owned CDP endpoint, tolerating delayed browser startup."""
    if wait_seconds < 0:
        raise ValueError("cdp wait must be non-negative")
    deadline = time.monotonic() + wait_seconds
    last_error: Exception | None = None
    while True:
        try:
            return chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
    raise TimeoutError(f"CDP endpoint unavailable after {wait_seconds:.1f}s: {last_error}")


def managed_context_options(auth_path: Path) -> dict[str, object]:
    """Build managed-context options only after validating the persisted auth state."""
    auth = validated_auth(auth_path)
    LOGGER.info("Managed browser will reuse Playwright storage state: %s", auth)
    return {"storage_state": str(auth), **browser_context_options()}


def launch_managed_context(playwright: object, args: argparse.Namespace) -> tuple[object, object]:
    """Launch a controlled Chromium and load the existing NightCafe session."""
    browser = playwright.chromium.launch(headless=args.headless, args=browser_launch_args())
    try:
        context = browser.new_context(**managed_context_options(args.auth))
    except Exception:
        browser.close()
        raise
    return browser, context


def write_mock(output: Path, metadata: dict[str, object]) -> Path:
    serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
    # PNG readers ignore trailing bytes; the digest remains unique per simulated daily asset.
    output.write_bytes(MOCK_PNG + b"\n" + serialized)
    output.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def validate_image_file(path: Path) -> None:
    """Reject empty/placeholder downloads before they enter the Media Store."""
    if not path.is_file() or path.stat().st_size < 256:
        raise RuntimeError(f"generated image is empty or too small: {path}")
    header = path.read_bytes()[:16]
    suffix = path.suffix.casefold()
    signatures = {
        ".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",), ".webp": (b"RIFF",),
    }
    if suffix not in signatures or not any(header.startswith(item) for item in signatures[suffix]):
        raise RuntimeError(f"generated image failed format validation: {path}")
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.width < 32 or image.height < 32:
                raise RuntimeError(f"generated image is only {image.width}x{image.height}: {path}")
    except ImportError:
        LOGGER.debug("Pillow unavailable; magic-byte validation used for %s", path)


RESULT_SELECTOR = "img[src*='images.nightcafe.studio/jobs/'], img[src*='nightcafe.studio/jobs/'], img[alt*='creation' i]"


def wait_for_rendered_result(page: object, timeout: int, *, exclude_sources: set[str] | None = None) -> None:
    """Wait until a result image is decoded at a useful resolution, not a placeholder."""
    excluded = list(exclude_sources or ())
    page.wait_for_function(
        """({selector, excluded}) => Array.from(document.querySelectorAll(selector)).some(img =>
            img.complete && img.naturalWidth >= 32 && img.naturalHeight >= 32 &&
            !excluded.includes(img.currentSrc || img.src))""",
        arg={"selector": RESULT_SELECTOR, "excluded": excluded},
        timeout=timeout,
    )


def run_live(args: argparse.Namespace, output: Path) -> tuple[Path, str | None]:
    from playwright.sync_api import sync_playwright

    trace_path: str | None = None
    with sync_playwright() as playwright:
        browser = None
        owns_browser = False
        owns_context = False
        cdp_url = args.cdp_url or os.getenv("NIGHTCAFE_CDP_URL")
        profile_dir = args.user_data_dir or (Path(os.getenv("NIGHTCAFE_USER_DATA_DIR")) if os.getenv("NIGHTCAFE_USER_DATA_DIR") else None)
        if cdp_url:
            LOGGER.info("Attaching to user-owned Chrome over CDP: %s", cdp_url.split("?", 1)[0])
            try:
                browser = connect_cdp_with_retry(playwright.chromium, cdp_url, args.cdp_wait_seconds)
            except Exception as exc:
                LOGGER.warning("CDP unavailable; using managed browser fallback: %s", exc)
                browser = None
            if browser is None:
                if profile_dir:
                    profile_dir = Path(profile_dir).expanduser().resolve()
                    profile_dir.mkdir(parents=True, exist_ok=True)
                    context = playwright.chromium.launch_persistent_context(
                        str(profile_dir), headless=False, **browser_context_options()
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                    owns_context = True
                else:
                    browser, context = launch_managed_context(playwright, args)
                    page = context.new_page()
                    owns_browser = True
                    owns_context = True
            else:
                context = browser.contexts[0] if browser.contexts else browser.new_context(**browser_context_options())
                page = select_cdp_page(context)
            LOGGER.info("Browser context ready: cdp=%s pages=%d url=%s", bool(cdp_url and browser), len(context.pages), str(getattr(page, "url", "")))
        elif profile_dir:
            profile_dir = Path(profile_dir).expanduser().resolve()
            profile_dir.mkdir(parents=True, exist_ok=True)
            LOGGER.info("Opening user-owned persistent profile in headed mode: %s", profile_dir)
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir), headless=False, **browser_context_options()
            )
            page = context.pages[0] if context.pages else context.new_page()
            owns_context = True
            LOGGER.info("Persistent browser context ready: pages=%d url=%s", len(context.pages), str(getattr(page, "url", "")))
        else:
            browser, context = launch_managed_context(playwright, args)
            page = context.new_page()
            owns_browser = True
            owns_context = True
            LOGGER.info("Managed storage-state context ready: pages=%d url=%s", len(context.pages), str(getattr(page, "url", "")))
        try:
            navigate_to_create_surface(page, args.timeout, reuse_existing=bool(cdp_url and browser and not owns_browser))
            if args.claim_daily:
                page.goto("https://creator.nightcafe.studio/notifications", wait_until="domcontentloaded", timeout=args.timeout)
                try:
                    find_control(
                        page,
                        roles=(("button", re.compile(r"claim|daily (?:top up|credits)", re.I)),),
                        timeout=4_000,
                    ).click()
                    LOGGER.info("Daily credit top-up claim submitted")
                except Exception:
                    LOGGER.info("No claimable daily top-up was visible")
                navigate_to_create_surface(page, args.timeout, reuse_existing=bool(cdp_url and browser and not owns_browser))
            prompt_box = find_prompt_input(page, timeout=15_000)
            LOGGER.info("Prompt input located; filling prompt (%d characters)", len(args.prompt))
            prompt_box.fill(args.prompt)
            configure_generation_options(page, args)
            LOGGER.info("Prompt input filled; resolving Create/Generate control")
            generate_name = re.compile(r"^(?:create(?:\s+[\d.,]+)?|generate(?:\s+[\d.,]+)?)$", re.I)
            # The sidebar also has an exact "Create" button. Prefer the last
            # visible matching control, which is the generator submit button.
            role_matches = page.get_by_role("button", name=generate_name)
            create_control = role_matches.last if role_matches.count() > 1 else role_matches.first
            try:
                create_control.wait_for(state="visible", timeout=15_000)
            except Exception:
                create_control = find_control(
                    page, roles=(("button", generate_name),),
                    css=("main button[type='submit']",), timeout=15_000,
                )
            clear_blocking_overlays(page)
            LOGGER.info("Blocking overlays cleared; clicking Create/Generate")
            existing_sources: set[str] = set()
            try:
                existing_sources = set(page.locator(RESULT_SELECTOR).evaluate_all("els => els.map(e => e.currentSrc || e.src)"))
            except Exception:
                pass
            click_with_overlay_fallback(create_control)
            LOGGER.info("Create/Generate clicked; waiting up to %d ms for rendered result", args.generation_timeout)
            wait_for_rendered_result(page, args.generation_timeout, exclude_sources=existing_sources)
            images = page.locator(RESULT_SELECTOR)
            result = None
            for index in range(images.count()):
                candidate = images.nth(index)
                try:
                    src = candidate.get_attribute("src") or ""
                    if src not in existing_sources and candidate.is_visible():
                        result = candidate
                        break
                except Exception:
                    continue
            if result is None:
                raise RuntimeError("NightCafe render appeared, but no new job image was found")
            LOGGER.info("Rendered result image detected with usable dimensions")
            source = result.get_attribute("src")
            if not source:
                raise RuntimeError("NightCafe result image has no downloadable source URL")
            try:
                LOGGER.info("Opening rendered thumbnail in detail view")
                try:
                    result.click(timeout=10_000)
                except Exception:
                    result.click(force=True, timeout=10_000)
                # Allow the detail route/modal to mount before resolving controls.
                page.wait_for_timeout(750)
                detail_scope = page
                try:
                    dialogs = page.locator("[role='dialog'], [aria-modal='true'], [data-testid*='modal' i]")
                    if dialogs.count():
                        detail_scope = dialogs.last
                except Exception:
                    pass
                LOGGER.info("Detail view opened; resolving download/options control")
                try:
                    options_control = find_control(
                        detail_scope,
                        roles=(("button", re.compile(r"^(?:more|options|actions?)$", re.I)),),
                        labels=(re.compile(r"more|options|download", re.I),),
                        css=("button[aria-label*='more' i]", "button[aria-label*='option' i]"),
                        timeout=3_000,
                    )
                    options_control.click()
                    LOGGER.info("Detail options opened")
                except Exception:
                    LOGGER.debug("No extra detail options menu required")
                download_button = find_control(
                    detail_scope,
                    roles=(("button", re.compile(r"^download(?:\s+(?:image|full(?:\s+size)?|original))?$", re.I)),
                           ("link", re.compile(r"^download(?:\s+(?:image|full(?:\s+size)?|original))?$", re.I))),
                    labels=(re.compile(r"download(?:\s+image|\s+full(?:\s+size)?|\s+original)?", re.I),),
                    css=("a[download]", "button[aria-label*='download' i]", "[data-testid*='download' i]"),
                    timeout=10_000,
                )
                with page.expect_download(timeout=15_000) as download_info:
                    download_button.click()
                download = download_info.value
                suffix = Path(download.suggested_filename).suffix.lower()
                if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
                    output = output.with_suffix(suffix)
                download.save_as(str(output))
                validate_image_file(output)
                LOGGER.info("Downloaded and validated image: %s (%d bytes)", output, output.stat().st_size)
                return output, source
            except Exception:
                LOGGER.info("Download control unavailable; using authenticated image response")
            response = context.request.get(source, timeout=args.timeout)
            if not response.ok:
                raise RuntimeError(f"NightCafe image download returned HTTP {response.status}")
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            suffix = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/png": ".png"}.get(content_type)
            if suffix:
                output = output.with_suffix(suffix)
            output.write_bytes(response.body())
            validate_image_file(output)
            LOGGER.info("Fetched and validated image response: %s (%d bytes)", output, output.stat().st_size)
            return output, source
        except Exception:
            trace = capture_page_trace(
                page, PROJECT_ROOT / "vault/media/traces", "nightcafe", "prompt-selection"
            )
            LOGGER.error("NightCafe DOM trace saved: screenshot=%s html=%s", trace["screenshot"], trace["html"])
            diagnostic = capture_sanitized_diagnostic(
                page, PROJECT_ROOT / "vault/logs/screenshots", "nightcafe", "generation"
            )
            trace_path = diagnostic["trace"]
            raise
        finally:
            if owns_context:
                context.close()
            if owns_browser and browser is not None:
                browser.close()


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    args.db = args.db.expanduser().resolve()
    register_plugin(args.db)
    if args.register and args.event_id is None and args.prompt is None:
        print(json.dumps({"status": "REGISTERED", "plugin": PLUGIN_NAME, "event_type": "NIGHTCAFE_GENERATE"}))
        return 0
    if args.event_id is not None:
        payload = load_event(args.db, args.event_id)
        args.prompt = args.prompt or str(payload.get("prompt") or payload.get("content") or "")
        args.model = args.model or payload.get("model")
        args.aspect_format = args.aspect_format or payload.get("format") or payload.get("aspect_ratio")
        args.negative_prompt = args.negative_prompt or str(payload.get("negative_prompt") or "")
        args.steps = args.steps or payload.get("steps")
        args.name = args.name or str(payload.get("name") or "Daily subject")
        args.sequence = args.sequence or int(payload.get("sequence", 1))
        args.run_date = args.run_date or str(payload.get("run_date") or datetime.now(timezone.utc).date().isoformat())
    if not all((args.prompt, args.name, args.sequence, args.run_date)):
        raise SystemExit("--prompt, --name, and --run-date are required unless --event_id supplies them")
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{args.run_date}_{args.sequence:02d}_{safe_stem(args.name)}.png"
    metadata: dict[str, object] = {
        "name": args.name, "sequence": args.sequence, "run_date": args.run_date,
        "prompt": args.prompt, "generator": PLUGIN_NAME,
        "model": args.model, "format": args.aspect_format,
        "negative_prompt": args.negative_prompt, "steps": args.steps,
    }
    try:
        if args.simulate:
            metadata["mode"] = "SIMULATED"
            write_mock(output, metadata)
            status = "SIMULATED"
        else:
            if not has_external_session(args):
                validated_auth(args.auth)
            if not args.live:
                raise PermissionError("NightCafe live generation requires --live; use --simulate only for explicit tests")
            output, platform_url = run_live(args, output)
            metadata.update({"mode": "LIVE", "platform_url": platform_url})
            output.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            status = "COMPLETED"
        asset_id = register_asset(args.db, output, args.prompt, metadata)
        result = {"status": status, "asset_id": asset_id, "output_path": str(output)}
        if args.event_id is not None:
            emit_result(status, result=result, payload_patch={"nightcafe": result})
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        LOGGER.exception("NightCafe generation failed")
        blocked = isinstance(exc, (PermissionError, ValueError)) and ("auth" in str(exc).lower() or "authentication" in str(exc).lower())
        outcome = "BLOCKED_AUTH" if blocked else "FAILED"
        result = {"status": "AUTH_REQUIRED" if blocked else "FAILED", "error": str(exc)}
        if args.event_id is not None:
            emit_result(outcome, result=result, error=str(exc), retryable=False)
            return 0
        print(json.dumps({"status": outcome, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
