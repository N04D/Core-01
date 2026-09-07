#!/usr/bin/env python3
"""Index local NightCafe exports and generation metadata into SQLite."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.media.base import MediaAsset, MediaProvider
from core.database import connect_database
from core.paths import DATABASE_PATH


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE: Final = DATABASE_PATH
PLUGIN_NAME: Final = "NightCafe Local Media"
SOURCE_KEY: Final = "nightcafe-local"
EXTENSIONS: Final = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image",
    ".gif": "image", ".mp4": "video", ".webm": "video", ".mov": "video",
}
LOGGER: Final = logging.getLogger("media_nightcafe")


class NightCafeProvider(MediaProvider):
    source_key = SOURCE_KEY
    display_name = "NightCafe (lokale map)"
    provider_name = "nightcafe"

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def source_config(self) -> dict[str, Any]:
        return {"recursive": True, "metadata_sidecars": [".json", ".txt"]}

    @staticmethod
    def _json_metadata(path: Path) -> dict[str, Any]:
        sidecar = path.with_suffix(".json")
        if not sidecar.is_file():
            return {}
        try:
            value = json.loads(sidecar.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"sidecar": value}
        except (OSError, json.JSONDecodeError) as exc:
            return {"metadata_warning": str(exc)}

    @staticmethod
    def _embedded_metadata(path: Path) -> dict[str, Any]:
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            return {}
        try:
            from PIL import Image

            with Image.open(path) as image:
                return {str(key): str(value) for key, value in image.info.items() if isinstance(value, (str, int, float, bool))}
        except (ImportError, OSError):
            return {}

    @staticmethod
    def _prompt(path: Path, metadata: dict[str, Any]) -> str | None:
        for key in ("prompt", "Prompt", "description", "caption", "input"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        text_sidecar = path.with_suffix(".txt")
        try:
            value = text_sidecar.read_text(encoding="utf-8").strip()
            return value or None
        except OSError:
            return None

    def discover(self) -> Iterator[MediaAsset]:
        if not self.root.is_dir():
            raise NotADirectoryError(f"NightCafe directory does not exist: {self.root}")
        for path in sorted(self.root.rglob("*")):
            media_type = EXTENSIONS.get(path.suffix.lower())
            if not media_type or not path.is_file():
                continue
            stat_result = path.stat()
            relative = path.relative_to(self.root).as_posix()
            sidecar = self._json_metadata(path)
            embedded = self._embedded_metadata(path)
            metadata = {**embedded, **sidecar, "relative_path": relative}
            yield MediaAsset(
                external_id=hashlib.sha256(relative.encode("utf-8")).hexdigest(),
                filename=path.name,
                file_path=path.resolve(),
                thumbnail_path=path.resolve() if media_type == "image" else None,
                media_type=media_type,
                mime_type=mimetypes.guess_type(path.name)[0],
                file_size=stat_result.st_size,
                modified_at=datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat(),
                prompt=self._prompt(path, metadata),
                metadata=metadata,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register or index a NightCafe media directory.")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--index", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--source-dir", type=Path, default=Path(os.getenv("NIGHTCAFE_MEDIA_DIR", "~/NightCafe")))
    args = parser.parse_args()
    if not (args.register or args.index):
        parser.error("choose --register and/or --index")
    return args


def connect(path: Path) -> sqlite3.Connection:
    return connect_database(path)


def register(connection: sqlite3.Connection, provider: NightCafeProvider) -> int:
    with connection:
        connection.execute(
            """INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active)
               VALUES (?,?,?,?,1) ON CONFLICT(plugin_name) DO UPDATE SET
               type=excluded.type,executable_path=excluded.executable_path,icon=excluded.icon""",
            (PLUGIN_NAME, "media", str(Path(__file__).resolve()), "🎨"),
        )
        connection.execute(
            """INSERT INTO media_sources(source_key,display_name,provider,root_path,config,is_active)
               VALUES (?,?,?,?,?,1) ON CONFLICT(source_key) DO UPDATE SET
               display_name=excluded.display_name,provider=excluded.provider,
               root_path=excluded.root_path,config=excluded.config,is_active=1""",
            (provider.source_key, provider.display_name, provider.provider_name, str(provider.root), json.dumps(provider.source_config())),
        )
    row = connection.execute("SELECT id FROM media_sources WHERE source_key=?", (provider.source_key,)).fetchone()
    return int(row[0])


def index(connection: sqlite3.Connection, provider: NightCafeProvider, source_id: int) -> int:
    assets = list(provider.discover())
    with connection:
        connection.execute("UPDATE media_assets SET is_available=0 WHERE source_id=?", (source_id,))
        for asset in assets:
            connection.execute(
                """INSERT INTO media_assets(source_id,external_id,filename,file_path,thumbnail_path,
                       media_type,mime_type,file_size,modified_at,prompt,metadata,is_available,indexed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,1,CURRENT_TIMESTAMP)
                   ON CONFLICT(source_id,external_id) DO UPDATE SET filename=excluded.filename,
                       file_path=excluded.file_path,thumbnail_path=excluded.thumbnail_path,
                       media_type=excluded.media_type,mime_type=excluded.mime_type,
                       file_size=excluded.file_size,modified_at=excluded.modified_at,
                       prompt=excluded.prompt,metadata=excluded.metadata,is_available=1,indexed_at=CURRENT_TIMESTAMP""",
                (source_id, asset.external_id, asset.filename, str(asset.file_path),
                 str(asset.thumbnail_path) if asset.thumbnail_path else None, asset.media_type,
                 asset.mime_type, asset.file_size, asset.modified_at, asset.prompt,
                 json.dumps(asset.metadata, ensure_ascii=False)),
            )
        connection.execute("UPDATE media_sources SET last_indexed_at=CURRENT_TIMESTAMP WHERE id=?", (source_id,))
    return len(assets)


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    provider = NightCafeProvider(args.source_dir)
    try:
        with connect(args.db) as connection:
            source_id = register(connection, provider)
            LOGGER.info("Registered %s source_id=%s root=%s", PLUGIN_NAME, source_id, provider.root)
            if args.index:
                count = index(connection, provider, source_id)
                LOGGER.info("Indexed %s available assets", count)
        return 0
    except (OSError, sqlite3.Error, ValueError):
        LOGGER.exception("NightCafe indexing failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
