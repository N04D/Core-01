from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from core.md_subject_parser import load_subject_document
from core.setup_database import initialize_database
from core.database import connect_database
from playbooks.daily_99names_runner import next_open_subject, validate_contract, finalize_download


class Daily99NamesRunnerTests(unittest.TestCase):
    def test_contract_and_first_open_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "events.db"
            initialize_database(db)
            plugin_path = Path(__file__).parents[1] / "plugins/media/nightcafe_automation.py"
            overlay_path = Path(__file__).parents[1] / "plugins/media/image_overlay.py"
            with connect_database(db) as connection:
                connection.executemany(
                    "INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active) VALUES (?,?,?,?,1)",
                    [("NightCafe Daily Stock Generator", "media", str(plugin_path), "🌙"),
                     ("Image Overlay Processor", "media", str(overlay_path), "🖼️")],
                )
                connection.executemany(
                    "INSERT INTO event_routes(event_type,target_plugin_name) VALUES (?,?)",
                    [("NIGHTCAFE_GENERATE", "NightCafe Daily Stock Generator"),
                     ("IMAGE_OVERLAY", "Image Overlay Processor")],
                )
                connection.commit()
            contract = root / "contract.json"
            contract.write_text(json.dumps({
                "required_plugins": [
                    {"name": "NightCafe Daily Stock Generator", "route": "NIGHTCAFE_GENERATE"},
                    {"name": "Image Overlay Processor", "route": "IMAGE_OVERLAY"}],
                "dependencies": {"sqlite_tables": ["nightcafe_names", "nightcafe_daily_runs"]},
            }), encoding="utf-8")
            validate_contract(contract, db)
            source = root / "subjects.md"
            source.write_text("---\nstyle: minimal\nrules: [\"no people\"]\n---\n\n1. First | One\n2. Second | Two\n", encoding="utf-8")
            document = load_subject_document(source)
            with connect_database(db) as connection:
                connection.executemany("INSERT INTO nightcafe_names(sequence,arabic_name,transliteration,meaning) VALUES (?,?,?,?)", [(1, "First", "First", "One"), (2, "Second", "Second", "Two")])
                connection.execute("INSERT INTO nightcafe_daily_runs(run_date,name_sequence,status,output_path) VALUES ('2020-01-01',1,'COMPLETED','/missing.jpg')")
                connection.commit()
            self.assertEqual(next_open_subject(db, document).sequence, 1)

    def test_invalid_download_stops_before_overlay(self) -> None:
        class Args:
            date = "2099-01-03"; sequence = 1; overlay_font_size = 64; db = Path("/tmp/unused.db")
        class Subject: sequence = 1; title = "First"; context = "One"
        with patch("playbooks.daily_99names_runner.validate_image_file", side_effect=RuntimeError("placeholder")):
            with self.assertRaisesRegex(RuntimeError, "placeholder"):
                finalize_download(Path("/tmp/placeholder.png"), Subject(), Args())


if __name__ == "__main__":
    unittest.main()
