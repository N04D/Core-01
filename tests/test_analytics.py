from __future__ import annotations

import json
from urllib.error import HTTPError
from pathlib import Path

from core.analytics import AnalyticsSnapshot, aggregate_publication, bounded_json_payload, normalize_metrics, record_snapshot
from core.evergreen import analyze_normalized
from core.database import connect_database
from core.setup_database import initialize_database
from daemon.worker import process_one
from plugins.analytics.website_analytics import (
    AnalyticsAuthRequired,
    AnalyticsRateLimited,
    PlausibleProvider,
    _publication_target,
    collect_event,
    register_plugin,
)


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
        performance = aggregate_publication(conn, int(attempt), "24h", mode="SIMULATED", emit_feedback=False)
        assert performance and performance["engagement_rate"] == 0.12
        assert performance["mode"] == "SIMULATED"
        processed, flagged = analyze_normalized(db, threshold=1)
        assert (processed, flagged) == (0, 0)

    aggregate_id = event(db, "ANALYTICS_AGGREGATE", {"mode": "SIMULATED", "publication_attempt_id": attempt, "window": "24h"})
    with connect_database(db) as conn:
        assert process_one(conn, db, "analytics-test", 60, 10)
    with connect_database(db, read_only=True) as conn:
        assert conn.execute("SELECT status FROM events_queue WHERE id=?", (aggregate_id,)).fetchone()["status"] == "SIMULATED"
        feedback_id = conn.execute("SELECT id FROM events_queue WHERE event_type='CONTENT_PERFORMANCE_UPDATED' ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert json.loads(conn.execute("SELECT payload FROM events_queue WHERE id=?", (feedback_id,)).fetchone()[0])["mode"] == "SIMULATED"
    with connect_database(db) as conn:
        assert process_one(conn, db, "analytics-feedback-test", 60, 10)
    with connect_database(db, read_only=True) as conn:
        assert conn.execute("SELECT status FROM events_queue WHERE id=?", (feedback_id,)).fetchone()["status"] == "SIMULATED"


def test_unattributed_snapshot_is_visible_and_api_exposes_history(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        record_snapshot(conn, AnalyticsSnapshot("fixture", "markdown_git", "unknown", None, {"views": 1}, {"metrics": {"views": 1}}, "lifetime", "SIMULATED"))
    from dashboard.app import create_app
    client = create_app(db).test_client()
    assert client.get("/api/analytics/snapshots?provider=fixture").get_json() == []
    assert len(client.get("/api/analytics/snapshots?provider=fixture&mode=SIMULATED").get_json()) == 1
    assert client.get("/api/analytics/publications").status_code == 200
    assert client.get("/api/analytics/providers").status_code == 200
    response = client.post("/api/analytics/collect", json={"mode": "SIMULATED", "external_id": "manual"})
    assert response.status_code == 202


def test_registration_keeps_feedback_as_a_downstream_consumer(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        register_plugin(conn)
        rows = dict(conn.execute("SELECT event_type,target_plugin_name FROM event_routes WHERE event_type IN ('ANALYTICS_COLLECT','ANALYTICS_AGGREGATE','CONTENT_PERFORMANCE_UPDATED')"))
        assert rows["ANALYTICS_COLLECT"] == "Website Analytics"
        assert rows["ANALYTICS_AGGREGATE"] == "Website Analytics"
        assert rows["CONTENT_PERFORMANCE_UPDATED"] == "Analytics Feedback / Evergreen Feedback"


def test_simulated_and_real_performance_are_isolated(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        attempt = conn.execute("INSERT INTO publication_attempts(event_id,channel,content_hash,target,status) VALUES (?,?,?,?,?) RETURNING id", (1, "PUBLISH_MARKDOWN_GIT", "h", "t", "CONFIRMED")).fetchone()[0]
        record_snapshot(conn, AnalyticsSnapshot("fixture", "markdown_git", "pub", None, {"views": 10}, {"v": 1}, "24h", "SIMULATED", int(attempt)))
        record_snapshot(conn, AnalyticsSnapshot("fixture", "markdown_git", "pub-real", None, {"views": 20}, {"v": 2}, "24h", "REAL", int(attempt)))
        assert aggregate_publication(conn, int(attempt), "24h", mode="REAL")["metrics"]["views"] == 20
        assert conn.execute("SELECT COUNT(*) FROM content_performance WHERE mode='SIMULATED'").fetchone()[0] == 0
        assert aggregate_publication(conn, int(attempt), "24h", mode="SIMULATED")["metrics"]["views"] == 10
        assert conn.execute("SELECT COUNT(*) FROM content_performance WHERE mode='SIMULATED'").fetchone()[0] == 1


def test_attribution_identity_and_invalid_explicit_id(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        first = conn.execute("INSERT INTO publication_attempts(event_id,channel,content_hash,target,status,platform_url) VALUES (?,?,?,?,?,?) RETURNING id", (1, "A", "a", "t", "CONFIRMED", "https://example.test/a")).fetchone()[0]
        second = conn.execute("INSERT INTO publication_attempts(event_id,channel,content_hash,target,status) VALUES (?,?,?,?,?) RETURNING id", (2, "B", "b", "t", "CONFIRMED")).fetchone()[0]
        _, _, identity_one, _ = _publication_target(conn, {"publication_attempt_id": first})
        _, _, identity_two, _ = _publication_target(conn, {"publication_attempt_id": second})
        assert identity_one == "https://example.test/a"
        assert identity_two == f"publication:{second}"
        try:
            _publication_target(conn, {"publication_attempt_id": 999})
        except Exception as exc:
            assert "does not exist" in str(exc)
        else:
            raise AssertionError("invalid publication attempt was accepted")


def test_two_attributed_publications_do_not_collide_on_identical_metrics(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        first = conn.execute("INSERT INTO publication_attempts(event_id,channel,content_hash,target,status) VALUES (?,?,?,?,?) RETURNING id", (1, "A", "a", "t", "CONFIRMED")).fetchone()[0]
        second = conn.execute("INSERT INTO publication_attempts(event_id,channel,content_hash,target,status) VALUES (?,?,?,?,?) RETURNING id", (2, "B", "b", "t", "CONFIRMED")).fetchone()[0]
        record_snapshot(conn, AnalyticsSnapshot("fixture", "A", f"publication:{first}", None, {"views": 5}, {"metrics": {"views": 5}}, "24h", "REAL", int(first)))
        record_snapshot(conn, AnalyticsSnapshot("fixture", "B", f"publication:{second}", None, {"views": 5}, {"metrics": {"views": 5}}, "24h", "REAL", int(second)))
        assert conn.execute("SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0] == 2


def test_bounded_raw_payload_is_valid_and_redacts_secrets():
    raw, digest = bounded_json_payload({"token": "do-not-store", "nested": {"password": "secret", "items": ["x" * 200_000]}})
    parsed = json.loads(raw)
    assert parsed["_truncated"] is True
    assert "do-not-store" not in raw and "secret" not in raw
    assert len(raw.encode()) <= 100_000
    assert len(digest) == 64


def test_plausible_error_types():
    provider = PlausibleProvider(base_url="http://127.0.0.1:1", site_id="", token=None)
    try:
        provider.collect(__import__("plugins.analytics.base", fromlist=["CollectionTarget"]).CollectionTarget("https://example.test", "x"))
    except AnalyticsAuthRequired:
        pass
    else:
        raise AssertionError("missing Plausible credentials were not classified")


def test_plausible_http_error_classification(monkeypatch):
    import plugins.analytics.website_analytics as website
    from plugins.analytics.base import CollectionTarget
    provider = PlausibleProvider(base_url="https://plausible.test", site_id="site", token="token")
    monkeypatch.setattr(website, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(HTTPError("https://plausible.test", 429, "rate", {}, None)))
    try:
        provider.collect(CollectionTarget("https://example.test", "x"))
    except AnalyticsRateLimited:
        pass
    else:
        raise AssertionError("429 was not classified as rate limited")


def test_collection_run_never_stays_running_on_rate_limit(monkeypatch, tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        attempt = conn.execute("INSERT INTO publication_attempts(event_id,channel,content_hash,target,status,platform_url) VALUES (?,?,?,?,?,?) RETURNING id", (1, "PUBLISH_MARKDOWN_GIT", "h", "t", "CONFIRMED", "https://example.test/article")).fetchone()[0]
        conn.commit()
    def rate_limited(self, target):
        raise AnalyticsRateLimited("rate")
    monkeypatch.setattr(PlausibleProvider, "collect", rate_limited)
    monkeypatch.setenv("PLAUSIBLE_SITE_ID", "site")
    monkeypatch.setenv("PLAUSIBLE_API_KEY", "secret")
    try:
        collect_event(db, 1, {"publication_attempt_id": int(attempt), "canonical_url": "https://example.test/article"})
    except AnalyticsRateLimited:
        pass
    else:
        raise AssertionError("rate-limit exception was not propagated")
    with connect_database(db, read_only=True) as conn:
        assert conn.execute("SELECT status FROM analytics_collection_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "RATE_LIMITED"


def test_default_database_follows_core_data(monkeypatch, tmp_path: Path):
    import subprocess
    import sys
    monkeypatch.setenv("CORE_DATA", str(tmp_path / "custom-data"))
    output = subprocess.check_output([sys.executable, "-c", "from plugins.analytics.website_analytics import DEFAULT_DB; print(DEFAULT_DB)"], text=True)
    assert output.strip() == str(tmp_path / "custom-data" / "db" / "events.db")
