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
from plugins.media.nightcafe_automation import browser_context_options, click_with_overlay_fallback, has_external_session, managed_context_options, navigate_to_create_surface, select_cdp_page, validated_auth


class _VisibleLocator:
    def __init__(self, visible: bool = True):
        self.visible = visible
        self.clicked = False
    @property
    def first(self):
        return self
    def wait_for(self, **_kwargs):
        if not self.visible:
            raise TimeoutError("hidden")
    def click(self):
        self.clicked = True


class _OverviewPage:
    url = "https://creator.nightcafe.studio/"
    def __init__(self):
        self.create = _VisibleLocator(True)
        self.goto_calls = []
    def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        self.url = url
    def title(self):
        return "NightCafe"
    def locator(self, selector):
        if selector == "body":
            return _VisibleLocator(True)
        return _VisibleLocator(False)
    def get_by_role(self, role, **_kwargs):
        return self.create if role in {"button", "link"} else _VisibleLocator(False)
    def get_by_label(self, _label):
        return _VisibleLocator(False)
    def get_by_placeholder(self, _placeholder):
        return _VisibleLocator(False)
    def wait_for_url(self, *_args, **_kwargs):
        return None


class _InterceptedLocator:
    def __init__(self):
        self.calls = []
    def click(self, **kwargs):
        self.calls.append(kwargs)
        if not kwargs.get("force"):
            raise RuntimeError("pointer events intercepted")
        raise RuntimeError("overlay remains")
    def evaluate(self, _script):
        self.calls.append("js")


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
        self.assertEqual(managed_context_options(auth)["storage_state"], str(auth.resolve()))
        auth.chmod(0o640)
        with self.assertRaises(PermissionError):
            validated_auth(auth)
        auth.chmod(0o600)
        auth.write_text(json.dumps({"cookies": [{
            "name": "session", "value": "expired", "domain": ".nightcafe.studio", "expires": 1,
        }]}), encoding="utf-8")
        with self.assertRaises(PermissionError):
            validated_auth(auth)

    def test_overview_create_action_is_attempted_before_prompt_lookup(self) -> None:
        page = _OverviewPage()
        navigate_to_create_surface(page, 1000)
        self.assertEqual(page.goto_calls[0], "https://creator.nightcafe.studio/")
        self.assertTrue(page.create.clicked)

    def test_browser_context_uses_configured_compatibility_profile(self) -> None:
        import os
        previous = {key: os.environ.get(key) for key in ("NIGHTCAFE_USER_AGENT", "NIGHTCAFE_VIEWPORT_WIDTH")}
        try:
            os.environ["NIGHTCAFE_USER_AGENT"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
            os.environ["NIGHTCAFE_VIEWPORT_WIDTH"] = "1440"
            options = browser_context_options()
            self.assertEqual(options["viewport"]["width"], 1440)
            self.assertIn("user_agent", options)
            self.assertNotIn("args", options)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_external_session_mode_is_explicitly_detected(self) -> None:
        from argparse import Namespace
        self.assertTrue(has_external_session(Namespace(cdp_url="http://127.0.0.1:9222", user_data_dir=None)))
        self.assertTrue(has_external_session(Namespace(cdp_url=None, user_data_dir=Path(self.root / "profile"))))
        self.assertFalse(has_external_session(Namespace(cdp_url=None, user_data_dir=None)))

    def test_create_click_escalates_to_force_and_dom_click(self) -> None:
        control = _InterceptedLocator()
        click_with_overlay_fallback(control)
        self.assertEqual(control.calls, [{}, {"force": True}, "js"])

    def test_cdp_selects_existing_nightcafe_page_and_rejects_blank_only_context(self) -> None:
        class Page:
            def __init__(self, url):
                self.url = url
        class Context:
            def __init__(self, pages):
                self.pages = pages
        page = Page("https://creator.nightcafe.studio/create")
        self.assertIs(select_cdp_page(Context([Page("about:blank"), page])), page)
        with self.assertRaises(RuntimeError):
            select_cdp_page(Context([Page("about:blank")]))
        with self.assertRaises(RuntimeError):
            select_cdp_page(Context([Page("https://nightcafe.studio/explore")]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
