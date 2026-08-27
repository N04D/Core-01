#!/usr/bin/env python3
"""Tests for selective Drive access, fail-closed auth and Media Store writes."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from core.database import connect_database
from core.setup_database import initialize_database
from plugins.io.gdrive_sync import (
    GDriveAuthError,
    classify_name,
    download_file,
    list_selected_files,
    register_asset,
    validate_auth,
    validate_target_folder,
)


class FakeResponse:
    def __init__(self, payload: bytes | dict, status: int = 200) -> None:
        self.payload, self.status_code = payload, status
        self.headers = {"content-type": "image/png"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self.payload

    def iter_content(self, chunk_size: int = 1):
        yield self.payload

    def close(self) -> None:
        pass

    @property
    def ok(self) -> bool:
        return self.status_code < 400


class FakeSession:
    def __init__(self, files: list[dict] | None = None):
        self.files = files or []
        self.queries: list[dict] = []

    def get(self, url, **kwargs):
        self.queries.append(kwargs.get("params", {}))
        if url.endswith("/files"):
            return FakeResponse({"files": self.files})
        return FakeResponse(b"\x89PNG\r\n\x1a\nvalid")


class GDriveBridgeTests(unittest.TestCase):
    def test_folder_filter_is_explicit_and_non_recursive(self) -> None:
        session = FakeSession([
            {"id": "1", "name": "ok.png", "parents": ["folder"]},
            {"id": "2", "name": "nested.png", "parents": ["other"]},
        ])
        selected = list_selected_files("token", "folder", session=session)
        self.assertEqual([item["id"] for item in selected], ["1"])
        self.assertIn("'folder' in parents", session.queries[0]["q"])
        self.assertNotIn("'other'", session.queries[0]["q"])
        with self.assertRaises(ValueError):
            validate_target_folder(None)
        self.assertEqual(classify_name("photo.PNG"), "image")
        self.assertIsNone(classify_name("secret.pdf"))

    def test_auth_requires_0600_and_explicit_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "gdrive_auth.json"
            auth.write_text(json.dumps({"access_token": "x"}), encoding="utf-8")
            auth.chmod(0o644)
            with self.assertRaises(GDriveAuthError):
                validate_auth(auth)
            auth.chmod(0o600)
            self.assertEqual(validate_auth(auth), "x")
            auth.write_text("{}", encoding="utf-8")
            with self.assertRaises(GDriveAuthError):
                validate_auth(auth)

    def test_download_magic_and_media_store_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "events.db"
            initialize_database(database)
            session = FakeSession()
            item = {"id": "drive-1", "name": "photo.png", "parents": ["folder"], "modifiedTime": "2026-01-01T00:00:00Z", "description": "prompt"}
            path = download_file("token", item, root / "staging", session=session)
            asset_id = register_asset(database, "folder", item, path)
            self.assertTrue(path.is_file())
            with connect_database(database) as db:
                row = db.execute("SELECT ms.display_name,ma.external_id,ma.media_type FROM media_assets ma JOIN media_sources ms ON ms.id=ma.source_id WHERE ma.id=?", (asset_id,)).fetchone()
            self.assertEqual(tuple(row), ("Google Drive", "drive-1", "image"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
