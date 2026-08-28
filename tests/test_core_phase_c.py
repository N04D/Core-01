#!/usr/bin/env python3
"""Focused tests for RAG offloading, browser resilience, and DLQ recovery."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.browser_robustness import (
    capture_page_trace,
    find_with_accessible_fallbacks,
    find_prompt_input,
    sanitized_url,
    verify_submission,
)
from core.database import connect_database
from core.rag_index import search, tokens
from core.setup_database import initialize_database
from daemon.rag_indexer import run_refresh
from dashboard.app import create_app


class RagIndexerTests(unittest.TestCase):
    def test_incremental_index_and_strict_token_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / "vault"
            vault.mkdir()
            note = vault / "note.md"
            note.write_text("# Context\n\n" + " ".join(f"token{i}" for i in range(180)), encoding="utf-8")
            database = root / "events.db"
            initialize_database(database)
            indexed, skipped, chunks = run_refresh(database, [vault], 40, 5)
            self.assertEqual((indexed, skipped), (1, 0))
            self.assertGreater(chunks, 1)
            with connect_database(database) as connection:
                counts = [row[0] for row in connection.execute("SELECT token_count FROM rag_chunks")]
                first_hash = connection.execute("SELECT content_hash FROM rag_documents").fetchone()[0]
            self.assertLessEqual(max(counts), 40)
            self.assertEqual(run_refresh(database, [vault], 40, 5)[0:2], (0, 1))
            note.write_text(note.read_text() + "\n\nnieuwe wijziging", encoding="utf-8")
            self.assertEqual(run_refresh(database, [vault], 40, 5)[0], 1)
            with connect_database(database) as connection:
                self.assertNotEqual(connection.execute("SELECT content_hash FROM rag_documents").fetchone()[0], first_hash)
            matches = search(database, "nieuwe wijziging", limit=10, minimum_score=-1, maximum_context_tokens=25)
            self.assertLessEqual(sum(len(tokens(item.content)) for item in matches), 25)


class FakeLocator:
    def __init__(self, visible: bool = True) -> None:
        self.visible = visible

    @property
    def first(self) -> "FakeLocator":
        return self

    def wait_for(self, **_kwargs: object) -> None:
        if not self.visible:
            raise TimeoutError("not visible")


class FakePage:
    def __init__(self, role_visible: bool, css_visible: bool = True) -> None:
        self.role_visible = role_visible
        self.css_visible = css_visible
        self.calls: list[str] = []
        self.url = "https://example.test/editor"

    def get_by_role(self, role: str, **_kwargs: object) -> FakeLocator:
        self.calls.append(f"role:{role}")
        return FakeLocator(self.role_visible)

    def get_by_label(self, _label: object) -> FakeLocator:
        self.calls.append("label")
        return FakeLocator(False)

    def get_by_placeholder(self, _placeholder: object) -> FakeLocator:
        self.calls.append("placeholder")
        return FakeLocator(False)

    def content(self) -> str:
        return "<textarea placeholder='Enter your prompt'></textarea>"

    def screenshot(self, *, path: str, **_kwargs: object) -> None:
        Path(path).write_bytes(b"PNG")

    def locator(self, selector: str) -> FakeLocator:
        self.calls.append(f"css:{selector}")
        return FakeLocator(self.css_visible)

    def wait_for_url(self, _predicate: object, **_kwargs: object) -> None:
        raise TimeoutError("URL unchanged")


class BrowserRobustnessTests(unittest.TestCase):
    def test_accessible_role_precedes_css_and_css_remains_fallback(self) -> None:
        page = FakePage(role_visible=True)
        find_with_accessible_fallbacks(page, ["button:has-text('Publish')", ".legacy"])
        self.assertEqual(page.calls[0], "role:button")
        fallback = FakePage(role_visible=False)
        find_with_accessible_fallbacks(fallback, ["button:has-text('Publish')"])
        self.assertEqual(fallback.calls[0], "role:button")
        self.assertTrue(fallback.calls[-1].startswith("css:"))

    def test_submit_requires_url_or_confirmation_indicator(self) -> None:
        page = FakePage(role_visible=False, css_visible=True)
        confirmed = verify_submission(page, page.url, indicators=("[role=status]",), timeout=100)
        self.assertEqual(confirmed, page.url)
        failing = FakePage(role_visible=False, css_visible=False)
        with self.assertRaisesRegex(RuntimeError, "explicitly confirmed"):
            verify_submission(failing, failing.url, indicators=("[role=status]",), timeout=100)

    def test_prompt_fallbacks_reach_placeholder_and_broad_css(self) -> None:
        page = FakePage(role_visible=False, css_visible=True)
        control = find_prompt_input(page, timeout=1000)
        self.assertIsInstance(control, FakeLocator)
        self.assertTrue(any("placeholder" in call for call in page.calls))
        self.assertTrue(any(call.startswith("css:") for call in page.calls))

    def test_page_trace_writes_protected_screenshot_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifacts = capture_page_trace(FakePage(True), Path(directory), "nightcafe", "selector")
            for key in ("screenshot", "html"):
                path = Path(artifacts[key])
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("Enter your prompt", Path(artifacts["html"]).read_text())

    def test_debug_urls_are_sanitized(self) -> None:
        self.assertEqual(
            sanitized_url("https://user:secret@example.com/post?id=secret#comments"),
            "https://example.com/post",
        )


class DlqApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "events.db"
        initialize_database(self.database)
        with connect_database(self.database) as connection:
            connection.execute(
                """INSERT INTO dead_letter_queue(
                       id,event_type,payload,status,retry_count,error_log,reason_for_death
                   ) VALUES (41,'PUBLISH_TEST',?,'FAILED',3,'network timeout','Retries exhausted')""",
                (json.dumps({"content": "recover me"}),),
            )
        app = create_app(self.database)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_redrive_preserves_source_and_links_new_event(self) -> None:
        response = self.client.post(
            "/api/dead-letters/41/redrive",
            json={"reason": "Network fixed", "requested_by": "test-suite"},
        )
        self.assertEqual(response.status_code, 201)
        new_id = response.get_json()["new_event_id"]
        with connect_database(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM dead_letter_queue WHERE id=41").fetchone()[0], 1)
            event = connection.execute("SELECT status,payload FROM events_queue WHERE id=?", (new_id,)).fetchone()
            self.assertEqual(event["status"], "PENDING")
            self.assertEqual(json.loads(event["payload"])["redrive_history"][-1]["dead_letter_id"], 41)
            audit = connection.execute("SELECT * FROM dead_letter_redrives WHERE new_event_id=?", (new_id,)).fetchone()
            self.assertEqual(audit["dead_letter_id"], 41)

    def test_dlq_filter_and_metrics(self) -> None:
        listing = self.client.get("/api/dead-letters?event_type=PUBLISH_TEST&q=Retries")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.get_json()), 1)
        metrics = self.client.get("/api/metrics")
        self.assertEqual(metrics.status_code, 200)
        body = metrics.get_json()
        self.assertEqual(body["dead_letters"], 1)
        self.assertIn("leases", body)
        self.assertIn("sessions", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
