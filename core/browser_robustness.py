#!/usr/bin/env python3
"""Shared accessible selectors, submit verification, and safe diagnostics."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4


def find_control(
    page: Any,
    *,
    roles: Iterable[tuple[str, str | re.Pattern[str]]] = (),
    labels: Iterable[str | re.Pattern[str]] = (),
    css: Iterable[str] = (),
    scope: Any | None = None,
    timeout: int = 30_000,
) -> Any:
    """Find a visible control, preferring accessible roles and labels over CSS."""
    root = scope or page
    candidates: list[Any] = []
    for role, name in roles:
        candidates.append(root.get_by_role(role, name=name).first)
    for label in labels:
        candidates.append(root.get_by_label(label).first)
    candidates.extend(root.locator(selector).first for selector in css)
    if not candidates:
        raise ValueError("at least one selector candidate is required")
    slice_timeout = max(250, timeout // len(candidates))
    for candidate in candidates:
        try:
            candidate.wait_for(state="visible", timeout=slice_timeout)
            return candidate
        except Exception:
            continue
    raise LookupError("no accessible or scoped fallback control became visible")


def find_with_accessible_fallbacks(
    page: Any, selectors: Iterable[str], *, timeout: int = 30_000, scope: Any | None = None
) -> Any:
    """Promote legacy text/ARIA CSS hints to accessible lookups before scoped CSS."""
    css = list(selectors)
    roles: list[tuple[str, str | re.Pattern[str]]] = []
    labels: list[str | re.Pattern[str]] = []
    for selector in css:
        text_match = re.search(r"(button|link):has-text\(['\"](.+?)['\"]\)", selector)
        if text_match:
            roles.append((text_match.group(1), re.compile(re.escape(text_match.group(2)), re.I)))
        aria_match = re.search(r"aria-label\*?=['\"](.+?)['\"]", selector)
        if aria_match:
            labels.append(re.compile(re.escape(aria_match.group(1)), re.I))
    return find_control(
        page, roles=roles, labels=labels, css=css, scope=scope, timeout=timeout
    )


def verify_submission(
    page: Any,
    before_url: str,
    *,
    indicators: Iterable[str],
    timeout: int = 30_000,
) -> str:
    """Require an explicit URL transition or visible confirmation indicator."""
    try:
        page.wait_for_url(lambda url: str(url) != before_url, timeout=timeout // 2)
        return str(page.url)
    except Exception:
        pass
    for selector in indicators:
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=max(500, timeout // 2))
            return str(page.url)
        except Exception:
            continue
    raise RuntimeError("publication submit could not be explicitly confirmed")


def sanitized_url(value: str) -> str:
    """Remove query strings, fragments, credentials, and non-web schemes."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return ""
    hostname = parsed.hostname or ""
    netloc = hostname + (f":{parsed.port}" if parsed.port else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def capture_sanitized_diagnostic(
    page: Any,
    directory: Path,
    platform: str,
    operation: str,
    error: BaseException | None = None,
) -> dict[str, str]:
    """Persist a masked screenshot and metadata-only trace with mode 0600."""
    directory.mkdir(parents=True, exist_ok=True)
    token = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"
    screenshot = directory / f"{platform}_{operation}_{token}.png"
    trace = directory / f"{platform}_{operation}_{token}.json"
    masks = []
    for selector in (
        "input", "textarea", "[contenteditable=true]", "[data-sensitive]",
        "img", "[class*='avatar']", "[class*='profile']", "a[href*='/in/']",
    ):
        try:
            masks.append(page.locator(selector))
        except Exception:
            pass
    page.screenshot(path=str(screenshot), full_page=True, mask=masks, mask_color="#111827")
    metadata = {
        "platform": platform,
        "operation": operation,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "url": sanitized_url(str(getattr(page, "url", ""))),
        "error_type": type(error).__name__ if error else None,
    }
    trace.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(screenshot, 0o600)
    os.chmod(trace, 0o600)
    return {"screenshot": str(screenshot), "trace": str(trace)}
