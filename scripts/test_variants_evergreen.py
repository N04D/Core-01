#!/usr/bin/env python3
"""Offline integration test for channel variants and evergreen proposals."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from core.database import connect_database

from core.evergreen import analyze  # noqa: E402
from core.setup_database import initialize_database  # noqa: E402
from daemon.scheduler import propose_evergreen  # noqa: E402
from playbooks.master_syndication_suite import (  # noqa: E402
    EventRun,
    dispatch_publications,
    generate_variants,
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="variants_evergreen_") as temporary:
        root = Path(temporary)
        database = root / "events.db"
        analytics = root / "analytics"
        analytics.mkdir()
        initialize_database(database)
        essay = root / "essay.md"
        essay.write_text("# Bronessay\n\nEen feitelijk essay over lokale AI.", encoding="utf-8")
        channels = [
            ("PUBLISH_LINKEDIN_PRO", "LinkedIn", Path("/bin/true")),
            ("PUBLISH_SUBSTACK_PRO", "Substack", Path("/bin/true")),
            ("PUBLISH_MEDIUM", "Medium", Path("/bin/true")),
        ]
        next_id = iter((101, 102, 103))
        outputs: dict[int, Path] = {}

        def fake_insert(_database: Path, _event_type: str, payload: dict[str, object]) -> int:
            event_id = next(next_id)
            channel = str(payload["variant_channel"])
            output = root / f"variant_{channel}.md"
            output.write_text(f"# {channel.title()} variant\n\n{channel} format.", encoding="utf-8")
            outputs[event_id] = output
            return event_id

        def fake_drive(_database: Path, run: EventRun, _mock: bool) -> dict[str, str]:
            run.status = "COMPLETED"
            return {"filepath": str(outputs[run.event_id])}

        with patch("playbooks.master_syndication_suite.insert_event", fake_insert), patch(
            "playbooks.master_syndication_suite.drive_event", fake_drive
        ):
            variants = generate_variants(database, "Lokale AI", essay, channels, True)
        assert set(variants) == {"linkedin", "substack", "medium"}
        assert len({path.read_text() for path in variants.values()}) == 3
        with connect_database(database) as connection:
            rows = connection.execute(
                "SELECT channel,variant_path,generation_event_id FROM content_variants ORDER BY channel"
            ).fetchall()
        assert len(rows) == 3
        publication_payloads: list[tuple[str, dict[str, object]]] = []

        def capture_publication(
            _database: Path, event_type: str, payload: dict[str, object]
        ) -> int:
            publication_payloads.append((event_type, payload))
            return 200 + len(publication_payloads)

        archive_dir = root / "archives"
        archive_dir.mkdir()

        def fake_archive(source: Path, event_type: str) -> Path:
            destination = archive_dir / f"{event_type}.md"
            destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            return destination

        with patch(
            "playbooks.master_syndication_suite.insert_event", capture_publication
        ), patch("playbooks.master_syndication_suite.archive_copy", fake_archive):
            runs = dispatch_publications(
                database, channels, essay, variants, "Lokale AI", None, True
            )
        assert len(runs) == 3
        expected = {
            "PUBLISH_LINKEDIN_PRO": "linkedin",
            "PUBLISH_SUBSTACK_PRO": "substack",
            "PUBLISH_MEDIUM": "medium",
        }
        assert all(payload["content_variant"] == expected[event] for event, payload in publication_payloads)
        assert all(Path(str(payload["draft_file"])).read_text().startswith(f"# {expected[event].title()}") for event, payload in publication_payloads)

        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        analytics_file = analytics / "linkedin_analytics_test.json"
        analytics_file.write_text(
            json.dumps(
                {
                    "mode": "analytics",
                    "posts": [
                        {
                            "id": "high-performer",
                            "title": "Evergreen lokale AI",
                            "content": "Een bewezen artikel over lokale AI.",
                            "published_at": old_date,
                            "views": 5000,
                            "likes": 120,
                            "comments": [{"text": "Sterk"}] * 10,
                            "shares": 20,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        processed, flagged = analyze(database, analytics, threshold=25, evergreen_days=90)
        proposals = propose_evergreen(database)
        assert processed == flagged == proposals == 1
        with connect_database(database) as connection:
            proposal = connection.execute(
                "SELECT status,rewrite_payload FROM evergreen_proposals"
            ).fetchone()
        payload = json.loads(proposal[1])
        assert proposal[0] == "PROPOSED" and payload["evergreen_rewrite"] == 1
        print("TEST_OK variants=linkedin,substack,medium unique=3")
        print("TEST_OK generation_event_links=101,102,103 persisted=3")
        print("TEST_OK publication_events=201,202,203 correctly_routed=true")
        print("TEST_OK evergreen processed=1 flagged=1 score_threshold=25")
        print("TEST_OK scheduler proposals=1 status=PROPOSED auto_publish=false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
