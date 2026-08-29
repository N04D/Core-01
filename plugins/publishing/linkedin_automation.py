"""LinkedIn analytics facade backed by the production Pro adapter."""
from __future__ import annotations
from typing import Any
from plugins.channels.pub_linkedin_pro import scrape_analytics, save_artifact

def collect_linkedin_stats(page: Any, *, company_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Inspect recent LinkedIn posts with Playwright and persist JSON/Markdown."""
    posts = scrape_analytics(page, company_id, limit)
    data = {"platform": "linkedin", "posts": posts}
    save_artifact(__import__('pathlib').Path(__file__).resolve().parents[2] / 'vault' / 'analytics', 'linkedin_analytics', data)
    return data
