"""Substack analytics facade backed by the production Pro adapter."""
from __future__ import annotations
from typing import Any
from plugins.channels.pub_substack_pro import scrape_analytics, save_artifacts

def collect_substack_stats(page: Any, payload: dict[str, Any] | None = None, *, limit: int = 10) -> dict[str, Any]:
    """Inspect Substack publication stats with Playwright and persist JSON/Markdown."""
    data = scrape_analytics(page, payload or {}, limit)
    if isinstance(data, list): data = {"platform": "substack", "posts": data}
    save_artifacts(__import__('pathlib').Path(__file__).resolve().parents[2] / 'vault' / 'analytics', 'substack_analytics', data)
    return data
