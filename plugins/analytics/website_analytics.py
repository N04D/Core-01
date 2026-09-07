#!/usr/bin/env python3
"""Website analytics adapter with deterministic simulation and Plausible support."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.analytics import AnalyticsSnapshot, aggregate_publication, record_snapshot
from core.database import connect_database
from core.event_protocol import emit_result
from core.evergreen import analyze_normalized
from core.paths import DATABASE_PATH
from core.setup_database import initialize_database
from plugins.analytics.base import AnalyticsProvider, CollectionTarget
from plugins.analytics.feedback_consumer import register_plugin as register_feedback_plugin

EVENT_COLLECT = "ANALYTICS_COLLECT"
EVENT_AGGREGATE = "ANALYTICS_AGGREGATE"
PLUGIN_NAME = "Website Analytics"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = DATABASE_PATH
WINDOWS = {"24h", "7d", "30d", "lifetime"}


class AnalyticsError(RuntimeError):
    """Safe provider/normalization failure."""


class AnalyticsAuthRequired(AnalyticsError):
    pass


class AnalyticsRateLimited(AnalyticsError):
    pass


class AnalyticsTemporaryError(AnalyticsError):
    pass


class AnalyticsInvalidResponse(AnalyticsError):
    pass


class PlausibleProvider:
    name = "plausible"

    def __init__(self, *, base_url: str, site_id: str, token: str | None, timeout: float = 20.0):
        self.base_url, self.site_id, self.token, self.timeout = base_url.rstrip("/"), site_id, token, timeout

    def collect(self, target: CollectionTarget) -> dict[str, Any]:
        if not self.site_id or not self.token:
            raise AnalyticsAuthRequired("Plausible API credentials are not configured")
        if not target.canonical_url or urlparse(target.canonical_url).scheme not in {"http", "https"}:
            raise AnalyticsInvalidResponse("a valid canonical URL is required for website analytics")
        query = urlencode({"site_id": self.site_id, "metrics": "pageviews,visitors,events", "date_range": target.window, "filters": json.dumps(["is", "event:page", [target.canonical_url]])})
        request = Request(f"{self.base_url}/api/v2/stats/aggregate?{query}", headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = json.loads(response.read(100_000).decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise AnalyticsAuthRequired("Plausible authentication is required") from exc
            if exc.code == 429:
                raise AnalyticsRateLimited("Plausible rate limit reached") from exc
            raise AnalyticsInvalidResponse(f"Plausible returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise AnalyticsTemporaryError("Plausible request failed") from exc
        except json.JSONDecodeError as exc:
            raise AnalyticsInvalidResponse("Plausible returned invalid JSON") from exc
        if not isinstance(raw, dict):
            raise AnalyticsInvalidResponse("Plausible returned an invalid response")
        results = raw.get("results", raw)
        if not isinstance(results, dict):
            raise AnalyticsInvalidResponse("Plausible metrics are missing")
        return {"metrics": results, "provider": self.name, "raw": raw}


class SimulatedProvider:
    name = "simulated-website"

    def __init__(self, fixture: dict[str, Any] | None = None):
        self.fixture = fixture or {}

    def collect(self, target: CollectionTarget) -> dict[str, Any]:
        if isinstance(self.fixture.get("metrics"), dict):
            metrics = dict(self.fixture["metrics"])
        else:
            seed = int(hashlib.sha256(target.external_id.encode()).hexdigest()[:8], 16)
            metrics = {"views": seed % 1000, "unique_views": seed % 700, "clicks": seed % 80, "likes": seed % 40, "comments": seed % 10, "shares": seed % 6}
        return {"metrics": metrics, "provider": self.name, "raw": {"simulated": True, "metrics": metrics}}


def register_plugin(connection: Any, enabled: bool = True) -> None:
    executable = str(Path(__file__).resolve())
    with connection:
        connection.execute("INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active) VALUES (?,?,?,?,?) ON CONFLICT(plugin_name) DO UPDATE SET executable_path=excluded.executable_path,type=excluded.type,icon=excluded.icon,is_active=excluded.is_active", (PLUGIN_NAME, "analytics", executable, "📈", int(enabled)))
        for event_type in (EVENT_COLLECT, EVENT_AGGREGATE):
            connection.execute("INSERT INTO event_routes(event_type,target_plugin_name) VALUES (?,?) ON CONFLICT(event_type) DO UPDATE SET target_plugin_name=excluded.target_plugin_name", (event_type, PLUGIN_NAME))
        register_feedback_plugin(connection, enabled=enabled)


def _publication_target(connection: Any, payload: dict[str, Any]) -> tuple[int | None, str | None, str, str | None]:
    explicit_id = payload.get("publication_attempt_id")
    url = payload.get("canonical_url") or payload.get("platform_url")
    row = None
    if explicit_id is not None:
        try:
            row = connection.execute("SELECT id,platform_id,platform_url,channel FROM publication_attempts WHERE id=?", (int(explicit_id),)).fetchone()
        except (TypeError, ValueError) as exc:
            raise AnalyticsInvalidResponse("publication_attempt_id must be an integer") from exc
        if row is None:
            raise AnalyticsInvalidResponse(f"publication attempt {explicit_id} does not exist")
    elif url:
        row = connection.execute("SELECT id,platform_id,platform_url,channel FROM publication_attempts WHERE platform_url=? ORDER BY updated_at DESC LIMIT 1", (url,)).fetchone()
    attempt_id = int(row["id"]) if row else None
    if row:
        url = url or row["platform_url"]
    external_id = payload.get("external_id")
    if external_id:
        identity = str(external_id)
    elif row and row["platform_id"]:
        identity = str(row["platform_id"])
    elif url:
        identity = str(url)
    elif attempt_id is not None:
        identity = f"publication:{attempt_id}"
    else:
        identity = "unattributed:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    channel = str(payload.get("channel") or (row["channel"] if row else "")) or None
    return attempt_id, url, identity, channel


def collect_event(database: Path, event_id: int, payload: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    window = str(payload.get("window", "lifetime"))
    if window not in WINDOWS:
        raise AnalyticsError(f"unsupported analytics window: {window}")
    simulated = bool(payload.get("mode", "").upper() == "SIMULATED" or payload.get("dry_run") or payload.get("simulated"))
    run_id: int | None = None
    with connect_database(database) as connection:
        attempt_id, url, external_id, channel = _publication_target(connection, payload)
        provider: AnalyticsProvider = SimulatedProvider(payload.get("fixture") if isinstance(payload.get("fixture"), dict) else payload) if simulated else PlausibleProvider(base_url=os.getenv("PLAUSIBLE_API_BASE_URL", "https://plausible.io"), site_id=os.getenv("PLAUSIBLE_SITE_ID", ""), token=os.getenv("PLAUSIBLE_API_KEY"))
        run_id = int(connection.execute("INSERT INTO analytics_collection_runs(provider,channel,status,window) VALUES (?,?,?,?) RETURNING id", (provider.name, channel, "RUNNING", window)).fetchone()[0])
        try:
            result = provider.collect(CollectionTarget(url, external_id, window))
            snapshot_id = record_snapshot(connection, AnalyticsSnapshot(provider=provider.name, channel=channel, publication_attempt_id=attempt_id, event_id=event_id, external_id=external_id, canonical_url=url, metrics=result["metrics"], raw_payload=result.get("raw", result), window=window, mode="SIMULATED" if simulated else "REAL"))
            status = "SIMULATED" if simulated else "COMPLETED"
            connection.execute("UPDATE analytics_collection_runs SET status=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (status, run_id))
            connection.commit()
        except Exception as exc:
            run_status = "AUTH_REQUIRED" if isinstance(exc, AnalyticsAuthRequired) else "RATE_LIMITED" if isinstance(exc, AnalyticsRateLimited) else "FAILED"
            connection.execute("UPDATE analytics_collection_runs SET status=?,completed_at=CURRENT_TIMESTAMP,error_log=? WHERE id=?", (run_status, str(exc)[:2000], run_id))
            connection.commit()
            raise
    return status, {"snapshot_id": snapshot_id, "provider": provider.name, "attribution_status": "ATTRIBUTED" if attempt_id else "UNATTRIBUTED", "window": window}, {"snapshot_id": snapshot_id}


def aggregate_event(database: Path, payload: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    attempt_id = payload.get("publication_attempt_id")
    if attempt_id is None:
        raise AnalyticsError("publication_attempt_id is required for aggregation")
    with connect_database(database) as connection:
        mode = str(payload.get("mode", "REAL")).upper()
        performance = aggregate_publication(connection, int(attempt_id), str(payload.get("window", "lifetime")), mode=mode, emit_feedback=True)
        if performance is None:
            raise AnalyticsError("no analytics snapshot exists for publication")
        if mode == "REAL":
            evergreen_processed, evergreen_flagged = analyze_normalized(database, mode="REAL")
            performance["evergreen"] = {"processed": evergreen_processed, "flagged": evergreen_flagged}
        else:
            performance["evergreen"] = {"processed": 0, "flagged": 0}
    return "SIMULATED" if mode == "SIMULATED" else "COMPLETED", {"performance": performance, "feedback_event_id": performance.get("feedback_event_id")}, {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    try:
        initialize_database(args.db)
        with connect_database(args.db) as connection:
            if args.register:
                register_plugin(connection)
                print(json.dumps({"status": "REGISTERED", "plugin": PLUGIN_NAME}))
                return 0
            if args.event_id is None:
                parser.error("--event_id is required unless --register is used")
            row = connection.execute("SELECT event_type,payload FROM events_queue WHERE id=?", (args.event_id,)).fetchone()
            if row is None:
                raise AnalyticsError(f"event {args.event_id} does not exist")
            payload = json.loads(row["payload"])
        if row["event_type"] == EVENT_AGGREGATE:
            outcome, result, patch = aggregate_event(args.db, payload)
        else:
            outcome, result, patch = collect_event(args.db, args.event_id, payload)
        emit_result(outcome, result=result, payload_patch=patch)
        return 0
    except AnalyticsAuthRequired as exc:
        emit_result("BLOCKED_AUTH", error=str(exc), retryable=False)
        return 0
    except AnalyticsRateLimited as exc:
        emit_result("FAILED", error=str(exc), retryable=True)
        return 0
    except AnalyticsTemporaryError as exc:
        emit_result("FAILED", error=str(exc), retryable=True)
        return 0
    except AnalyticsInvalidResponse as exc:
        emit_result("FAILED", error=str(exc), retryable=False)
        return 0
    except AnalyticsError as exc:
        emit_result("FAILED", error=str(exc), retryable=False)
        return 0
    except Exception as exc:
        emit_result("FAILED", error=str(exc)[:2000], retryable=False)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
