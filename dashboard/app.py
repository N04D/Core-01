"""Flask application for managing the local Event-Driven AI OS."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from flask import Flask, abort, jsonify, render_template, request


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
VAULT_ROOT: Final = PROJECT_ROOT / "vault"
EDITABLE_AREAS: Final = {"concepten", "uitgaand"}
PLUGIN_ANALYTICS: Final = {
    "LinkedIn Pro Publisher & Analytics": ("linkedin_analytics_*.json",),
    "LinkedIn Publisher (Productie)": ("linkedin_analytics_*.json",),
    "Substack Publisher": ("substack_analytics_*.json",),
    "Substack Pro Publisher & Analytics": (
        "substack_analytics_*.json",
        "substack_read_comments_*.json",
    ),
}


def create_app(database_path: Path | None = None) -> Flask:
    """Create a configured dashboard application."""
    app = Flask(__name__)
    app.config["DATABASE"] = str(
        (database_path or Path(os.getenv("SOCIAL_DB_PATH", DEFAULT_DATABASE)))
        .expanduser()
        .resolve()
    )
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(app.config["DATABASE"], timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def safe_markdown_path(area: str, relative_path: str) -> Path:
        if area not in EDITABLE_AREAS:
            abort(400, description="Onbekend vaultgebied")
        root = (VAULT_ROOT / area).resolve()
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            abort(400, description="Ongeldig bestandspad")
        if candidate.suffix.lower() != ".md":
            abort(400, description="Alleen Markdown-bestanden zijn toegestaan")
        return candidate

    def artifact_record(path: Path) -> dict[str, Any]:
        stat_result = path.stat()
        return {
            "name": path.name,
            "path": str(path.relative_to(VAULT_ROOT)),
            "size": stat_result.st_size,
            "modified_at": datetime.fromtimestamp(
                stat_result.st_mtime, tz=timezone.utc
            ).isoformat(),
        }

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/api/health")
    def health() -> Any:
        try:
            with connect() as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as exc:
            return jsonify({"status": "error", "database": str(exc)}), 503
        return jsonify({"status": "ok", "database_integrity": integrity})

    @app.get("/api/overview")
    def overview() -> Any:
        with connect() as connection:
            queue = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) count FROM events_queue GROUP BY status"
                )
            }
            dead_letters = connection.execute(
                "SELECT COUNT(*) FROM dead_letter_queue"
            ).fetchone()[0]
            active_plugins = connection.execute(
                "SELECT COUNT(*) FROM plugin_registry WHERE is_active=1"
            ).fetchone()[0]
        return jsonify(
            {
                "queue": queue,
                "dead_letters": dead_letters,
                "active_plugins": active_plugins,
                "drafts": len(list((VAULT_ROOT / "concepten").glob("*.md"))),
                "publications": len(list((VAULT_ROOT / "gepubliceerd").glob("*.md"))),
            }
        )

    @app.get("/api/plugins")
    def plugins() -> Any:
        with connect() as connection:
            rows = connection.execute(
                """
                SELECT plugin_name, type, executable_path, icon, is_active
                  FROM plugin_registry
                 ORDER BY type, plugin_name
                """
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.patch("/api/plugins/<path:plugin_name>")
    def update_plugin(plugin_name: str) -> Any:
        body = request.get_json(silent=True) or {}
        active = body.get("is_active")
        if not isinstance(active, bool):
            abort(400, description="is_active moet boolean zijn")
        with connect() as connection:
            cursor = connection.execute(
                "UPDATE plugin_registry SET is_active=? WHERE plugin_name=?",
                (int(active), plugin_name),
            )
            connection.commit()
        if cursor.rowcount != 1:
            abort(404, description="Plugin niet gevonden")
        return jsonify({"plugin_name": plugin_name, "is_active": active})

    @app.get("/api/plugin-stats")
    def plugin_stats() -> Any:
        """Load widget data only for plugins explicitly marked active."""
        with connect() as connection:
            active = {
                row[0]
                for row in connection.execute(
                    "SELECT plugin_name FROM plugin_registry WHERE is_active=1"
                )
            }
            routed_counts = {
                row["plugin_name"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT er.target_plugin_name plugin_name, COUNT(eq.id) count
                      FROM event_routes er
                      LEFT JOIN events_queue eq ON eq.event_type=er.event_type
                     GROUP BY er.target_plugin_name
                    """
                )
            }
        widgets = []
        for plugin_name in sorted(active):
            widget: dict[str, Any] = {
                "plugin_name": plugin_name,
                "events": routed_counts.get(plugin_name, 0),
                "analytics_files": 0,
                "likes": 0,
                "views": 0,
                "comments": 0,
            }
            patterns = PLUGIN_ANALYTICS.get(plugin_name, ())
            analytics_paths = {
                path
                for pattern in patterns
                for path in (VAULT_ROOT / "analytics").glob(pattern)
            }
            widget["analytics_files"] = len(analytics_paths)
            for path in analytics_paths:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                posts = data.get("posts", []) if isinstance(data, dict) else []
                for post in posts if isinstance(posts, list) else []:
                    if not isinstance(post, dict):
                        continue
                    for field in ("likes", "views"):
                        value = post.get(field, 0)
                        try:
                            widget[field] += int(value)
                        except (TypeError, ValueError):
                            pass
                    comments = post.get("comments", [])
                    widget["comments"] += len(comments) if isinstance(comments, list) else 0
            widgets.append(widget)
        return jsonify(widgets)

    @app.get("/api/files/<area>")
    def list_files(area: str) -> Any:
        if area not in EDITABLE_AREAS:
            abort(400, description="Onbekend vaultgebied")
        root = VAULT_ROOT / area
        root.mkdir(parents=True, exist_ok=True)
        return jsonify(
            [artifact_record(path) for path in sorted(root.rglob("*.md"), reverse=True)]
        )

    @app.get("/api/files/<area>/<path:relative_path>")
    def read_file(area: str, relative_path: str) -> Any:
        path = safe_markdown_path(area, relative_path)
        if not path.is_file():
            abort(404, description="Bestand niet gevonden")
        return jsonify(
            {
                "name": path.name,
                "path": str(path.relative_to(VAULT_ROOT / area)),
                "content": path.read_text(encoding="utf-8"),
            }
        )

    @app.put("/api/files/<area>/<path:relative_path>")
    def save_file(area: str, relative_path: str) -> Any:
        path = safe_markdown_path(area, relative_path)
        body = request.get_json(silent=True) or {}
        content = body.get("content")
        if not isinstance(content, str):
            abort(400, description="content moet tekst zijn")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return jsonify({"saved": True, "path": str(path.relative_to(VAULT_ROOT))})

    @app.post("/api/drafts/<path:relative_path>/dispatch")
    def dispatch_draft(relative_path: str) -> Any:
        draft = safe_markdown_path("concepten", relative_path)
        if not draft.is_file():
            abort(404, description="Draft niet gevonden")
        body = request.get_json(silent=True) or {}
        channels = body.get("channels", ["PUBLISH_MOCK"])
        if not isinstance(channels, list) or not channels or not all(
            isinstance(channel, str) for channel in channels
        ):
            abort(400, description="channels moet een niet-lege lijst zijn")
        event_ids: list[int] = []
        with connect() as connection:
            for channel in dict.fromkeys(channels):
                route = connection.execute(
                    """
                    SELECT 1 FROM event_routes er
                    JOIN plugin_registry pr ON pr.plugin_name=er.target_plugin_name
                    WHERE er.event_type=? AND pr.is_active=1
                    """,
                    (channel,),
                ).fetchone()
                if route is None:
                    abort(409, description=f"Geen actieve route voor {channel}")
                cursor = connection.execute(
                    "INSERT INTO events_queue(event_type,payload) VALUES (?,?)",
                    (
                        channel,
                        json.dumps(
                            {
                                "draft_file": str(draft),
                                "source": "dashboard",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                event_ids.append(int(cursor.lastrowid))
            connection.commit()
        return jsonify({"queued": True, "event_ids": event_ids}), 202

    @app.get("/api/publications")
    def publications() -> Any:
        root = VAULT_ROOT / "gepubliceerd"
        root.mkdir(parents=True, exist_ok=True)
        return jsonify(
            [artifact_record(path) for path in sorted(root.glob("*.md"), reverse=True)]
        )

    @app.get("/api/analytics")
    def analytics() -> Any:
        """Return analytics only for active plugins with matching adapters."""
        with connect() as connection:
            active = {
                row[0]
                for row in connection.execute(
                    "SELECT plugin_name FROM plugin_registry WHERE is_active=1"
                )
            }
        records = []
        for plugin_name, patterns in PLUGIN_ANALYTICS.items():
            if plugin_name not in active:
                continue
            for pattern in patterns:
                for path in sorted((VAULT_ROOT / "analytics").glob(pattern), reverse=True):
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    records.append(
                        {
                            "plugin_name": plugin_name,
                            "file": path.name,
                            "data": data,
                        }
                    )
        return jsonify(records)

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(409)
    def api_error(error: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), error.code
        return error

    return app
