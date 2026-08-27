"""Integration tests for the visual planning API."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import create_app


class AgendaApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "agenda.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE scheduled_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                scheduled_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING'
            );
            """
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.start = now - timedelta(days=1)
        self.end = now + timedelta(days=1)
        connection.executemany(
            "INSERT INTO scheduled_events(event_type,payload,scheduled_time,status) VALUES (?,?,?,?)",
            [
                ("PUBLISH_LINKEDIN_PRO", json.dumps({"content_type": "text"}), now.isoformat(), "PENDING"),
                ("PUBLISH_MEDIUM", "{}", (now + timedelta(days=5)).isoformat(), "PENDING"),
            ],
        )
        connection.commit()
        connection.close()
        app = create_app(self.database)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scheduled_events_can_be_limited_to_calendar_range(self) -> None:
        response = self.client.get(
            "/api/scheduled-events",
            query_string={"start": self.start.isoformat(), "end": self.end.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        records = response.get_json()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event_type"], "PUBLISH_LINKEDIN_PRO")
        self.assertEqual(records[0]["payload"]["content_type"], "text")

    def test_calendar_range_requires_timezone(self) -> None:
        response = self.client.get("/api/scheduled-events?start=2026-08-27T10:00:00")
        self.assertEqual(response.status_code, 400)

    def test_agenda_page_contains_day_week_and_schedule_controls(self) -> None:
        response = self.client.get("/")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        for marker in ("calendar-day", "calendar-week", "quickScheduleDraft", "schedule-list"):
            self.assertIn(marker, page)

    def test_dashboard_contains_reliable_accessible_feedback_primitives(self) -> None:
        page = self.client.get("/").get_data(as_text=True)
        for marker in (
            'id="toast-stack"',
            'id="live-region"',
            'aria-live="polite"',
            "async function runAction",
            "Promise.allSettled",
            "Opnieuw proberen",
        ):
            self.assertIn(marker, page)
        self.assertNotIn('id="flash"', page)

    def test_dashboard_contains_responsive_navigation_shell(self) -> None:
        page = self.client.get("/").get_data(as_text=True)
        for marker in (
            'class="app-shell flex overflow-hidden"',
            'id="sidebar-backdrop"',
            'id="open-sidebar"',
            'id="close-sidebar"',
            'aria-controls="sidebar"',
            "function openMobileSidebar",
            "function closeMobileSidebar",
            "event.key==='Escape'",
        ):
            self.assertIn(marker, page)


if __name__ == "__main__":
    unittest.main()
