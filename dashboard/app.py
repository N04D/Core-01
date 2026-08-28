"""Flask application for managing the local Event-Driven AI OS."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database import connect_database


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
VAULT_ROOT: Final = PROJECT_ROOT / "vault"
EDITABLE_AREAS: Final = {"concepten", "uitgaand"}
MEDIA_EXTENSIONS: Final = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".gif": "image",
    ".webp": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
}
MEDIA_MIME_TYPES: Final = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".mp4": "video/mp4",
    ".webm": "video/webm", ".mov": "video/quicktime",
}
PLUGIN_ANALYTICS: Final = {
    "LinkedIn Pro Publisher & Analytics": ("linkedin_analytics_*.json",),
    "LinkedIn Publisher (Productie)": ("linkedin_analytics_*.json",),
    "Substack Publisher": ("substack_analytics_*.json",),
    "Substack Pro Publisher & Analytics": (
        "substack_analytics_*.json",
        "substack_read_comments_*.json",
    ),
}
AUTH_PROFILES: Final = {
    "LinkedIn Publisher (Productie)": {
        "platform": "linkedin",
        "auth_file": PROJECT_ROOT / "config" / "linkedin_auth.json",
    },
    "LinkedIn Pro Publisher & Analytics": {
        "platform": "linkedin",
        "auth_file": PROJECT_ROOT / "config" / "linkedin_auth.json",
    },
    "Substack Publisher": {
        "platform": "substack",
        "auth_file": PROJECT_ROOT / "config" / "substack_auth.json",
    },
    "Substack Pro Publisher & Analytics": {
        "platform": "substack",
        "auth_file": PROJECT_ROOT / "config" / "substack_auth.json",
    },
    "Medium Publisher": {
        "platform": "medium",
        "auth_file": PROJECT_ROOT / "config" / "medium_auth.json",
    },
}


def create_app(database_path: Path | None = None) -> Flask:
    """Create a configured dashboard application."""
    app = Flask(__name__)
    app.config["DATABASE"] = str(
        (database_path or Path(os.getenv("SOCIAL_DB_PATH", DEFAULT_DATABASE)))
        .expanduser()
        .resolve()
    )
    app.config["MAX_CONTENT_LENGTH"] = int(
        os.getenv("DASHBOARD_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))
    )

    def connect() -> sqlite3.Connection:
        return connect_database(app.config["DATABASE"], timeout=10.0)

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

    def safe_media_path(relative_path: str, *, must_exist: bool = True) -> Path:
        root = (VAULT_ROOT / "media").resolve()
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            abort(400, description="Ongeldig mediapad")
        if candidate.suffix.lower() not in MEDIA_EXTENSIONS:
            abort(400, description="Niet-ondersteund mediaformaat")
        if must_exist and not candidate.is_file():
            abort(404, description="Mediabestand niet gevonden")
        return candidate

    def media_record(path: Path) -> dict[str, Any]:
        record = artifact_record(path)
        record.update(
            {
                "relative_path": str(path.relative_to(VAULT_ROOT / "media")),
                "content_type": MEDIA_EXTENSIONS[path.suffix.lower()],
                "url": f"/api/media/{path.relative_to(VAULT_ROOT / 'media').as_posix()}",
            }
        )
        return record

    def valid_media_signature(suffix: str, header: bytes) -> bool:
        signatures = {
            ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
            ".jpg": header.startswith(b"\xff\xd8\xff"),
            ".jpeg": header.startswith(b"\xff\xd8\xff"),
            ".gif": header.startswith((b"GIF87a", b"GIF89a")),
            ".webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
            ".mp4": len(header) >= 12 and header[4:8] == b"ftyp",
            ".mov": len(header) >= 12 and header[4:8] == b"ftyp",
            ".webm": header.startswith(b"\x1aE\xdf\xa3"),
        }
        return bool(signatures.get(suffix, False))

    def indexed_asset_record(row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata"])
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        return {
            "id": row["id"], "source_id": row["source_id"], "source_key": row["source_key"],
            "source_name": row["source_name"], "filename": row["filename"],
            "content_type": row["media_type"], "mime_type": row["mime_type"],
            "size": row["file_size"], "modified_at": row["modified_at"],
            "prompt": row["prompt"], "metadata": metadata,
            "url": f"/api/media-store/assets/{row['id']}/content",
        }

    def indexed_asset(connection: sqlite3.Connection, asset_id: int) -> sqlite3.Row:
        row = connection.execute(
            """SELECT ma.*,ms.source_key,ms.display_name source_name,ms.root_path
                 FROM media_assets ma JOIN media_sources ms ON ms.id=ma.source_id
                WHERE ma.id=? AND ma.is_available=1 AND ms.is_active=1""",
            (asset_id,),
        ).fetchone()
        if row is None:
            abort(404, description="Media-asset niet gevonden")
        return row

    def validated_indexed_file(row: sqlite3.Row) -> Path:
        path = Path(row["file_path"]).expanduser().resolve()
        root_value = row["root_path"]
        if root_value:
            try:
                path.relative_to(Path(root_value).expanduser().resolve())
            except ValueError:
                abort(403, description="Asset valt buiten de geregistreerde bron")
        if not path.is_file():
            abort(404, description="Geïndexeerd bestand is niet meer beschikbaar")
        return path

    def payload_media(payload: dict[str, Any]) -> dict[str, Any] | None:
        raw = payload.get("media_path") or payload.get("image_path") or payload.get("video_path")
        if not isinstance(raw, str) or not raw:
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        try:
            candidate = candidate.resolve()
            candidate.relative_to((VAULT_ROOT / "media").resolve())
        except (OSError, ValueError):
            try:
                with connect() as connection:
                    row = connection.execute(
                        """SELECT ma.*,ms.source_key,ms.display_name source_name,ms.root_path
                             FROM media_assets ma JOIN media_sources ms ON ms.id=ma.source_id
                            WHERE ma.file_path=? AND ma.is_available=1 AND ms.is_active=1""",
                        (str(candidate),),
                    ).fetchone()
                return indexed_asset_record(row) if row else None
            except sqlite3.Error:
                return None
        if not candidate.is_file() or candidate.suffix.lower() not in MEDIA_EXTENSIONS:
            return None
        return media_record(candidate)

    def active_plugin(connection: sqlite3.Connection, plugin_name: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT plugin_name,type,executable_path,icon,is_active
              FROM plugin_registry
             WHERE plugin_name=? AND is_active=1
            """,
            (plugin_name,),
        ).fetchone()
        if row is None:
            abort(404, description="Actieve plugin niet gevonden")
        return row

    def auth_status(plugin_name: str) -> dict[str, Any]:
        profile = AUTH_PROFILES.get(plugin_name)
        if profile is None:
            return {
                "supported": False,
                "status": "NOT_REQUIRED",
                "connected": True,
                "message": "Deze lokale plugin vereist geen browsersessie.",
            }
        auth_file = Path(profile["auth_file"])
        result: dict[str, Any] = {
            "supported": True,
            "platform": profile["platform"],
            "auth_file": str(auth_file),
            "connected": False,
            "status": "AUTH_REQUIRED",
            "message": "Log in om een beveiligde browsersessie op te slaan.",
        }
        if not auth_file.is_file():
            return result
        try:
            mode = stat.S_IMODE(auth_file.stat().st_mode)
            state = json.loads(auth_file.read_text(encoding="utf-8"))
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                result["message"] = f"Onveilige bestandsrechten ({mode:o}); verwacht 600."
                return result
            if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                result["message"] = "Sessiebestand is geen geldige Playwright storage-state."
                return result
        except (OSError, json.JSONDecodeError) as exc:
            result["message"] = f"Sessiebestand kon niet worden gelezen: {exc}"
            return result
        result.update(
            {
                "connected": True,
                "status": "CONNECTED",
                "message": "Beveiligde Playwright-sessie beschikbaar.",
            }
        )
        return result

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

    @app.get("/api/metrics")
    def metrics() -> Any:
        """Expose lightweight operational metrics without payload contents."""
        with connect() as connection:
            queue = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status,COUNT(*) count FROM events_queue GROUP BY status"
                )
            }
            ages = connection.execute(
                """SELECT
                    COALESCE(MAX(0, strftime('%s','now')-strftime('%s',MIN(CASE WHEN status='PENDING' THEN created_at END))),0) pending_oldest_seconds,
                    COALESCE(MAX(0, strftime('%s','now')-strftime('%s',MIN(CASE WHEN status='PROCESSING' THEN created_at END))),0) processing_oldest_seconds
                   FROM events_queue"""
            ).fetchone()
            leases = connection.execute(
                """SELECT COUNT(*) active,
                          SUM(CASE WHEN lease_until<=CURRENT_TIMESTAMP THEN 1 ELSE 0 END) expired
                     FROM events_queue WHERE status='PROCESSING'"""
            ).fetchone()
            dlq = connection.execute("SELECT COUNT(*) FROM dead_letter_queue").fetchone()[0]
            sessions = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status,COUNT(*) count FROM session_health GROUP BY status"
                )
            }
        return jsonify({
            "queue": queue,
            "queue_age_seconds": dict(ages),
            "leases": {"active": leases["active"], "expired": leases["expired"] or 0},
            "dead_letters": dlq,
            "sessions": sessions,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    @app.get("/api/dead-letters")
    def dead_letters() -> Any:
        event_type = request.args.get("event_type", "").strip()
        query = request.args.get("q", "").strip()
        try:
            limit = min(200, max(1, int(request.args.get("limit", "50"))))
        except ValueError:
            abort(400, description="limit moet een geheel getal zijn")
        where = ["(?='' OR dlq.event_type=?)", "(?='' OR dlq.reason_for_death LIKE '%'||?||'%' OR dlq.error_log LIKE '%'||?||'%')"]
        parameters = (event_type, event_type, query, query, query, limit)
        with connect() as connection:
            rows = connection.execute(
                f"""SELECT dlq.id,dlq.event_type,dlq.status,dlq.retry_count,
                            dlq.error_log,dlq.reason_for_death,dlq.created_at,dlq.updated_at,
                            COUNT(dr.id) redrive_count,MAX(dr.created_at) last_redrive_at
                       FROM dead_letter_queue dlq
                       LEFT JOIN dead_letter_redrives dr ON dr.dead_letter_id=dlq.id
                      WHERE {' AND '.join(where)}
                      GROUP BY dlq.id ORDER BY dlq.id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/dead-letters/<int:dead_letter_id>/redrive")
    def redrive_dead_letter(dead_letter_id: int) -> Any:
        body = request.get_json(silent=True) or {}
        reason = str(body.get("reason", "Controlled dashboard redrive")).strip()[:1000]
        requested_by = str(body.get("requested_by", "dashboard")).strip()[:120] or "dashboard"
        with connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                source = connection.execute(
                    "SELECT event_type,payload FROM dead_letter_queue WHERE id=?",
                    (dead_letter_id,),
                ).fetchone()
                if source is None:
                    connection.rollback()
                    abort(404, description="Dead-letter item niet gevonden")
                payload = json.loads(source["payload"])
                if not isinstance(payload, dict):
                    raise ValueError("DLQ payload is geen JSON-object")
                history = payload.get("redrive_history", [])
                if not isinstance(history, list):
                    history = []
                payload["redrive_history"] = [
                    *history,
                    {"dead_letter_id": dead_letter_id, "requested_by": requested_by,
                     "reason": reason, "at": datetime.now(timezone.utc).isoformat()},
                ]
                cursor = connection.execute(
                    "INSERT INTO events_queue(event_type,payload) VALUES (?,?)",
                    (source["event_type"], json.dumps(payload, ensure_ascii=False)),
                )
                new_event_id = int(cursor.lastrowid)
                connection.execute(
                    """INSERT INTO dead_letter_redrives(
                           dead_letter_id,new_event_id,event_type,requested_by,reason
                       ) VALUES (?,?,?,?,?)""",
                    (dead_letter_id, new_event_id, source["event_type"], requested_by, reason),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return jsonify({"dead_letter_id": dead_letter_id, "new_event_id": new_event_id, "status": "PENDING"}), 201

    @app.get("/api/session-health")
    def session_health() -> Any:
        with connect() as connection:
            rows = connection.execute(
                """SELECT platform,plugin_name,auth_file,status,detail,checked_at
                     FROM session_health ORDER BY platform"""
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/notifications")
    def notifications() -> Any:
        unread_only = request.args.get("unread", "0") in {"1", "true"}
        with connect() as connection:
            rows = connection.execute(
                """SELECT id,severity,title,message,platform,is_read,created_at
                     FROM system_notifications WHERE (?=0 OR is_read=0)
                     ORDER BY id DESC LIMIT 50""",
                (int(unread_only),),
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.patch("/api/notifications/<int:notification_id>")
    def read_notification(notification_id: int) -> Any:
        with connect() as connection:
            cursor = connection.execute(
                "UPDATE system_notifications SET is_read=1 WHERE id=?",
                (notification_id,),
            )
            connection.commit()
        if cursor.rowcount != 1:
            abort(404, description="Notificatie niet gevonden")
        return jsonify({"id": notification_id, "is_read": True})

    @app.get("/api/routes")
    def routes() -> Any:
        """Return event routes backed by active plugins."""
        with connect() as connection:
            rows = connection.execute(
                """SELECT er.event_type,er.target_plugin_name,pr.icon
                     FROM event_routes er
                     JOIN plugin_registry pr ON pr.plugin_name=er.target_plugin_name
                    WHERE pr.is_active=1 ORDER BY er.event_type"""
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/scheduled-events")
    def scheduled_events() -> Any:
        start = request.args.get("start", "").strip()
        end = request.args.get("end", "").strip()
        where: list[str] = []
        parameters: list[str] = []
        for value, operator, label in ((start, ">=", "start"), (end, "<", "end")):
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                abort(400, description=f"{label} moet geldige ISO-8601 zijn")
            if parsed.tzinfo is None:
                abort(400, description=f"{label} moet een tijdzone bevatten")
            where.append(f"scheduled_time {operator} ?")
            parameters.append(parsed.astimezone(timezone.utc).isoformat(timespec="seconds"))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with connect() as connection:
            rows = connection.execute(
                f"""SELECT id,event_type,payload,scheduled_time,status
                       FROM scheduled_events {clause}
                      ORDER BY scheduled_time ASC,id ASC LIMIT 500""",
                parameters,
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            try:
                record["payload"] = json.loads(record["payload"])
            except json.JSONDecodeError:
                record["payload"] = {}
            record["media"] = payload_media(record["payload"])
            records.append(record)
        return jsonify(records)

    @app.get("/api/planning-feed")
    def planning_feed() -> Any:
        """Return filterable draft, schedule, publication, and error records."""
        records: list[dict[str, Any]] = []
        concept_root = VAULT_ROOT / "concepten"
        for path in sorted(concept_root.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
            item = artifact_record(path)
            item.update({"kind": "draft", "status_group": "DRAFT", "channel": None, "content_type": "text"})
            records.append(item)
        with connect() as connection:
            schedules = connection.execute(
                "SELECT id,event_type,payload,scheduled_time,status FROM scheduled_events ORDER BY scheduled_time DESC LIMIT 250"
            ).fetchall()
            failures = connection.execute(
                """SELECT id,event_type,payload,updated_at,error_log FROM events_queue
                    WHERE status='FAILED' ORDER BY id DESC LIMIT 100"""
            ).fetchall()
        for row in schedules:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {}
            media = payload_media(payload)
            records.append(
                {
                    "id": row["id"], "kind": "schedule", "channel": row["event_type"],
                    "status_group": "ERROR" if row["status"] in {"FAILED", "BLOCKED_AUTH", "CANCELLED"} else "SCHEDULED",
                    "status": row["status"], "timestamp": row["scheduled_time"], "payload": payload,
                    "content_type": media["content_type"] if media else payload.get("content_type", "text"), "media": media,
                }
            )
        publication_root = VAULT_ROOT / "gepubliceerd"
        for path in sorted(publication_root.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
            item = artifact_record(path)
            item.update({"kind": "publication", "status_group": "PUBLISHED", "channel": None, "content_type": "text"})
            records.append(item)
        for row in failures:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {}
            media = payload_media(payload)
            records.append(
                {"id": row["id"], "kind": "event", "channel": row["event_type"], "status_group": "ERROR",
                 "status": "FAILED", "timestamp": row["updated_at"], "error_log": row["error_log"],
                 "payload": payload, "content_type": media["content_type"] if media else payload.get("content_type", "text"), "media": media}
            )
        return jsonify(records)

    @app.get("/api/media")
    def media_library() -> Any:
        root = VAULT_ROOT / "media"
        root.mkdir(parents=True, exist_ok=True)
        paths = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS]
        return jsonify([media_record(path) for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True)])

    @app.get("/api/media-store/sources")
    def media_store_sources() -> Any:
        with connect() as connection:
            rows = connection.execute(
                """SELECT ms.id,ms.source_key,ms.display_name,ms.provider,ms.is_active,
                          ms.last_indexed_at,COUNT(CASE WHEN ma.is_available=1 THEN 1 END) asset_count
                     FROM media_sources ms LEFT JOIN media_assets ma ON ma.source_id=ms.id
                    GROUP BY ms.id ORDER BY ms.display_name"""
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/media-store/assets")
    def media_store_assets() -> Any:
        query = request.args.get("q", "").strip()
        source = request.args.get("source", "").strip()
        media_type = request.args.get("type", "").strip()
        if media_type and media_type not in {"image", "video"}:
            abort(400, description="type moet image of video zijn")
        where = ["ma.is_available=1", "ms.is_active=1"]
        parameters: list[Any] = []
        if query:
            where.append("(ma.filename LIKE ? ESCAPE '\\' OR COALESCE(ma.prompt,'') LIKE ? ESCAPE '\\' OR ma.metadata LIKE ? ESCAPE '\\')")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.extend([f"%{escaped}%"] * 3)
        if source:
            where.append("ms.source_key=?")
            parameters.append(source)
        if media_type:
            where.append("ma.media_type=?")
            parameters.append(media_type)
        with connect() as connection:
            rows = connection.execute(
                f"""SELECT ma.*,ms.source_key,ms.display_name source_name,ms.root_path
                       FROM media_assets ma JOIN media_sources ms ON ms.id=ma.source_id
                      WHERE {' AND '.join(where)} ORDER BY ma.modified_at DESC,ma.id DESC LIMIT 250""",
                parameters,
            ).fetchall()
        return jsonify([indexed_asset_record(row) for row in rows])

    @app.get("/api/media-store/targets")
    def media_store_targets() -> Any:
        drafts = [
            {"target_type": "DRAFT", "target_ref": str(path.relative_to(VAULT_ROOT / "concepten")), "label": path.name}
            for path in sorted((VAULT_ROOT / "concepten").glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
        ]
        with connect() as connection:
            schedules = connection.execute(
                "SELECT id,event_type,payload,scheduled_time FROM scheduled_events WHERE status='PENDING' ORDER BY scheduled_time"
            ).fetchall()
        for row in schedules:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {}
            drafts.append({
                "target_type": "SCHEDULE", "target_ref": str(row["id"]),
                "label": f"{row['event_type']} · {row['scheduled_time']} · {Path(payload.get('draft_file', '')).name}",
            })
        return jsonify(drafts)

    @app.get("/api/media-store/assets/<int:asset_id>/content")
    def media_store_content(asset_id: int) -> Any:
        with connect() as connection:
            row = indexed_asset(connection, asset_id)
            path = validated_indexed_file(row)
        return send_file(path, conditional=True)

    @app.post("/api/media-store/assets/<int:asset_id>/link")
    def link_media_asset(asset_id: int) -> Any:
        body = request.get_json(silent=True) or {}
        target_type = str(body.get("target_type", "")).upper()
        target_ref = body.get("target_ref")
        if target_type not in {"DRAFT", "SCHEDULE"} or not isinstance(target_ref, str) or not target_ref:
            abort(400, description="target_type en target_ref zijn verplicht")
        with connect() as connection:
            asset = indexed_asset(connection, asset_id)
            media_path = validated_indexed_file(asset)
            if target_type == "DRAFT":
                draft = safe_markdown_path("concepten", target_ref)
                if not draft.is_file():
                    abort(404, description="Draft niet gevonden")
                canonical_ref = str(draft.relative_to(VAULT_ROOT / "concepten"))
            else:
                try:
                    schedule_id = int(target_ref)
                except ValueError:
                    abort(400, description="Ongeldig planning-id")
                row = connection.execute("SELECT payload,status FROM scheduled_events WHERE id=?", (schedule_id,)).fetchone()
                if row is None:
                    abort(404, description="Planning niet gevonden")
                if row["status"] != "PENDING":
                    abort(409, description="Media kan alleen aan een PENDING planning worden gekoppeld")
                payload = json.loads(row["payload"])
                payload.update({
                    "media_asset_id": asset_id, "media_path": str(media_path),
                    "content_type": asset["media_type"],
                    "image_path" if asset["media_type"] == "image" else "video_path": str(media_path),
                })
                connection.execute("UPDATE scheduled_events SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), schedule_id))
                canonical_ref = str(schedule_id)
            connection.execute(
                """INSERT INTO media_links(asset_id,target_type,target_ref) VALUES (?,?,?)
                   ON CONFLICT(asset_id,target_type,target_ref) DO NOTHING""",
                (asset_id, target_type, canonical_ref),
            )
            connection.commit()
        return jsonify({"linked": True, "asset_id": asset_id, "target_type": target_type, "target_ref": canonical_ref})

    @app.post("/api/media")
    def upload_media() -> Any:
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            abort(400, description="Selecteer een mediabestand")
        filename = secure_filename(upload.filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in MEDIA_EXTENSIONS:
            abort(400, description="Alleen JPG, PNG, GIF, WebP, MP4, WebM en MOV zijn toegestaan")
        header = upload.stream.read(32)
        upload.stream.seek(0)
        if not valid_media_signature(suffix, header):
            abort(400, description="Bestandsinhoud komt niet overeen met het mediaformaat")
        root = VAULT_ROOT / "media"
        root.mkdir(parents=True, exist_ok=True)
        unique_id = uuid4().hex
        destination = root / f"{Path(filename).stem}_{unique_id[:10]}{suffix}"
        temporary = root / f".{destination.name}.{uuid4().hex}.part"
        try:
            upload.save(temporary)
            if temporary.stat().st_size <= 0:
                abort(400, description="Leeg bestand is niet toegestaan")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        stat_result = destination.stat()
        modified = datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat()
        metadata = json.dumps(
            {
                "original_filename": upload.filename,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "source": "dashboard_upload",
            },
            ensure_ascii=False,
        )
        try:
            with connect() as connection:
                connection.execute(
                """INSERT INTO media_sources(source_key,display_name,provider,root_path,config,is_active,last_indexed_at)
                   VALUES ('vault-uploads','Dashboard Uploads','vault-upload',?,'{}',1,CURRENT_TIMESTAMP)
                   ON CONFLICT(source_key) DO UPDATE SET root_path=excluded.root_path,
                       is_active=1,last_indexed_at=CURRENT_TIMESTAMP""",
                    (str(root.resolve()),),
                )
                source_id = connection.execute(
                    "SELECT id FROM media_sources WHERE source_key='vault-uploads'"
                ).fetchone()[0]
                cursor = connection.execute(
                """INSERT INTO media_assets(source_id,external_id,filename,file_path,thumbnail_path,
                       media_type,mime_type,file_size,modified_at,metadata,is_available)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                    (source_id, unique_id, destination.name, str(destination.resolve()),
                     str(destination.resolve()) if MEDIA_EXTENSIONS[suffix] == "image" else None,
                     MEDIA_EXTENSIONS[suffix], MEDIA_MIME_TYPES[suffix], stat_result.st_size,
                     modified, metadata),
                )
                asset_id = int(cursor.lastrowid)
                row = indexed_asset(connection, asset_id)
                connection.commit()
        except sqlite3.Error:
            destination.unlink(missing_ok=True)
            raise
        result = indexed_asset_record(row)
        result.update(media_record(destination))
        result["id"] = asset_id
        result["source_key"] = "vault-uploads"
        result["source_name"] = "Dashboard Uploads"
        result["url"] = f"/api/media-store/assets/{asset_id}/content"
        return jsonify(result), 201

    @app.get("/api/media/<path:relative_path>")
    def serve_media(relative_path: str) -> Any:
        return send_file(safe_media_path(relative_path), conditional=True)

    @app.post("/api/scheduled-events")
    def create_scheduled_event() -> Any:
        body = request.get_json(silent=True) or {}
        event_types = body.get("event_types")
        if event_types is None and isinstance(body.get("event_type"), str):
            event_types = [body["event_type"]]
        scheduled_time = body.get("scheduled_time")
        draft_path = body.get("draft_file")
        if not isinstance(event_types, list) or not event_types or not all(isinstance(value, str) and value.strip() for value in event_types):
            abort(400, description="event_types moet een niet-lege lijst zijn")
        event_types = list(dict.fromkeys(event_types))
        if not all(isinstance(value, str) and value.strip() for value in (scheduled_time, draft_path)):
            abort(400, description="scheduled_time en draft_file zijn verplicht")
        try:
            parsed_time = datetime.fromisoformat(scheduled_time.replace("Z", "+00:00"))
        except ValueError:
            abort(400, description="scheduled_time moet geldige ISO-8601 zijn")
        if parsed_time.tzinfo is None:
            abort(400, description="scheduled_time moet een tijdzone bevatten")
        draft = safe_markdown_path("concepten", draft_path)
        if not draft.is_file():
            abort(404, description="Draft niet gevonden")
        canonical_time = parsed_time.astimezone(timezone.utc).isoformat(timespec="seconds")
        media_path = body.get("media_path")
        media: Path | None = None
        content_type = "text"
        if media_path:
            if not isinstance(media_path, str):
                abort(400, description="media_path moet tekst zijn")
            media = safe_media_path(media_path)
            content_type = MEDIA_EXTENSIONS[media.suffix.lower()]
        schedule_ids: list[int] = []
        with connect() as connection:
            if media is None:
                linked = connection.execute(
                    """SELECT ma.* FROM media_links ml JOIN media_assets ma ON ma.id=ml.asset_id
                        JOIN media_sources ms ON ms.id=ma.source_id
                       WHERE ml.target_type='DRAFT' AND ml.target_ref=?
                         AND ma.is_available=1 AND ms.is_active=1
                       ORDER BY ml.created_at DESC,ml.id DESC LIMIT 1""",
                    (str(draft.relative_to(VAULT_ROOT / "concepten")),),
                ).fetchone()
                if linked is not None:
                    media = Path(linked["file_path"]).expanduser().resolve()
                    content_type = linked["media_type"]
            for event_type in event_types:
                route = connection.execute(
                    """SELECT 1 FROM event_routes er JOIN plugin_registry pr
                         ON pr.plugin_name=er.target_plugin_name
                        WHERE er.event_type=? AND pr.is_active=1""",
                    (event_type,),
                ).fetchone()
                if route is None:
                    abort(409, description=f"Geen actieve route voor {event_type}")
            for event_type in event_types:
                payload_data = {
                    "draft_file": str(draft),
                    "source": "dashboard_scheduler",
                    "content_type": content_type,
                }
                if media:
                    payload_data["media_path"] = str(media)
                    payload_data["image_path" if content_type == "image" else "video_path"] = str(media)
                cursor = connection.execute(
                    "INSERT INTO scheduled_events(event_type,payload,scheduled_time) VALUES (?,?,?)",
                    (event_type, json.dumps(payload_data, ensure_ascii=False), canonical_time),
                )
                schedule_ids.append(int(cursor.lastrowid))
            connection.commit()
        return jsonify({"ids": schedule_ids, "status": "PENDING", "scheduled_time": canonical_time}), 201

    @app.delete("/api/scheduled-events/<int:schedule_id>")
    def cancel_scheduled_event(schedule_id: int) -> Any:
        with connect() as connection:
            cursor = connection.execute(
                "UPDATE scheduled_events SET status='CANCELLED' WHERE id=? AND status='PENDING'",
                (schedule_id,),
            )
            connection.commit()
        if cursor.rowcount != 1:
            abort(409, description="Alleen een bestaande PENDING planning kan worden geannuleerd")
        return jsonify({"id": schedule_id, "status": "CANCELLED"})

    @app.get("/api/inbound/telegram")
    def telegram_inbound_status() -> Any:
        with connect() as connection:
            plugin = connection.execute(
                "SELECT plugin_name,is_active FROM plugin_registry WHERE plugin_name='Telegram Inbound Hub'"
            ).fetchone()
            recent = connection.execute(
                """SELECT id,payload,created_at FROM events_queue
                    WHERE event_type='TELEGRAM_INBOUND' ORDER BY id DESC LIMIT 10"""
            ).fetchall()
        items = []
        for row in recent:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {}
            items.append({"id": row["id"], "created_at": row["created_at"], "topic": payload.get("topic"), "draft_file": payload.get("draft_file")})
        return jsonify(
            {
                "registered": plugin is not None,
                "active": bool(plugin and plugin["is_active"]),
                "token_configured": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
                "media_files": len(list((VAULT_ROOT / "media").glob("*"))),
                "recent": items,
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

    @app.get("/api/plugins/<path:plugin_name>/settings")
    def plugin_settings(plugin_name: str) -> Any:
        """Return settings only for an active plugin."""
        with connect() as connection:
            plugin = active_plugin(connection, plugin_name)
        response = {"plugin": dict(plugin), "auth": auth_status(plugin_name)}
        if plugin_name == "Image Overlay Processor":
            response["overlay"] = {"title_size": 64, "subtitle_size": 38, "meaning_size": 30,
                                    "position": "center", "border": 24}
        return jsonify(response)

    @app.post("/api/plugins/<path:plugin_name>/overlay")
    def apply_plugin_overlay(plugin_name: str) -> Any:
        if plugin_name != "Image Overlay Processor":
            abort(404, description="Overlay-instellingen niet beschikbaar voor deze plugin")
        with connect() as connection:
            active_plugin(connection, plugin_name)
        body = request.get_json(silent=True) or {}
        relative = body.get("input")
        source = safe_media_path(relative) if isinstance(relative, str) else None
        if source is None and isinstance(body.get("asset_id"), int):
            with connect() as connection:
                source = Path(indexed_asset(connection, int(body["asset_id"]))["file_path"]).resolve()
        if source is None:
            abort(400, description="input mediapad is verplicht")
        from plugins.media.image_overlay import apply_overlay
        title, phonetic, meaning = (str(body.get(key, "")).strip() for key in ("title", "phonetic", "meaning"))
        lines = "\n".join(value for value in (title, phonetic, meaning) if value)
        output = (VAULT_ROOT / "media" / "published" / f"{source.stem}_overlay.jpg").resolve()
        apply_overlay(source, output, text=lines, border=int(body.get("border", 24)), font_size=int(body.get("title_size", 64)))
        return jsonify({"status": "COMPLETED", "output": str(output)})

    @app.post("/api/plugins/<path:plugin_name>/authenticate")
    def authenticate_plugin(plugin_name: str) -> Any:
        """Start a detached headed login helper for a supported active plugin."""
        with connect() as connection:
            active_plugin(connection, plugin_name)
        profile = AUTH_PROFILES.get(plugin_name)
        if profile is None:
            abort(409, description="Deze plugin vereist geen authenticatie")

        auth_file = Path(profile["auth_file"])
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        helper = Path(__file__).resolve().parent / "authenticate.py"
        command = [
            sys.executable,
            str(helper),
            "--platform",
            str(profile["platform"]),
            "--auth-file",
            str(auth_file),
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            return jsonify({"error": f"Loginbrowser kon niet starten: {exc}"}), 503
        return (
            jsonify(
                {
                    "started": True,
                    "pid": process.pid,
                    "platform": profile["platform"],
                    "message": "Browser gestart. Rond de login af; status wordt automatisch vernieuwd.",
                }
            ),
            202,
        )

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
