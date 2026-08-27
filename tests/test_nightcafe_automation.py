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


if __name__ == "__main__":
    unittest.main(verbosity=2)
