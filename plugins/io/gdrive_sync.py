#!/usr/bin/env python3
"""Selective Google Drive folder bridge for the local Media Store.

Only files whose direct parent is the configured folder ID are queried.  No
Drive-wide listing or recursive traversal is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.database import connect_database
from core.paths import DATABASE_PATH
from core.event_protocol import emit_result

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DB: Final = DATABASE_PATH
DEFAULT_AUTH: Final = PROJECT_ROOT / "config" / "gdrive_auth.json"
DEFAULT_STAGING: Final = PROJECT_ROOT / "vault" / "media" / "gdrive"
MAX_DOWNLOAD_BYTES: Final = 500 * 1024 * 1024
PLUGIN_NAME: Final = "Google Drive Selective Folder Bridge"
SOURCE_KEY: Final = "google-drive"
EVENT_TYPE: Final = "GDRIVE_SYNC"
LOGGER = logging.getLogger("gdrive_sync")
API_ROOT: Final = "https://www.googleapis.com/drive/v3"
ALLOWED: Final[dict[str, tuple[str, ...]]] = {
    "image": (".jpg", ".jpeg", ".png", ".webp", ".gif"),
    "video": (".mp4", ".webm", ".mov"),
}
MAGIC: Final[dict[str, tuple[bytes, ...]]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".webp": (b"RIFF",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".mp4": (b"\x00\x00\x00", b"\x00\x00\x00\x18ftyp"),
    ".mov": (b"\x00\x00\x00",),
    ".webm": (b"\x1a\x45\xdf\xa3",),
}


class GDriveAuthError(PermissionError):
    """Raised when the Drive credential is absent, unsafe, or expired."""


class GDriveSyncError(RuntimeError):
    """Raised when a selected Drive file cannot be safely synchronized."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH)
    parser.add_argument("--target-folder-id", default=os.getenv("GDRIVE_TARGET_FOLDER_ID"))
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING)
    parser.add_argument("--once", action="store_true", help="Run one synchronization pass and exit.")
    parser.add_argument("--dry-run", action="store_true", help="List selected files without downloading.")
    parser.add_argument("--page-size", type=int, default=100, choices=range(1, 1001))
    return parser.parse_args()


def validate_auth(path: Path) -> str:
    """Read a short-lived OAuth token from a private 0600 JSON file."""
    auth = path.expanduser().resolve()
    if not auth.is_file():
        raise GDriveAuthError(f"Google Drive authentication required: missing {auth}")
    mode = stat.S_IMODE(auth.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise GDriveAuthError(f"Google Drive credentials must have mode 0600 (found {mode:o})")
    try:
        data = json.loads(auth.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GDriveAuthError(f"Google Drive credentials are invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GDriveAuthError("Google Drive credentials must be a JSON object")
    token = data.get("access_token") or data.get("token")
    if not isinstance(token, str) or not token.strip():
        raise GDriveAuthError("Google Drive credentials contain no access_token")
    expires_at = data.get("expires_at", data.get("expiry"))
    if expires_at is not None:
        try:
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
            if float(expires_at) <= datetime.now(timezone.utc).timestamp():
                raise GDriveAuthError("Google Drive OAuth access token has expired")
        except (TypeError, ValueError) as exc:
            raise GDriveAuthError(f"Google Drive credential expiry is invalid: {exc}") from exc
    return token.strip()


def validate_target_folder(folder_id: str | None) -> str:
    if not isinstance(folder_id, str) or not folder_id.strip():
        raise ValueError("GDRIVE_TARGET_FOLDER_ID or --target-folder-id is required; refusing broad Drive access")
    normalized = folder_id.strip()
    if len(normalized) > 256 or any(char.isspace() for char in normalized):
        raise ValueError("target folder ID is invalid")
    return normalized


def list_selected_files(token: str, folder_id: str, page_size: int = 100, session: requests.Session | None = None) -> list[dict[str, Any]]:
    """List direct children only; pagination remains constrained to one folder."""
    client = session or requests.Session()
    files: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "spaces": "drive",
            "pageSize": page_size,
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,parents,description,webContentLink)",
            "orderBy": "modifiedTime desc",
        }
        if page_token:
            params["pageToken"] = page_token
        response = client.get(f"{API_ROOT}/files", params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        response.raise_for_status()
        body = response.json()
        batch = body.get("files", [])
        if not isinstance(batch, list):
            raise GDriveSyncError("Drive API returned an invalid files collection")
        for item in batch:
            if isinstance(item, dict) and folder_id in (item.get("parents") or []):
                files.append(item)
        page_token = body.get("nextPageToken")
        if not page_token:
            return files


def classify_name(name: str) -> str | None:
    suffix = Path(name).suffix.casefold()
    for media_type, extensions in ALLOWED.items():
        if suffix in extensions:
            return media_type
    return None


def verify_magic(path: Path) -> None:
    suffix = path.suffix.casefold()
    signatures = MAGIC.get(suffix)
    if not signatures:
        raise GDriveSyncError(f"unsupported file extension: {suffix}")
    with path.open("rb") as stream:
        header = stream.read(16)
    if not any(header.startswith(signature) for signature in signatures):
        raise GDriveSyncError(f"magic-byte verification failed for {path.name}")


def safe_filename(name: str) -> str:
    cleaned = Path(name).name.replace("\x00", "")
    if not cleaned or cleaned in {".", ".."}:
        raise GDriveSyncError("Drive returned an unsafe filename")
    return cleaned[:180]


def download_file(token: str, item: dict[str, Any], staging: Path, session: requests.Session | None = None) -> Path:
    name = safe_filename(str(item.get("name", "")))
    if classify_name(name) is None:
        raise GDriveSyncError(f"unsupported media extension: {name}")
    file_id = item.get("id")
    if not isinstance(file_id, str) or not file_id:
        raise GDriveSyncError("Drive item has no file ID")
    staging.mkdir(parents=True, exist_ok=True)
    client = session or requests.Session()
    destination = staging / f"{file_id}_{name}"
    with tempfile.NamedTemporaryFile(prefix=".gdrive-", dir=staging, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        response = client.get(f"{API_ROOT}/files/{file_id}", params={"alt": "media"}, headers={"Authorization": f"Bearer {token}"}, stream=True, timeout=120)
        try:
            response.raise_for_status()
            declared_size = item.get("size")
            if declared_size is not None and int(declared_size) > MAX_DOWNLOAD_BYTES:
                raise GDriveSyncError(f"Drive file exceeds maximum size of {MAX_DOWNLOAD_BYTES} bytes: {name}")
            downloaded = 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_BYTES:
                        raise GDriveSyncError(f"Drive file exceeds maximum size of {MAX_DOWNLOAD_BYTES} bytes: {name}")
                    temporary.write(chunk)
        finally:
            response.close()
    try:
        if temporary_path.stat().st_size == 0:
            raise GDriveSyncError(f"Drive file is empty: {name}")
        # Rename only after the complete download; magic validation happens on the final extension.
        temporary_path.rename(destination)
        verify_magic(destination)
        return destination
    except Exception:
        temporary_path.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise


def register_asset(db_path: Path, folder_id: str, item: dict[str, Any], path: Path) -> int:
    media_type = classify_name(path.name) or "image"
    mime_type = mimetypes.guess_type(path.name)[0]
    external_id = str(item["id"])
    metadata = {"drive_file_id": external_id, "target_folder_id": folder_id, "drive_name": item.get("name"), "description": item.get("description")}
    modified_at = item.get("modifiedTime")
    with connect_database(db_path) as db:
        db.execute(
            """INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active)
               VALUES (?,?,?,?,1) ON CONFLICT(plugin_name) DO UPDATE SET executable_path=excluded.executable_path,is_active=1""",
            (PLUGIN_NAME, "io", str(Path(__file__).resolve()), "☁️"),
        )
        db.execute(
            """INSERT INTO event_routes(event_type,target_plugin_name) VALUES (?,?)
               ON CONFLICT(event_type) DO UPDATE SET target_plugin_name=excluded.target_plugin_name""",
            (EVENT_TYPE, PLUGIN_NAME),
        )
        db.execute(
            """INSERT INTO media_sources(source_key,display_name,provider,root_path,config,is_active,last_indexed_at)
               VALUES (?,?,?,?,?,1,CURRENT_TIMESTAMP) ON CONFLICT(source_key) DO UPDATE SET root_path=excluded.root_path,config=excluded.config,is_active=1,last_indexed_at=CURRENT_TIMESTAMP""",
            (SOURCE_KEY, "Google Drive", "google_drive", str(path.parent), json.dumps({"target_folder_id": folder_id}, ensure_ascii=False)),
        )
        source_id = int(db.execute("SELECT id FROM media_sources WHERE source_key=?", (SOURCE_KEY,)).fetchone()[0])
        db.execute(
            """INSERT INTO media_assets(source_id,external_id,filename,file_path,thumbnail_path,media_type,mime_type,file_size,modified_at,prompt,metadata,is_available,indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,CURRENT_TIMESTAMP) ON CONFLICT(source_id,external_id) DO UPDATE SET filename=excluded.filename,file_path=excluded.file_path,thumbnail_path=excluded.thumbnail_path,media_type=excluded.media_type,mime_type=excluded.mime_type,file_size=excluded.file_size,modified_at=excluded.modified_at,metadata=excluded.metadata,is_available=1,indexed_at=CURRENT_TIMESTAMP""",
            (source_id, external_id, str(item.get("name", path.name)), str(path), str(path) if media_type == "image" else None, media_type, mime_type, path.stat().st_size, modified_at, item.get("description"), json.dumps(metadata, ensure_ascii=False)),
        )
        asset_id = int(db.execute("SELECT id FROM media_assets WHERE source_id=? AND external_id=?", (source_id, external_id)).fetchone()[0])
        db.commit()
        return asset_id


def sync_once(db_path: Path, auth: Path, folder_id: str | None, staging: Path, dry_run: bool = False, session: requests.Session | None = None) -> list[dict[str, Any]]:
    selected_folder = validate_target_folder(folder_id)
    token = validate_auth(auth)
    items = list_selected_files(token, selected_folder, session=session)
    results: list[dict[str, Any]] = []
    for item in items:
        name = safe_filename(str(item.get("name", "")))
        if classify_name(name) is None:
            LOGGER.info("Skipping non-media file in selected folder: %s", name)
            continue
        if dry_run:
            results.append({"id": item.get("id"), "name": name, "status": "DRY_RUN"})
            continue
        path = download_file(token, item, staging, session=session)
        asset_id = register_asset(db_path, selected_folder, item, path)
        results.append({"id": item.get("id"), "name": name, "status": "COMPLETED", "asset_id": asset_id, "path": str(path)})
    return results


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    database = args.db.expanduser().resolve()
    if args.register:
        with connect_database(database) as db:
            db.execute("INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active) VALUES (?,?,?,?,1) ON CONFLICT(plugin_name) DO UPDATE SET executable_path=excluded.executable_path,is_active=1", (PLUGIN_NAME, "io", str(Path(__file__).resolve()), "☁️"))
            db.execute("INSERT INTO event_routes(event_type,target_plugin_name) VALUES (?,?) ON CONFLICT(event_type) DO UPDATE SET target_plugin_name=excluded.target_plugin_name", (EVENT_TYPE, PLUGIN_NAME))
            db.commit()
        LOGGER.info("Registered %s route=%s", PLUGIN_NAME, EVENT_TYPE)
        if args.event_id is None and args.target_folder_id is None:
            return 0
    try:
        results = sync_once(database, args.auth, args.target_folder_id, args.staging_dir.expanduser().resolve(), args.dry_run)
        payload = {"target_folder_id": validate_target_folder(args.target_folder_id), "files": results}
        if args.event_id is not None:
            emit_result("COMPLETED", result=payload, payload_patch={"gdrive": payload})
        else:
            print(json.dumps({"status": "COMPLETED", **payload}, ensure_ascii=False))
        return 0
    except GDriveAuthError as exc:
        LOGGER.error("BLOCKED_AUTH: %s", exc)
        if args.event_id is not None:
            emit_result("BLOCKED_AUTH", result={"status": "AUTH_REQUIRED"}, error=str(exc))
            return 0
        print(json.dumps({"status": "BLOCKED_AUTH", "error": str(exc)}))
        return 1
    except Exception as exc:
        LOGGER.exception("Google Drive sync failed")
        if args.event_id is not None:
            emit_result("FAILED", error=f"{type(exc).__name__}: {exc}")
            return 0
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
