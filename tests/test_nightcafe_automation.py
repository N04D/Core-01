#!/usr/bin/env python3
"""Tests for the idempotent NightCafe daily stock workflow."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.database import connect_database
from core.setup_database import initialize_database
from playbooks.nightcafe_99names import NAMES, seed_names, select_daily_name
from plugins.media.nightcafe_automation import validated_auth


class NightCafeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "events.db"
        initialize_database(self.database)
        seed_names(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_catalog_has_99_unique_names_and_daily_selection_is_idempotent(self) -> None:
        self.assertEqual(len(NAMES), 99)
        self.assertEqual(len({item[1] for item in NAMES}), 99)
        first = select_daily_name(self.database, "2026-08-27")
        repeated = select_daily_name(self.database, "2026-08-27")
        second = select_daily_name(self.database, "2026-08-28")
        self.assertEqual(first["sequence"], repeated["sequence"])
        self.assertEqual(second["sequence"], first["sequence"] + 1)

    def test_simulation_registers_asset_and_source(self) -> None:
        output = self.root / "media"
        script = Path(__file__).resolve().parents[1] / "plugins/media/nightcafe_automation.py"
        completed = subprocess.run(
            [
                __import__("sys").executable, str(script), "--db", str(self.database),
                "--prompt", "symbolic light", "--name", "Ar-Rahman", "--sequence", "1",
                "--run-date", "2026-08-27", "--output-dir", str(output),
                "--simulate",
            ],
            text=True, capture_output=True, check=True,
        )
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(result["status"], "SIMULATED")
        self.assertTrue(Path(result["output_path"]).is_file())
        with connect_database(self.database) as db:
            source = db.execute("SELECT display_name FROM media_sources WHERE source_key='nightcafe-99-names'").fetchone()
            asset = db.execute("SELECT metadata FROM media_assets WHERE id=?", (result["asset_id"],)).fetchone()
        self.assertEqual(source[0], "NightCafe - 99 Names")
        self.assertEqual(json.loads(asset[0])["name"], "Ar-Rahman")
        with connect_database(self.database) as db:
            route = db.execute("SELECT target_plugin_name FROM event_routes WHERE event_type='NIGHTCAFE_GENERATE'").fetchone()
        self.assertEqual(route[0], "NightCafe Daily Stock Generator")

    def test_missing_auth_returns_blocked_auth_event_envelope(self) -> None:
        script = Path(__file__).resolve().parents[1] / "plugins/media/nightcafe_automation.py"
        with connect_database(self.database) as db:
            db.execute("INSERT INTO events_queue(event_type,payload) VALUES ('NIGHTCAFE_GENERATE',?)", ('{"prompt":"x","name":"Ar-Rahman","sequence":1}',))
            event_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.commit()
        completed = subprocess.run(
            [__import__("sys").executable, str(script), "--db", str(self.database), "--event_id", str(event_id), "--auth", str(self.root / "missing.json")],
            text=True, capture_output=True, check=True,
        )
        self.assertIn('"outcome":"BLOCKED_AUTH"', completed.stdout)
        self.assertIn("AUTH_REQUIRED", completed.stdout)

    def test_worker_commits_blocked_auth_status(self) -> None:
        script = Path(__file__).resolve().parents[1] / "plugins/media/nightcafe_automation.py"
        worker = Path(__file__).resolve().parents[1] / "daemon/worker.py"
        with connect_database(self.database) as db:
            db.execute("INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active) VALUES (?,?,?,?,1)",
                       ("NightCafe Daily Stock Generator", "media", str(script), "🌙"))
            db.execute("INSERT INTO event_routes(event_type,target_plugin_name) VALUES ('NIGHTCAFE_GENERATE','NightCafe Daily Stock Generator')")
            db.execute("INSERT INTO events_queue(event_type,payload) VALUES ('NIGHTCAFE_GENERATE',?)", ('{"prompt":"x","name":"Ar-Rahman","sequence":1}',))
            db.commit()
        env = __import__("os").environ.copy()
        env["NIGHTCAFE_AUTH_FILE"] = str(self.root / "missing.json")
        subprocess.run([__import__("sys").executable, str(worker), "--database", str(self.database), "--once"], check=True, text=True, capture_output=True, env=env)
        with connect_database(self.database) as db:
            status = db.execute("SELECT status FROM events_queue WHERE event_type='NIGHTCAFE_GENERATE'").fetchone()[0]
        self.assertEqual(status, "BLOCKED_AUTH")

    def test_manual_playwright_cookie_state_without_origins_is_accepted(self) -> None:
        auth = self.root / "nightcafe_auth.json"
        auth.write_text(json.dumps({"cookies": [{
            "name": "session", "value": "redacted-test-value",
            "domain": ".nightcafe.studio", "path": "/", "httpOnly": True,
            "secure": True, "sameSite": "Lax", "expires": -1,
        }]}), encoding="utf-8")
        auth.chmod(0o600)
        self.assertEqual(validated_auth(auth), auth.resolve())
        auth.chmod(0o640)
        with self.assertRaises(PermissionError):
            validated_auth(auth)
        auth.chmod(0o600)
        auth.write_text(json.dumps({"cookies": [{
            "name": "session", "value": "expired", "domain": ".nightcafe.studio", "expires": 1,
        }]}), encoding="utf-8")
        with self.assertRaises(PermissionError):
            validated_auth(auth)


if __name__ == "__main__":
    unittest.main(verbosity=2)
