#!/usr/bin/env python3
"""Read-only LinkedIn analytics provider for the normalized analytics pipeline.

This module deliberately contains no event routing or queue handling.  The
Website Analytics dispatcher owns the generic analytics events and invokes this
provider when ``provider=linkedin`` is requested.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from plugins.analytics.base import AnalyticsProvider, CollectionTarget


LINKEDIN_HOSTS = {"linkedin.com", "www.linkedin.com"}
GENERIC_LINKEDIN_PATHS = {"/feed", "/posts", "/company", "/in"}


class LinkedInAnalyticsProviderError(RuntimeError):
    """Base class for safe, read-only LinkedIn collection failures."""


class LinkedInAuthRequired(LinkedInAnalyticsProviderError):
    pass


class LinkedInInvalidResponse(LinkedInAnalyticsProviderError):
    pass


class LinkedInTemporaryError(LinkedInAnalyticsProviderError):
    pass


def is_publication_specific_linkedin_url(value: str | None) -> bool:
    """Return whether a LinkedIn URL identifies one publication, not a feed."""
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in LINKEDIN_HOSTS:
        return False
    path = parsed.path.rstrip("/") or "/"
    lowered = path.lower()
    if lowered in GENERIC_LINKEDIN_PATHS:
        return False
    if "/recent-activity" in lowered or "/admin/page-posts" in lowered:
        return False
    if lowered.startswith("/company/") and lowered.endswith("/posts"):
        return False
    if lowered.startswith("/company/") and lowered.endswith("/about"):
        return False
    # A specific post normally has an identifier after /posts/ or an activity
    # URN in /feed/update/. Other broad profile/company pages are not enough.
    return "/posts/" in lowered or "/feed/update/" in lowered or "urn:li:activity:" in value


def validate_linkedin_url(value: str | None) -> str:
    """Accept only HTTPS LinkedIn URLs identifying one publication."""
    if not value:
        raise LinkedInInvalidResponse("a LinkedIn post URL is required")
    if not is_publication_specific_linkedin_url(value):
        raise LinkedInInvalidResponse("publication-specific LinkedIn identity is required")
    return value


def parse_metric(value: Any) -> int | None:
    """Parse common LinkedIn count formats without turning unknown into zero."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace("\u00a0", " ")
    if not text:
        return None
    match = re.search(r"([0-9][0-9.,]*)\s*([KkMm])?", text)
    if not match:
        return None
    number = match.group(1)
    suffix = (match.group(2) or "").lower()
    # LinkedIn commonly uses comma thousands separators; accept localized
    # decimal abbreviations such as 1,2K as well.
    if suffix:
        if "," in number and "." not in number and len(number.rsplit(",", 1)[-1]) <= 2:
            base = float(number.replace(",", "."))
        elif "." in number and "," not in number and len(number.rsplit(".", 1)[-1]) <= 2:
            base = float(number)
        else:
            base = float(number.replace(",", "").replace(".", ""))
        return int(base * (1000 if suffix == "k" else 1_000_000))
    # A four digit value with punctuation is a count, not a decimal metric.
    return int(number.replace(",", "").replace(".", ""))


def normalize_linkedin_metrics(source: dict[str, Any]) -> dict[str, int]:
    """Normalize only metrics LinkedIn explicitly exposed in ``source``."""
    aliases = {
        "views": ("views", "view_count"),
        "impressions": ("impressions", "impression_count"),
        "reactions": ("reactions", "reaction_count"),
        "likes": ("likes", "like_count"),
        "comments": ("comments", "comment_count"),
        "shares": ("shares", "share_count", "reposts"),
        "clicks": ("clicks", "click_count"),
    }
    normalized: dict[str, int] = {}
    for metric, keys in aliases.items():
        for key in keys:
            if key in source:
                parsed = parse_metric(source[key])
                if parsed is not None:
                    normalized[metric] = parsed
                break
    return normalized


def _card_metric(card: Any, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        try:
            locator = card.locator(selector).first
            if locator.is_visible(timeout=500):
                text = locator.inner_text(timeout=500).strip()
                if text:
                    return text
        except Exception:
            continue
    return None


class LinkedInAnalyticsProvider:
    """Collect cumulative, publication-level counts using the existing session."""

    name = "linkedin"

    def collect(self, target: CollectionTarget) -> dict[str, Any]:
        metadata = target.metadata or {}
        auth_path = Path(metadata.get("auth_path", ""))
        # Import lazily so simulated collection and unit tests never import or
        # launch Playwright.
        from plugins.channels.pub_linkedin_pro import browser_page, validated_auth

        # Reject generic feed/list identities before touching the browser or
        # auth state, preventing accidental first-card attribution.
        url = validate_linkedin_url(target.canonical_url)
        try:
            auth = validated_auth(auth_path)
        except (PermissionError, ValueError, OSError) as exc:
            raise LinkedInAuthRequired(str(exc)) from exc
        if auth is None:
            raise LinkedInAuthRequired("LinkedIn storage state is missing")

        try:
            with browser_page(auth, bool(metadata.get("headless", True))) as page:
                page.goto(url, wait_until="domcontentloaded")
                if "login" in str(page.url).lower() or "checkpoint" in str(page.url).lower():
                    raise LinkedInAuthRequired("LinkedIn session requires authentication")
                validate_linkedin_url(str(page.url))
                cards = page.locator("article, div.feed-shared-update-v2")
                card = cards.first
                card.wait_for(state="visible", timeout=int(metadata.get("timeout_ms", 30000)))
                metrics = normalize_linkedin_metrics(
                    {
                        "views": _card_metric(card, ("text=/[0-9.,]+\\s+(views|weergaven)/i",)),
                        "reactions": _card_metric(card, (".social-details-social-counts__reactions-count",)),
                        "comments": _card_metric(card, (".social-details-social-counts__comments",)),
                        "shares": _card_metric(card, (".social-details-social-counts__shares",)),
                    }
                )
                if not metrics:
                    raise LinkedInInvalidResponse("LinkedIn post metrics were not visible")
        except LinkedInAnalyticsProviderError:
            raise
        except TimeoutError as exc:
            raise LinkedInTemporaryError("LinkedIn analytics page timed out") from exc
        except Exception as exc:
            if exc.__class__.__name__ == "TimeoutError":
                raise LinkedInTemporaryError("LinkedIn analytics page timed out") from exc
            # Do not expose page contents, cookies or arbitrary exception data.
            raise LinkedInInvalidResponse("LinkedIn analytics page could not be read") from exc

        # The normalized provider stores counts only.  In particular, no
        # commenter names/text or browser/session state is retained.
        return {
            "provider": self.name,
            "metrics": metrics,
            "raw": {"provider": self.name, "metrics": metrics, "source": "post_card"},
        }


__all__ = [
    "LinkedInAnalyticsProvider",
    "LinkedInAnalyticsProviderError",
    "LinkedInAuthRequired",
    "LinkedInInvalidResponse",
    "LinkedInTemporaryError",
    "normalize_linkedin_metrics",
    "parse_metric",
    "is_publication_specific_linkedin_url",
    "validate_linkedin_url",
]
