from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.database import connect_database
from core.setup_database import initialize_database
from plugins.analytics.linkedin_analytics import (
    LinkedInInvalidResponse,
    is_publication_specific_linkedin_url,
    normalize_linkedin_metrics,
    parse_metric,
    validate_linkedin_url,
)
from plugins.analytics.website_analytics import collect_event, register_plugin


def _attempt(db: Path, channel: str = "PUBLISH_LINKEDIN_PRO", url: str | None = None) -> int:
    with connect_database(db) as conn:
        row = conn.execute(
            "INSERT INTO publication_attempts(event_id,channel,content_hash,target,status,platform_url) VALUES (?,?,?,?,?,?) RETURNING id",
            (1, channel, "hash", "target", "CONFIRMED", url),
        ).fetchone()
        conn.commit()
        return int(row[0])


def test_provider_registration_does_not_overwrite_dispatch_route(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        register_plugin(conn)
        assert conn.execute("SELECT target_plugin_name FROM event_routes WHERE event_type='ANALYTICS_COLLECT'").fetchone()[0] == "Website Analytics"
        assert conn.execute("SELECT is_active FROM plugin_registry WHERE plugin_name='LinkedIn Analytics'").fetchone()[0] == 1


def test_simulated_linkedin_does_not_open_browser_or_require_auth(tmp_path: Path, monkeypatch):
    db = tmp_path / "events.db"
    initialize_database(db)
    attempt = _attempt(db, url="https://www.linkedin.com/posts/example-123")
    with connect_database(db) as conn:
        register_plugin(conn)
    monkeypatch.setattr("plugins.analytics.linkedin_analytics.LinkedInAnalyticsProvider.collect", lambda *_: pytest.fail("browser provider called"))
    status, result, _ = collect_event(db, None, {"provider": "linkedin", "mode": "SIMULATED", "publication_attempt_id": attempt, "fixture": {"metrics": {"views": 0, "reactions": 2}}})
    assert status == "SIMULATED"
    assert result["provider"] == "linkedin"
    with connect_database(db, read_only=True) as conn:
        row = conn.execute("SELECT provider,mode FROM analytics_snapshots").fetchone()
        assert tuple(row) == ("linkedin", "SIMULATED")


def test_disabled_linkedin_provider_is_rejected(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    attempt = _attempt(db, url="https://www.linkedin.com/posts/example-123")
    with connect_database(db) as conn:
        register_plugin(conn)
        conn.execute("UPDATE plugin_registry SET is_active=0 WHERE plugin_name='LinkedIn Analytics'")
        conn.commit()
    with pytest.raises(Exception, match="disabled"):
        collect_event(db, 1, {"provider": "linkedin", "mode": "SIMULATED", "publication_attempt_id": attempt})


def test_missing_auth_is_classified_and_no_real_snapshot(tmp_path: Path, monkeypatch):
    db = tmp_path / "events.db"
    initialize_database(db)
    attempt = _attempt(db, url="https://www.linkedin.com/posts/example-123")
    with connect_database(db) as conn:
        register_plugin(conn)
    monkeypatch.setenv("LINKEDIN_AUTH_PATH", str(tmp_path / "missing.json"))
    from plugins.analytics.website_analytics import AnalyticsAuthRequired
    with pytest.raises(AnalyticsAuthRequired):
        collect_event(db, None, {"provider": "linkedin", "publication_attempt_id": attempt})
    with connect_database(db, read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM analytics_collection_runs").fetchone()[0] == "AUTH_REQUIRED"


def test_metric_semantics_and_url_allowlist():
    assert parse_metric("1,2K") == 1200
    assert parse_metric("1,234") == 1234
    assert normalize_linkedin_metrics({"views": "1,234", "impressions": "2K", "reactions": 0, "comments": None}) == {"views": 1234, "impressions": 2000, "reactions": 0}
    assert validate_linkedin_url("https://www.linkedin.com/posts/example")
    with pytest.raises(LinkedInInvalidResponse):
        validate_linkedin_url("https://example.com/posts/example")


def test_unknown_provider_rejected(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    attempt = _attempt(db, url="https://www.linkedin.com/posts/example-123")
    with connect_database(db) as conn:
        register_plugin(conn)
    with pytest.raises(Exception, match="unknown analytics provider"):
        collect_event(db, 1, {"provider": "not-real", "mode": "SIMULATED", "publication_attempt_id": attempt})


@pytest.mark.parametrize("channel", ["LINKEDIN", "PUBLISH_LINKEDIN", "PUBLISH_LINKEDIN_PRO"])
@pytest.mark.parametrize("requested_window", ["24h", "7d", "30d", "lifetime"])
def test_linkedin_channel_inference_and_lifetime_window(tmp_path: Path, channel: str, requested_window: str):
    db = tmp_path / "events.db"
    initialize_database(db)
    attempt = _attempt(db, channel=channel, url="https://www.linkedin.com/posts/example-123")
    with connect_database(db) as conn:
        register_plugin(conn)
    status, result, _ = collect_event(db, None, {"mode": "SIMULATED", "window": requested_window, "publication_attempt_id": attempt, "fixture": {"metrics": {"views": 3}}})
    assert status == "SIMULATED"
    assert result["provider"] == "linkedin"
    assert result["stored_window"] == "lifetime"
    with connect_database(db, read_only=True) as conn:
        assert conn.execute("SELECT window FROM analytics_snapshots").fetchone()[0] == "lifetime"


def test_unrelated_channel_does_not_infer_linkedin(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    attempt = _attempt(db, channel="PUBLISH_MARKDOWN_GIT", url="https://example.test/article")
    with connect_database(db) as conn:
        register_plugin(conn)
    # The fallback is Plausible, so with no credentials this fails auth rather
    # than contacting LinkedIn or creating a LinkedIn snapshot.
    from plugins.analytics.website_analytics import AnalyticsAuthRequired
    with pytest.raises(AnalyticsAuthRequired):
        collect_event(db, None, {"publication_attempt_id": attempt})


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/feed/",
    "https://www.linkedin.com/in/example/recent-activity/all/",
    "https://www.linkedin.com/company/example/posts/",
    "https://www.linkedin.com/company/example/admin/page-posts/published/",
])
def test_generic_linkedin_urls_are_rejected(url: str):
    assert not is_publication_specific_linkedin_url(url)
    with pytest.raises(LinkedInInvalidResponse):
        validate_linkedin_url(url)


def test_exact_linkedin_post_url_is_accepted():
    assert is_publication_specific_linkedin_url("https://www.linkedin.com/posts/example-activity-123")
    assert is_publication_specific_linkedin_url("https://www.linkedin.com/feed/update/urn:li:activity:123")


def test_generic_identity_fails_before_browser_and_snapshot(tmp_path: Path, monkeypatch):
    from plugins.analytics.base import CollectionTarget
    from plugins.analytics.linkedin_analytics import LinkedInAnalyticsProvider
    monkeypatch.setattr("plugins.channels.pub_linkedin_pro.browser_page", lambda *_args, **_kwargs: pytest.fail("browser must not open"))
    with pytest.raises(LinkedInInvalidResponse):
        LinkedInAnalyticsProvider().collect(CollectionTarget("https://www.linkedin.com/feed/", "feed"))


def test_missing_linkedin_capability_fails_closed_without_browser(tmp_path: Path, monkeypatch):
    db = tmp_path / "events.db"
    initialize_database(db)
    attempt = _attempt(db, url="https://www.linkedin.com/posts/example-123")
    monkeypatch.setattr("plugins.analytics.linkedin_analytics.LinkedInAnalyticsProvider.collect", lambda *_: pytest.fail("browser provider called"))
    with pytest.raises(Exception, match="disabled or unregistered"):
        collect_event(db, None, {"provider": "linkedin", "mode": "SIMULATED", "publication_attempt_id": attempt})


def test_manual_api_preserves_and_validates_provider(tmp_path: Path):
    from dashboard.app import create_app
    db = tmp_path / "events.db"
    initialize_database(db)
    client = create_app(db).test_client()
    response = client.post("/api/analytics/collect", json={"provider": "linkedin", "publication_attempt_id": 42})
    assert response.status_code == 202
    event_id = response.get_json()["event_id"]
    with connect_database(db, read_only=True) as conn:
        payload = json.loads(conn.execute("SELECT payload FROM events_queue WHERE id=?", (event_id,)).fetchone()[0])
        assert payload["provider"] == "linkedin"
    assert client.post("/api/analytics/collect", json={"provider": "evil-provider"}).status_code == 400
