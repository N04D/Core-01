from __future__ import annotations

import json
from pathlib import Path

from core.analytics import AnalyticsSnapshot, aggregate_publication, normalize_metrics, record_snapshot
from core.evergreen import analyze_normalized
from core.database import connect_database
from core.setup_database import initialize_database
from daemon.worker import process_one
from plugins.analytics.website_analytics import register_plugin


def event(db: Path, event_type: str, payload: dict) -> int:
    with connect_database(db) as conn:
        row = conn.execute("INSERT INTO events_queue(event_type,payload) VALUES (?,?) RETURNING id", (event_type, json.dumps(payload))).fetchone()
        conn.commit()
        return int(row[0])


def test_normalization_preserves_missing_and_measured_zero():
    metrics = normalize_metrics({"pageviews": 0, "visitors": 2, "clicks": None})
    assert metrics == {"views": 0.0, "unique_views": 2.0}


def test_snapshot_history_and_deduplication(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    snapshot = AnalyticsSnapshot("fixture", "markdown_git", "article", "https://example.test/a", {"views": 0, "likes": 2}, {"metrics": {"views": 0, "likes": 2}}, "24h", "SIMULATED", collected_at="2026-01-02T10:05:00+00:00")
    with connect_database(db) as conn:
        first = record_snapshot(conn, snapshot)
        second = record_snapshot(conn, snapshot)
        assert first == second
        assert conn.execute("SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT metric_value FROM analytics_metrics WHERE metric_name='views'").fetchone()[0] == 0


def test_worker_collects_simulated_snapshot_and_aggregation_emits_feedback(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        register_plugin(conn)
        attempt = conn.execute("INSERT INTO publication_attempts(event_id,channel,content_hash,target,status,platform_url) VALUES (?,?,?,?,?,?) RETURNING id", (99, "PUBLISH_MARKDOWN_GIT", "hash", "site:article.md", "CONFIRMED", "https://example.test/article")).fetchone()[0]
        conn.commit()
    event_id = event(db, "ANALYTICS_COLLECT", {"mode": "SIMULATED", "publication_attempt_id": attempt, "canonical_url": "https://example.test/article", "window": "24h", "fixture": {"metrics": {"views": 100, "likes": 10, "comments": 2}}})
    with connect_database(db) as conn:
        assert process_one(conn, db, "analytics-test", 60, 10)
    with connect_database(db, read_only=True) as conn:
        row = conn.execute("SELECT status FROM events_queue WHERE id=?", (event_id,)).fetchone()
        assert row["status"] == "SIMULATED"
        assert conn.execute("SELECT COUNT(*) FROM analytics_snapshots WHERE publication_attempt_id=?", (attempt,)).fetchone()[0] == 1
    with connect_database(db) as conn:
        performance = aggregate_publication(conn, int(attempt), "24h", emit_feedback=True)
        assert performance and performance["engagement_rate"] == 0.12
        processed, flagged = analyze_normalized(db, threshold=1)
        assert (processed, flagged) == (1, 1)
        feedback_id = conn.execute("SELECT id FROM events_queue WHERE event_type='CONTENT_PERFORMANCE_UPDATED'").fetchone()[0]
        assert feedback_id


def test_unattributed_snapshot_is_visible_and_api_exposes_history(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        record_snapshot(conn, AnalyticsSnapshot("fixture", "markdown_git", "unknown", None, {"views": 1}, {"metrics": {"views": 1}}, "lifetime", "SIMULATED"))
    from dashboard.app import create_app
    client = create_app(db).test_client()
    assert client.get("/api/analytics/snapshots?provider=fixture").status_code == 200
    assert client.get("/api/analytics/publications").status_code == 200
    assert client.get("/api/analytics/providers").status_code == 200
