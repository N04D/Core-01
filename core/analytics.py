"""Small, channel-neutral analytics store and normalization helpers.

Providers return dictionaries; this module is the only place that translates
provider fields into the stable Core-01 metric vocabulary and persists history.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

METRIC_NAMES = (
    "views", "impressions", "unique_views", "clicks", "reactions", "likes",
    "comments", "shares", "saves", "engagements", "watch_time_seconds",
    "followers_gained", "followers_lost", "conversions",
)
ALIASES = {
    "views": "views", "pageviews": "views", "visitors": "unique_views", "unique_views": "unique_views", "impressions": "impressions",
    "likes": "likes", "reactions": "reactions", "comments": "comments",
    "shares": "shares", "reposts": "shares", "saves": "saves",
    "engagement": "engagements", "engagements": "engagements", "clicks": "clicks",
    "watch_time": "watch_time_seconds", "watch_time_seconds": "watch_time_seconds",
    "followers_gained": "followers_gained", "followers_lost": "followers_lost",
    "conversions": "conversions",
}
WINDOW_SECONDS = {"24h": 86400, "7d": 604800, "30d": 2592000, "lifetime": None}
MAX_RAW_BYTES = 100_000
SENSITIVE_KEY_PARTS = ("token", "password", "secret", "authorization", "cookie", "api_key")


@dataclass(frozen=True)
class AnalyticsSnapshot:
    provider: str
    channel: str | None
    external_id: str
    canonical_url: str | None
    metrics: dict[str, float]
    raw_payload: dict[str, Any]
    window: str = "lifetime"
    mode: str = "REAL"
    publication_attempt_id: int | None = None
    event_id: int | None = None
    collected_at: str | None = None


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not any(part in str(key).casefold() for part in SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def bounded_json_payload(payload: dict[str, Any], max_bytes: int = MAX_RAW_BYTES) -> tuple[str, str]:
    """Return valid bounded JSON and a hash of the full sanitized payload."""
    sanitized = _sanitize(payload)
    canonical = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = canonical.encode("utf-8")
    source_hash = hashlib.sha256(encoded).hexdigest()
    if len(encoded) <= max_bytes:
        return canonical, source_hash
    diagnostic = {
        "_truncated": True,
        "_original_size": len(encoded),
        "_sha256": source_hash,
        "_preview": canonical[:4096],
    }
    bounded = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(bounded.encode("utf-8")) > max_bytes:
        bounded = json.dumps({"_truncated": True, "_original_size": len(encoded), "_sha256": source_hash}, separators=(",", ":"))
    return bounded, source_hash


def normalize_metrics(payload: dict[str, Any]) -> dict[str, float]:
    """Normalize provider fields while preserving absent-vs-zero semantics."""
    if not isinstance(payload, dict):
        raise ValueError("analytics provider response must be an object")
    source = payload.get("metrics", payload)
    if not isinstance(source, dict):
        raise ValueError("analytics metrics must be an object")
    normalized: dict[str, float] = {}
    for key, value in source.items():
        name = ALIASES.get(str(key).casefold())
        if name is None or value is None:
            continue
        if isinstance(value, bool):
            raise ValueError(f"invalid boolean metric: {key}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid metric value for {key}") from exc
        if number < 0:
            raise ValueError(f"metric value cannot be negative: {key}")
        normalized[name] = number
    return normalized


def _bounds(window: str, collected_at: datetime) -> tuple[str, str]:
    if window not in WINDOW_SECONDS:
        raise ValueError(f"unsupported analytics window: {window}")
    end = collected_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    seconds = WINDOW_SECONDS[window]
    start = end - timedelta(seconds=seconds) if seconds is not None else None
    return (start.isoformat() if start else "1970-01-01T00:00:00+00:00", end.isoformat())


def record_snapshot(connection: sqlite3.Connection, snapshot: AnalyticsSnapshot) -> int:
    """Insert a normalized snapshot once and return its durable id."""
    metrics = normalize_metrics(snapshot.metrics)
    collected = datetime.fromisoformat(snapshot.collected_at.replace("Z", "+00:00")) if snapshot.collected_at else datetime.now(timezone.utc)
    collected = collected if collected.tzinfo else collected.replace(tzinfo=timezone.utc)
    window_start, window_end = _bounds(snapshot.window, collected)
    raw, source_hash = bounded_json_payload(snapshot.raw_payload)
    external_id = snapshot.external_id or snapshot.canonical_url or source_hash
    attribution = "ATTRIBUTED" if snapshot.publication_attempt_id is not None else "UNATTRIBUTED"
    with connection:
        connection.execute(
            """INSERT INTO analytics_snapshots
               (provider,channel,publication_attempt_id,event_id,external_id,canonical_url,
                collected_at,window_start,window_end,window,mode,attribution_status,
                raw_payload_json,source_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(provider,external_id,window_start,window_end,source_hash) DO NOTHING""",
            (snapshot.provider, snapshot.channel, snapshot.publication_attempt_id,
             snapshot.event_id, external_id, snapshot.canonical_url, collected.isoformat(),
             window_start, window_end, snapshot.window, snapshot.mode, attribution, raw, source_hash),
        )
        row = connection.execute(
            """SELECT id FROM analytics_snapshots
               WHERE provider=? AND external_id=? AND window_start=? AND window_end=? AND source_hash=?""",
            (snapshot.provider, external_id, window_start, window_end, source_hash),
        ).fetchone()
        if row is None:
            raise RuntimeError("analytics snapshot was not persisted")
        snapshot_id = int(row[0])
        for name, value in metrics.items():
            connection.execute(
                "INSERT INTO analytics_metrics(snapshot_id,metric_name,metric_value) VALUES (?,?,?) ON CONFLICT(snapshot_id,metric_name) DO UPDATE SET metric_value=excluded.metric_value",
                (snapshot_id, name, value),
            )
    return snapshot_id


def aggregate_publication(connection: sqlite3.Connection, publication_attempt_id: int, window: str = "lifetime", *, mode: str = "REAL", emit_feedback: bool = False) -> dict[str, Any] | None:
    """Build an explainable latest performance row for one publication."""
    if mode not in {"REAL", "SIMULATED"}:
        raise ValueError("analytics mode must be REAL or SIMULATED")
    row = connection.execute(
        """SELECT s.*, pa.channel AS publication_channel FROM analytics_snapshots s
           LEFT JOIN publication_attempts pa ON pa.id=s.publication_attempt_id
           WHERE s.publication_attempt_id=? AND s.window=? AND s.mode=?
           ORDER BY s.collected_at DESC,s.id DESC LIMIT 1""",
        (publication_attempt_id, window, mode),
    ).fetchone()
    if row is None:
        return None
    metrics = {item[0]: item[1] for item in connection.execute("SELECT metric_name,metric_value FROM analytics_metrics WHERE snapshot_id=?", (row["id"],))}
    views = metrics.get("views")
    impressions = metrics.get("impressions")
    clicks = metrics.get("clicks")
    engagements = metrics.get("engagements")
    if engagements is None:
        parts = [metrics.get(name) for name in ("likes", "comments", "shares", "saves")]
        if any(value is not None for value in parts):
            engagements = sum(value or 0 for value in parts)
    performance = {
        "publication_attempt_id": publication_attempt_id, "snapshot_id": row["id"],
        "provider": row["provider"], "channel": row["publication_channel"] or row["channel"], "window": window, "mode": mode,
        "metrics": metrics,
        "engagement_rate": (engagements / views) if engagements is not None and views not in (None, 0) else None,
        "click_rate": (clicks / views) if clicks is not None and views not in (None, 0) else None,
    }
    components = {
        "engagements": engagements, "clicks": clicks,
        "shares": metrics.get("shares"), "saves": metrics.get("saves"),
        "engagement_rate": performance["engagement_rate"],
    }
    score = (engagements or 0.0) + (clicks or 0.0) * 0.5 + (metrics.get("shares") or 0.0) * 2 + (metrics.get("saves") or 0.0) * 2
    with connection:
        connection.execute(
            """INSERT INTO content_performance(publication_attempt_id,snapshot_id,provider,channel,window,mode,views,impressions,unique_views,clicks,reactions,likes,comments,shares,saves,engagements,engagement_rate,click_rate)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(snapshot_id,window) DO UPDATE SET
                 views=excluded.views, impressions=excluded.impressions, unique_views=excluded.unique_views,
                 clicks=excluded.clicks, reactions=excluded.reactions, likes=excluded.likes,
                 comments=excluded.comments, shares=excluded.shares, saves=excluded.saves,
                 engagements=excluded.engagements, engagement_rate=excluded.engagement_rate,
                 click_rate=excluded.click_rate""",
            (publication_attempt_id, row["id"], row["provider"], row["channel"], window, mode,
             metrics.get("views"), metrics.get("impressions"), metrics.get("unique_views"), metrics.get("clicks"), metrics.get("reactions"), metrics.get("likes"), metrics.get("comments"), metrics.get("shares"), metrics.get("saves"), engagements, performance["engagement_rate"], performance["click_rate"]),
        )
        connection.execute(
            """INSERT INTO content_feedback(publication_attempt_id,channel,performance_window,performance_score,score_components,mode)
               VALUES (?,?,?,?,?,?) ON CONFLICT(publication_attempt_id,performance_window,generated_at) DO NOTHING""",
            (publication_attempt_id, performance["channel"], window, score, json.dumps(components, ensure_ascii=False), mode),
        )
        if emit_feedback:
            performance["feedback_event_id"] = enqueue_feedback_event(connection, performance)
    return performance


def enqueue_feedback_event(connection: sqlite3.Connection, performance: dict[str, Any]) -> int:
    """Emit a compact durable feedback event for future editorial consumers."""
    payload = {key: value for key, value in performance.items() if key != "metrics"}
    payload["metrics"] = performance.get("metrics", {})
    with connection:
        cursor = connection.execute("INSERT INTO events_queue(event_type,payload) VALUES ('CONTENT_PERFORMANCE_UPDATED',?)", (json.dumps(payload, ensure_ascii=False),))
    return int(cursor.lastrowid)
