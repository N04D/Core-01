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
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.browser_robustness import capture_sanitized_diagnostic, find_control
from core.database import connect_database

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DB: Final = PROJECT_ROOT / "db" / "events.db"
DEFAULT_AUTH: Final = PROJECT_ROOT / "config" / "nightcafe_auth.json"
DEFAULT_OUTPUT: Final = PROJECT_ROOT / "vault" / "media" / "nightcafe"
SOURCE_KEY: Final = "nightcafe-99-names"
PLUGIN_NAME: Final = "NightCafe Daily Stock Generator"
LOGGER = logging.getLogger("nightcafe_automation")
MOCK_PNG: Final = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+Xw4SAAAAAElFTkSuQmCC"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH)
    parser.add_argument("--live", action="store_true", help="Allow a real external generation.")
    parser.add_argument("--claim-daily", action="store_true", help="Claim an available daily top-up in live mode.")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--timeout", type=int, default=180_000)
    args = parser.parse_args()
    if not 1 <= args.sequence <= 99 or args.timeout < 10_000:
        parser.error("sequence must be 1..99 and timeout at least 10000 ms")
    return args


def safe_stem(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:60] or "name"


def register_asset(db_path: Path, path: Path, prompt: str, metadata: dict[str, object]) -> int:
    external_id = hashlib.sha256(path.read_bytes()).hexdigest()
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with connect_database(db_path) as db:
        db.execute(
            """INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active)
               VALUES (?,?,?,?,1) ON CONFLICT(plugin_name) DO UPDATE SET
               executable_path=excluded.executable_path,is_active=1""",
            (PLUGIN_NAME, "media", str(Path(__file__).resolve()), "🌙"),
        )
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


def write_mock(output: Path, metadata: dict[str, object]) -> Path:
    serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
    # PNG readers ignore trailing bytes; the digest remains unique per simulated daily asset.
    output.write_bytes(MOCK_PNG + b"\n" + serialized)
    output.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def run_live(args: argparse.Namespace, output: Path) -> tuple[Path, str | None]:
    from playwright.sync_api import sync_playwright

    trace_path: str | None = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        context = browser.new_context(storage_state=str(args.auth))
        page = context.new_page()
        try:
            page.goto(os.getenv("NIGHTCAFE_CREATE_URL", "https://creator.nightcafe.studio/create"), wait_until="domcontentloaded", timeout=args.timeout)
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
                page.goto(os.getenv("NIGHTCAFE_CREATE_URL", "https://creator.nightcafe.studio/create"), wait_until="domcontentloaded", timeout=args.timeout)
            prompt_box = find_control(
                page,
                roles=(("textbox", re.compile(r"prompt|describe|creation", re.I)),),
                labels=(re.compile(r"prompt|describe|creation", re.I),),
                css=("main textarea", "main [contenteditable='true']"),
                timeout=15_000,
            )
            prompt_box.fill(args.prompt)
            find_control(
                page,
                roles=(("button", re.compile(r"create|generate", re.I)),),
                css=("main button[type='submit']",),
                timeout=15_000,
            ).click()
            result = page.locator("main img[src*='nightcafe'], main img[src*='r2.'], main img[alt*='creation' i]").last
            result.wait_for(state="visible", timeout=args.timeout)
            source = result.get_attribute("src")
            if not source:
                raise RuntimeError("NightCafe result image has no downloadable source URL")
            try:
                download_button = find_control(
                    page,
                    roles=(("button", re.compile(r"download", re.I)), ("link", re.compile(r"download", re.I))),
                    timeout=4_000,
                )
                with page.expect_download(timeout=15_000) as download_info:
                    download_button.click()
                download = download_info.value
                suffix = Path(download.suggested_filename).suffix.lower()
                if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
                    output = output.with_suffix(suffix)
                download.save_as(str(output))
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
            return output, source
        except Exception:
            diagnostic = capture_sanitized_diagnostic(
                page, PROJECT_ROOT / "vault/logs/screenshots", "nightcafe", "generation"
            )
            trace_path = diagnostic["trace"]
            raise
        finally:
            context.close()
            browser.close()


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{args.run_date}_{args.sequence:02d}_{safe_stem(args.name)}.png"
    metadata: dict[str, object] = {
        "name": args.name, "sequence": args.sequence, "run_date": args.run_date,
        "prompt": args.prompt, "generator": PLUGIN_NAME,
    }
    try:
        if not args.live or not args.auth.is_file():
            metadata["mode"] = "SIMULATED"
            metadata["auth_required"] = not args.auth.is_file()
            write_mock(output, metadata)
            status = "SIMULATED"
            LOGGER.warning("Safe simulation used; live=%s auth_present=%s", args.live, args.auth.is_file())
        else:
            output, platform_url = run_live(args, output)
            metadata.update({"mode": "LIVE", "platform_url": platform_url})
            output.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            status = "COMPLETED"
        asset_id = register_asset(args.db, output, args.prompt, metadata)
        print(json.dumps({"status": status, "asset_id": asset_id, "output_path": str(output)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        LOGGER.exception("NightCafe generation failed")
        print(json.dumps({"status": "FAILED", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
