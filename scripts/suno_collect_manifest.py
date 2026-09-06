#!/usr/bin/env python3
"""Collect authenticated Suno song detail URLs from the active CDP tabs."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "vault/media/suno/song_manifest.json"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    urls = {}
    for context in browser.contexts:
        for page in context.pages:
            if "suno.com" not in page.url:
                continue
            try:
                page.mouse.wheel(0, 100000)
                page.wait_for_timeout(800)
                for href in page.locator('a[href*="/song/"]').evaluate_all("els => els.map(e => e.href)"):
                    urls[href] = True
            except Exception:
                continue
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(sorted(urls), indent=2), encoding="utf-8")
    print(json.dumps({"status": "COLLECTED", "songs": len(urls), "manifest": str(OUT)}))
