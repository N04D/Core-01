#!/usr/bin/env python3
"""Normalize channel analytics and flag high-performing evergreen content."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
DEFAULT_ANALYTICS: Final = PROJECT_ROOT / "vault" / "analytics"
LOGGER: Final = logging.getLogger("evergreen")


def parse_time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return fallback


def number(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def engagement(post: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    comments = post.get("comments", [])
    comment_count = len(comments) if isinstance(comments, list) else int(number(comments))
    metrics = {
        "views": number(post.get("views")),
        "likes": number(post.get("likes", post.get("reactions"))),
        "comments": comment_count,
        "shares": number(post.get("shares", post.get("reposts"))),
        "engagement": number(post.get("engagement")),
    }
    score = (
        metrics["views"] * 0.01 + metrics["likes"] * 4 + metrics["comments"] * 6
        + metrics["shares"] * 8 + metrics["engagement"]
    )
    return score, metrics


def platform_for(path: Path, data: dict[str, Any]) -> str:
    explicit = data.get("platform")
    if isinstance(explicit, str) and explicit:
        return explicit.casefold()
    name = path.name.casefold()
    return "linkedin" if "linkedin" in name else "substack" if "substack" in name else "medium" if "medium" in name else "unknown"


def analyze(database: Path, analytics_dir: Path, threshold: float, evergreen_days: int) -> tuple[int, int]:
    processed = evergreen = 0
    now = datetime.now(timezone.utc)
    with sqlite3.connect(database.expanduser().resolve(), timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        for path in sorted(analytics_dir.expanduser().resolve().glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or data.get("mode") not in {None, "analytics"}:
                continue
            posts = data.get("posts", [])
            if not isinstance(posts, list):
                continue
            platform = platform_for(path, data)
            fallback = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            for index, post in enumerate(posts):
                if not isinstance(post, dict):
                    continue
                score, metrics = engagement(post)
                published = parse_time(post.get("published_at", post.get("date")), fallback)
                eligible = published + timedelta(days=evergreen_days)
                stable = str(post.get("id") or post.get("url") or f"{path.resolve()}:{index}")
                external_key = hashlib.sha256(f"{platform}:{stable}".encode()).hexdigest()
                content = post.get("content") or post.get("title")
                source_path = post.get("source_path") or post.get("draft_file")
                flag = int(score >= threshold)
                with connection:
                    connection.execute(
                        """INSERT INTO evergreen_posts(external_key,platform,source_path,analytics_file,
                               title,content,published_at,metrics,engagement_score,is_evergreen,eligible_after,updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                           ON CONFLICT(external_key) DO UPDATE SET analytics_file=excluded.analytics_file,
                               source_path=excluded.source_path,title=excluded.title,content=excluded.content,
                               published_at=excluded.published_at,metrics=excluded.metrics,
                               engagement_score=excluded.engagement_score,is_evergreen=excluded.is_evergreen,
                               eligible_after=excluded.eligible_after,updated_at=CURRENT_TIMESTAMP""",
                        (external_key, platform, source_path, str(path.resolve()), post.get("title"),
                         content, published.isoformat(), json.dumps(metrics), score, flag, eligible.isoformat()),
                    )
                processed += 1
                evergreen += flag
    return processed, evergreen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze analytics for evergreen content.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--analytics-dir", type=Path, default=DEFAULT_ANALYTICS)
    parser.add_argument("--threshold", type=float, default=float(os.getenv("EVERGREEN_SCORE_THRESHOLD", "25")))
    parser.add_argument("--days", type=int, default=int(os.getenv("EVERGREEN_REPURPOSE_DAYS", "90")))
    args = parser.parse_args()
    if args.threshold < 0 or args.days < 1:
        parser.error("threshold must be non-negative and days positive")
    return args


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    try:
        processed, evergreen = analyze(args.db, args.analytics_dir, args.threshold, args.days)
        LOGGER.info("Analytics processed=%s evergreen=%s", processed, evergreen)
        return 0
    except (OSError, sqlite3.Error, ValueError):
        LOGGER.exception("Evergreen analysis failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
