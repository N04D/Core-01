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
from core.keyring_store import set_secret
from core.paths import CORE_ROOT, CONCEPTS_DIR, DATABASE_PATH, OUTGOING_DIR, SESSIONS_DIR


PROJECT_ROOT: Final = CORE_ROOT
DEFAULT_DATABASE: Final = DATABASE_PATH
VAULT_ROOT: Final = CORE_ROOT / "vault"  # source templates remain in the repository
EDITABLE_AREAS: Final = {"concepten", "uitgaand"}
EDITOR_AREAS: Final = {"concepten": CONCEPTS_DIR, "uitgaand": OUTGOING_DIR}
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
        "auth_file": SESSIONS_DIR / "linkedin_auth.json",
    },
    "LinkedIn Pro Publisher & Analytics": {
        "platform": "linkedin",
        "auth_file": SESSIONS_DIR / "linkedin_auth.json",
    },
    "Substack Publisher": {
        "platform": "substack",
        "auth_file": SESSIONS_DIR / "substack_auth.json",
    },
    "Substack Pro Publisher & Analytics": {
        "platform": "substack",
        "auth_file": SESSIONS_DIR / "substack_auth.json",
    },
    "Medium Publisher": {
        "platform": "medium",
        "auth_file": SESSIONS_DIR / "medium_auth.json",
    },
}
AUTH_PROCESSES: dict[str, subprocess.Popen[Any]] = {}


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
    from core.scheduler import start_scheduler
    app.extensions["job_scheduler"] = start_scheduler(app.config["DATABASE"], float(os.getenv("SCHEDULER_INTERVAL_SECONDS", "30")))

    def connect() -> sqlite3.Connection:
        return connect_database(app.config["DATABASE"], timeout=10.0)

    def safe_markdown_path(area: str, relative_path: str) -> Path:
        if area not in EDITABLE_AREAS:
            abort(400, description="Onbekend vaultgebied")
        root = EDITOR_AREAS[area].resolve()
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
                "drafts": len(list(EDITOR_AREAS["concepten"].glob("*.md"))),
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
        # The health daemon may run infrequently; derive the displayed status
        # from the auth file on every request so a freshly captured session is
        # immediately reflected on Home.
        with connect() as connection:
            plugins = connection.execute(
                "SELECT plugin_name FROM plugin_registry WHERE is_active=1 ORDER BY plugin_name"
            ).fetchall()
            checked = {
                row["plugin_name"]: dict(row)
                for row in connection.execute(
                    "SELECT plugin_name,checked_at FROM session_health"
                ).fetchall()
            }
        result = []
        for plugin in plugins:
            name = plugin["plugin_name"]
            profile = AUTH_PROFILES.get(name)
            if profile is None:
                continue
            auth = auth_status(name)
            result.append({
                "platform": profile["platform"],
                "plugin_name": name,
                "auth_file": str(profile["auth_file"]),
                "status": auth["status"],
                "detail": auth["message"],
                "checked_at": checked.get(name, {}).get("checked_at"),
            })
        return jsonify(result)

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

    @app.get("/api/publication-attempts/unresolved")
    def unresolved_publications() -> Any:
        with connect() as connection:
            rows = connection.execute(
                "SELECT * FROM publication_attempts WHERE status IN ('SUBMITTED','UNKNOWN') ORDER BY updated_at,id"
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/publication-attempts/operator-queue")
    def operator_publications() -> Any:
        with connect() as connection:
            rows = connection.execute(
                "SELECT * FROM publication_attempts WHERE status='NEEDS_OPERATOR' ORDER BY updated_at,id"
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/publication-attempts/<int:attempt_id>/operator-resolve")
    def operator_resolve_publication(attempt_id: int) -> Any:
        body = request.get_json(silent=True) or {}
        status = str(body.get("status", "NEEDS_OPERATOR")).upper()
        if status not in {"CONFIRMED", "FAILED", "NEEDS_OPERATOR"}:
            abort(400, description="status must be CONFIRMED, FAILED or NEEDS_OPERATOR")
        detail = str(body.get("detail", "Operator resolution"))[:8000]
        with connect() as connection:
            row = connection.execute("SELECT event_id,channel FROM publication_attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None: abort(404, description="publication attempt not found")
            connection.execute("UPDATE publication_attempts SET status=?,detail=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, detail, attempt_id))
            connection.commit()
        return jsonify({"id": attempt_id, "status": status, "detail": detail})

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
        concept_root = EDITOR_AREAS["concepten"]
        for path in sorted(concept_root.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
            item = {"name": path.name, "path": str(path.relative_to(concept_root)), "size": path.stat().st_size, "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()}
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

    @app.get("/api/playbooks")
    def playbooks() -> Any:
        records = []
        for path in sorted((PROJECT_ROOT / "playbooks").glob("*.py")):
            if path.name.startswith("__"): continue
            item = {"name": path.stem, "path": str(path.relative_to(PROJECT_ROOT)), "description": ""}
            try:
                text = path.read_text(encoding="utf-8")[:1200]
                item["description"] = text.split('"""', 2)[1].strip().splitlines()[0] if '"""' in text else ""
            except OSError: pass
            contract = PROJECT_ROOT / "playbooks" / "contracts" / f"{path.stem}.contract.json"
            if contract.is_file():
                try: item["contract"] = json.loads(contract.read_text(encoding="utf-8"))
                except json.JSONDecodeError: item["contract"] = None
            records.append(item)
        return jsonify(records)

    @app.get("/api/jobs")
    def jobs() -> Any:
        with connect() as connection:
            return jsonify([dict(row) for row in connection.execute("SELECT * FROM scheduled_jobs ORDER BY next_run_at,id")])

    @app.post("/api/jobs")
    def create_job() -> Any:
        from core.scheduler import add_job
        body = request.get_json(silent=True) or {}
        try:
            job_id = add_job(app.config["DATABASE"], str(body["playbook"]), str(body["scheduled_time"]), str(body.get("frequency", "ONCE")), body.get("payload") or {})
        except (KeyError, ValueError) as exc: abort(400, description=str(exc))
        return jsonify({"id": job_id}), 201

    @app.patch("/api/jobs/<int:job_id>")
    def update_job(job_id: int) -> Any:
        status = (request.get_json(silent=True) or {}).get("status")
        if status not in {"ACTIVE", "PAUSED", "CANCELLED"}: abort(400, description="ongeldige jobstatus")
        with connect() as connection:
            cur = connection.execute("UPDATE scheduled_jobs SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, job_id)); connection.commit()
        if cur.rowcount != 1: abort(404)
        return jsonify({"id": job_id, "status": status})

    @app.delete("/api/jobs/<int:job_id>")
    def delete_job(job_id: int) -> Any:
        with connect() as connection:
            cur = connection.execute("DELETE FROM scheduled_jobs WHERE id=?", (job_id,)); connection.commit()
        if cur.rowcount != 1: abort(404)
        return jsonify({"deleted": job_id})

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
            {"target_type": "DRAFT", "target_ref": str(path.relative_to(EDITOR_AREAS["concepten"])), "label": path.name}
            for path in sorted(EDITOR_AREAS["concepten"].glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
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

    @app.post("/api/nightcafe/generations")
    def start_nightcafe_generation() -> Any:
        body = request.get_json(silent=True) or {}
        prompt = str(body.get("prompt", "")).strip()
        if not prompt: abort(400, description="prompt is verplicht")
        payload = {key: body[key] for key in ("prompt", "model", "format", "negative_prompt", "steps", "live", "simulate") if key in body}
        payload["mode"] = "SIMULATED" if body.get("simulate", False) else "LIVE"
        with connect() as connection:
            route = connection.execute("SELECT target_plugin_name FROM event_routes WHERE event_type='NIGHTCAFE_GENERATE'").fetchone()
            if route is None: abort(503, description="NIGHTCAFE_GENERATE route is not registered")
            cursor = connection.execute("INSERT INTO events_queue(event_type,payload,status,next_attempt_at) VALUES (?,?, 'PENDING', CURRENT_TIMESTAMP)", ("NIGHTCAFE_GENERATE", json.dumps(payload, ensure_ascii=False)))
            connection.commit()
            event_id = int(cursor.lastrowid)
        return jsonify({"id": str(event_id), "job_id": str(event_id), "event_id": event_id, "status": "PENDING"}), 202

    @app.get("/api/nightcafe/generations/<job_id>")
    def nightcafe_generation_status(job_id: str) -> Any:
        try: event_id = int(job_id)
        except ValueError: abort(400, description="ongeldig generatie-id")
        with connect() as connection:
            row = connection.execute("SELECT id,status,payload,error_log,created_at,updated_at FROM events_queue WHERE id=? AND event_type='NIGHTCAFE_GENERATE'", (event_id,)).fetchone()
        if row is None: abort(404, description="generatie niet gevonden")
        payload = json.loads(row["payload"] or "{}")
        return jsonify({"id": str(row["id"]), "job_id": str(row["id"]), "event_id": row["id"], "status": row["status"], "result": payload.get("result") or payload.get("nightcafe"), "error": row["error_log"], "created_at": row["created_at"], "updated_at": row["updated_at"]})

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
                canonical_ref = str(draft.relative_to(EDITOR_AREAS["concepten"]))
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
                    (str(draft.relative_to(EDITOR_AREAS["concepten"])),),
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
        response: dict[str, Any] = {"plugin": dict(plugin), "auth": auth_status(plugin_name)}
        if plugin_name == "Markdown Website Git Publisher":
            config_file = Path(os.getenv("MARKDOWN_GIT_CONFIG", PROJECT_ROOT / "config" / "markdown_git.json"))
            config_values: dict[str, Any] = {}
            if config_file.is_file():
                try:
                    loaded = json.loads(config_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        config_values = loaded
                except (OSError, json.JSONDecodeError):
                    response["config_error"] = "invalid Markdown Git configuration"
            response["config"] = {
                "config_file": str(config_file.expanduser().resolve()),
                "configured": config_file.is_file(),
                "repository_path": os.getenv("MARKDOWN_GIT_REPOSITORY_PATH", config_values.get("repository_path", "")),
                "content_directory": os.getenv("MARKDOWN_GIT_CONTENT_DIRECTORY", config_values.get("content_directory", "content")),
                "media_directory": os.getenv("MARKDOWN_GIT_MEDIA_DIRECTORY", config_values.get("media_directory", "static/media")),
                "push_enabled": os.getenv("MARKDOWN_GIT_PUSH_ENABLED", str(config_values.get("push_enabled", False))).lower() in {"1", "true", "yes", "on"},
                "commit_enabled": os.getenv("MARKDOWN_GIT_COMMIT_ENABLED", str(config_values.get("commit_enabled", False))).lower() in {"1", "true", "yes", "on"},
            }
        return jsonify(response)

    @app.get("/api/publication-attempts")
    def publication_attempts() -> Any:
        """Return ledger history, optionally filtered for dashboard views."""
        status = request.args.get("status", "").strip().upper()
        channel = request.args.get("channel", "").strip()
        limit = min(max(int(request.args.get("limit", "100")), 1), 500)
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if channel:
            clauses.append("channel=?")
            params.append(channel)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM publication_attempts {where} ORDER BY updated_at DESC,id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/overlay-formats")
    def overlay_formats() -> Any:
        with connect() as connection:
            rows = connection.execute("SELECT * FROM overlay_formats ORDER BY is_default DESC,name").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["lines"] = json.loads(item["lines"])
            except (TypeError, json.JSONDecodeError): item["lines"] = []
            result.append(item)
        return jsonify(result)

    @app.post("/api/overlay-formats")
    def create_overlay_format() -> Any:
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", "")).strip()
        lines = body.get("lines")
        if not name or not isinstance(lines, list) or not lines or any(not isinstance(line, str) or not line.strip() for line in lines):
            abort(400, description="naam en minimaal één tekstregel zijn verplicht")
        position = str(body.get("position", "center"));
        if position not in {"center", "top", "bottom"}: abort(400, description="ongeldige positie")
        try:
            title_size = max(12, min(240, int(body.get("title_size", 96))))
            subtitle_size = max(10, min(180, int(body.get("subtitle_size", 48))))
            border = max(0, min(200, int(body.get("border", 24))))
        except (TypeError, ValueError): abort(400, description="lettergroottes en kader moeten numeriek zijn")
        try:
            x_percent = max(0, min(100, int(body.get("x_percent", 50))))
            y_percent = max(0, min(100, int(body.get("y_percent", 50))))
        except (TypeError, ValueError): abort(400, description="positie moet numeriek zijn")
        line_settings = body.get("line_settings", [])
        if not isinstance(line_settings, list) or len(line_settings) > 3 or any(not isinstance(item, dict) for item in line_settings):
            abort(400, description="line_settings moet een lijst zijn")
        with connect() as connection:
            try:
                cur = connection.execute("INSERT INTO overlay_formats(name,lines,title_size,subtitle_size,position,border,x_percent,y_percent,line_settings) VALUES (?,?,?,?,?,?,?,?,?)", (name, json.dumps(lines, ensure_ascii=False), title_size, subtitle_size, position, border, x_percent, y_percent, json.dumps(line_settings, ensure_ascii=False))); connection.commit()
                if connection.execute("SELECT COUNT(*) FROM overlay_formats WHERE is_default=1").fetchone()[0] == 0:
                    connection.execute("UPDATE overlay_formats SET is_default=1 WHERE id=?", (cur.lastrowid,)); connection.commit()
            except sqlite3.IntegrityError: abort(409, description="formatnaam bestaat al")
        return jsonify({"id": cur.lastrowid, "name": name}), 201

    @app.delete("/api/overlay-formats/<int:format_id>")
    def delete_overlay_format(format_id: int) -> Any:
        with connect() as connection:
            cur = connection.execute("DELETE FROM overlay_formats WHERE id=?", (format_id,)); connection.commit()
        if cur.rowcount != 1: abort(404)
        return jsonify({"deleted": format_id})

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
        existing = AUTH_PROCESSES.get(plugin_name)
        if existing is not None and existing.poll() is None:
            return jsonify({"started": True, "pid": existing.pid, "already_running": True,
                            "message": "Loginbrowser draait al. Rond de login af in het bestaande venster."}), 202
        helper = Path(__file__).resolve().parent / "authenticate.py"
        command = [
            sys.executable,
            str(helper),
            "--platform",
            str(profile["platform"]),
            "--auth-file",
            str(auth_file),
        ]
        # Session capture should reuse the user's already-open Chrome tab.
        # A CDP URL can be overridden for remote hosts; localhost is the
        # standard browser service used by this installation.
        cdp_url = os.getenv("BROWSER_CDP_URL") or os.getenv("LINKEDIN_CDP_URL")
        # All browser-backed plugins share the managed Chrome session.
        cdp_url = cdp_url or "http://127.0.0.1:9222"
        if cdp_url:
            command.extend(["--cdp-url", cdp_url])
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
        AUTH_PROCESSES[plugin_name] = process
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

    @app.post("/api/plugins/<path:plugin_name>/credentials")
    def store_plugin_credentials(plugin_name: str) -> Any:
        """Store username/password in the OS keyring; never persist plaintext."""
        with connect() as connection:
            active_plugin(connection, plugin_name)
        if plugin_name not in {"LinkedIn Pro Publisher & Analytics", "LinkedIn Publisher (Productie)"}:
            abort(409, description="Credentialopslag is momenteel alleen beschikbaar voor LinkedIn")
        body = request.get_json(silent=True) or {}
        username, password = str(body.get("username", "")).strip(), str(body.get("password", ""))
        if not username or not password:
            abort(400, description="gebruikersnaam en wachtwoord zijn verplicht")
        try:
            set_secret("linkedin.username", username)
            set_secret("linkedin.password", password)
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"stored": True, "message": "LinkedIn-gegevens veilig opgeslagen in de OS-keyring."})

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
        root = EDITOR_AREAS[area]
        root.mkdir(parents=True, exist_ok=True)
        return jsonify(
            [{"name": path.name, "path": str(path.relative_to(root)), "size": path.stat().st_size, "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()} for path in sorted(root.rglob("*.md"), reverse=True)]
        )

    @app.get("/api/files/<area>/<path:relative_path>")
    def read_file(area: str, relative_path: str) -> Any:
        path = safe_markdown_path(area, relative_path)
        if not path.is_file():
            abort(404, description="Bestand niet gevonden")
        return jsonify(
            {
                "name": path.name,
                "path": str(path.relative_to(EDITOR_AREAS[area])),
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
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return jsonify({"saved": True, "path": f"{area}/{path.relative_to(EDITOR_AREAS[area])}"})

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
        mode = request.args.get("mode", "REAL").upper()
        if mode not in {"REAL", "SIMULATED", "ALL"}:
            abort(400, description="mode must be REAL, SIMULATED or ALL")
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
                    artifact_mode = "SIMULATED" if isinstance(data, dict) and str(data.get("mode", "")).upper() == "SIMULATED" else "REAL"
                    if mode != "ALL" and artifact_mode != mode:
                        continue
                    records.append(
                        {
                            "plugin_name": plugin_name,
                            "file": path.name,
                            "data": data,
                        }
                    )
        return jsonify(records)

    @app.get("/api/analytics/publications")
    def analytics_publications() -> Any:
        """Return latest normalized performance for attributed publications."""
        channel = request.args.get("channel")
        mode = request.args.get("mode", "REAL").upper()
        if mode not in {"REAL", "SIMULATED", "ALL"}:
            abort(400, description="mode must be REAL, SIMULATED or ALL")
        limit = min(max(int(request.args.get("limit", "100")), 1), 500)
        clauses = ["1=1"]
        params: list[Any] = []
        performance_mode = "" if mode == "ALL" else "AND mode=?"
        if mode != "ALL":
            params.append(mode)
        if channel:
            clauses.append("pa.channel=?")
            params.append(channel)
        params.append(limit)
        with connect() as connection:
            rows = connection.execute(
                f"""SELECT pa.id AS publication_attempt_id,pa.event_id,pa.channel,pa.platform_url,
                           pa.updated_at,cp.provider,cp.window,cp.snapshot_id,cp.views,cp.impressions,
                           cp.unique_views,cp.clicks,cp.reactions,cp.likes,cp.comments,cp.shares,
                           cp.saves,cp.engagements,cp.engagement_rate,cp.click_rate
                      FROM publication_attempts pa
                      LEFT JOIN content_performance cp ON cp.id=(
                          SELECT id FROM content_performance WHERE publication_attempt_id=pa.id {performance_mode}
                          ORDER BY created_at DESC,id DESC LIMIT 1)
                     WHERE {' AND '.join(clauses)}
                     ORDER BY pa.updated_at DESC,pa.id DESC LIMIT ?""", params).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/analytics/collect")
    def analytics_collect() -> Any:
        """Enqueue a manual analytics read through the durable event bus."""
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            abort(400, description="Analytics payload must be an object")
        provider = payload.get("provider")
        if provider is not None:
            provider = str(provider).strip().lower()
            if provider not in {"plausible", "linkedin"}:
                abort(400, description="provider must be plausible or linkedin")
        payload = {key: value for key, value in payload.items() if key in {"provider", "publication_attempt_id", "canonical_url", "external_id", "channel", "window", "mode", "fixture", "dry_run"}}
        if provider is not None:
            payload["provider"] = provider
        with connect() as connection:
            cursor = connection.execute("INSERT INTO events_queue(event_type,payload) VALUES ('ANALYTICS_COLLECT',?)", (json.dumps(payload, ensure_ascii=False),))
            connection.commit()
            event_id = int(cursor.lastrowid)
        return jsonify({"event_id": event_id, "status": "PENDING"}), 202

    @app.get("/api/analytics/publications/<int:publication_id>")
    def analytics_publication(publication_id: int) -> Any:
        mode = request.args.get("mode", "REAL").upper()
        if mode not in {"REAL", "SIMULATED", "ALL"}:
            abort(400, description="mode must be REAL, SIMULATED or ALL")
        with connect() as connection:
            publication = connection.execute("SELECT * FROM publication_attempts WHERE id=?", (publication_id,)).fetchone()
            if publication is None:
                abort(404, description="Publication attempt not found")
            snapshot_filter = "" if mode == "ALL" else "AND s.mode=?"
            snapshot_params: tuple[Any, ...] = (publication_id,) if mode == "ALL" else (publication_id, mode)
            snapshots = connection.execute(f"""SELECT s.*,GROUP_CONCAT(m.metric_name || '=' || COALESCE(CAST(m.metric_value AS TEXT),'NULL')) AS metrics
                                             FROM analytics_snapshots s LEFT JOIN analytics_metrics m ON m.snapshot_id=s.id
                                             WHERE s.publication_attempt_id=? {snapshot_filter} GROUP BY s.id ORDER BY s.collected_at DESC,s.id DESC""", snapshot_params).fetchall()
            performance_filter = "" if mode == "ALL" else "AND mode=?"
            performance_params: tuple[Any, ...] = (publication_id,) if mode == "ALL" else (publication_id, mode)
            performance = connection.execute(f"SELECT * FROM content_performance WHERE publication_attempt_id=? {performance_filter} ORDER BY created_at DESC", performance_params).fetchall()
        return jsonify({"publication": dict(publication), "snapshots": [dict(row) for row in snapshots], "performance": [dict(row) for row in performance]})

    @app.get("/api/analytics/snapshots")
    def analytics_snapshots() -> Any:
        provider = request.args.get("provider")
        channel = request.args.get("channel")
        metric = request.args.get("metric")
        mode = request.args.get("mode", "REAL").upper()
        if mode not in {"REAL", "SIMULATED", "ALL"}:
            abort(400, description="mode must be REAL, SIMULATED or ALL")
        limit = min(max(int(request.args.get("limit", "100")), 1), 500)
        clauses = ["1=1"]
        params: list[Any] = []
        if provider:
            clauses.append("s.provider=?"); params.append(provider)
        if channel:
            clauses.append("s.channel=?"); params.append(channel)
        if metric:
            clauses.append("m.metric_name=?"); params.append(metric)
        if mode != "ALL":
            clauses.append("s.mode=?"); params.append(mode)
        params.append(limit)
        with connect() as connection:
            rows = connection.execute(f"""SELECT s.*,m.metric_name,m.metric_value,m.unit
                                           FROM analytics_snapshots s LEFT JOIN analytics_metrics m ON m.snapshot_id=s.id
                                          WHERE {' AND '.join(clauses)} ORDER BY s.collected_at DESC,s.id DESC LIMIT ?""", params).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/analytics/providers")
    def analytics_providers() -> Any:
        with connect() as connection:
            rows = connection.execute("""SELECT pr.plugin_name,pr.is_active,
                    (SELECT status FROM analytics_collection_runs r ORDER BY r.started_at DESC,r.id DESC LIMIT 1) AS last_status,
                    (SELECT MAX(collected_at) FROM analytics_snapshots s WHERE s.provider IN ('plausible','simulated-website')) AS last_sync
                    FROM plugin_registry pr WHERE pr.type='analytics' ORDER BY pr.plugin_name""").fetchall()
        records = []
        for row in rows:
            status = "DISABLED" if not row["is_active"] else (row["last_status"] if row["last_status"] in {"AUTH_REQUIRED", "RATE_LIMITED", "FAILED"} else "READY")
            records.append({**dict(row), "status": status})
        return jsonify(records)

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(409)
    def api_error(error: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), error.code
        return error

    return app
