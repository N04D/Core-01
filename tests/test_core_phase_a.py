#!/usr/bin/env python3
"""Unit and integration coverage for Phase A event safety semantics."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.event_protocol import (
    PluginResult,
    begin_submission,
    emit_result,
    parse_result,
    update_publication,
)
from core.setup_database import initialize_database
from daemon.worker import (
    claim_event,
    connect,
    finalize_event,
    reap_expired_leases,
    renew_lease,
    process_one,
)
from plugins.inputs.telegram_in import allowed_chat


class DatabaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "events.db"
        initialize_database(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def enqueue(self, payload: dict[str, object] | None = None) -> int:
        with connect(self.database) as connection:
            cursor = connection.execute(
                "INSERT INTO events_queue(event_type,payload) VALUES ('TEST',?)",
                (json.dumps(payload or {"content": "hello"}),),
            )
            connection.commit()
            return int(cursor.lastrowid)


class LeaseTests(DatabaseCase):
    def test_claim_has_unique_owner_and_heartbeat(self) -> None:
        event_id = self.enqueue()
        with connect(self.database) as connection:
            event = claim_event(connection, "worker-a", 60)
            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(event.id, event_id)
            self.assertTrue(event.claim_token)
            self.assertIsNone(claim_event(connection, "worker-b", 60))
        self.assertTrue(renew_lease(self.database, event, 120))

    def test_reaper_recovers_only_expired_processing_lease(self) -> None:
        event_id = self.enqueue()
        with connect(self.database) as connection:
            event = claim_event(connection, "dead-worker", 60)
            assert event is not None
            connection.execute(
                "UPDATE events_queue SET lease_until='2000-01-01 00:00:00' WHERE id=?",
                (event_id,),
            )
            connection.commit()
            self.assertEqual(reap_expired_leases(connection), 1)
            row = connection.execute(
                "SELECT status,claimed_by,lease_until,claim_token FROM events_queue WHERE id=?",
                (event_id,),
            ).fetchone()
            self.assertEqual(row["status"], "PENDING")
            self.assertTrue(all(row[key] is None for key in ("claimed_by", "lease_until", "claim_token")))
        self.assertFalse(renew_lease(self.database, event, 60))

    def test_blocked_auth_is_terminal_and_clears_lease(self) -> None:
        event_id = self.enqueue()
        with connect(self.database) as connection:
            event = claim_event(connection, "worker-a", 60)
            assert event is not None
            finalize_event(
                connection,
                event,
                PluginResult(
                    "BLOCKED_AUTH",
                    {"status": "AUTH_REQUIRED", "published": False},
                    {"channel": "linkedin"},
                    "session missing",
                ),
            )
            row = connection.execute(
                "SELECT status,retry_count,claim_token,payload FROM events_queue WHERE id=?",
                (event_id,),
            ).fetchone()
            self.assertEqual(row["status"], "BLOCKED_AUTH")
            self.assertEqual(row["retry_count"], 0)
            self.assertIsNone(row["claim_token"])
            self.assertEqual(json.loads(row["payload"])["channel"], "linkedin")

    def test_worker_owns_status_for_envelope_plugin(self) -> None:
        event_id = self.enqueue()
        plugin = Path(self.temporary.name) / "envelope_plugin.py"
        plugin.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print('EVENT_RESULT_JSON:' + json.dumps({"
            "'version':1,'outcome':'COMPLETED','result':{'ok':True},"
            "'payload_patch':{'artifact':'done.md'},'error':None,'retryable':False}))\n",
            encoding="utf-8",
        )
        plugin.chmod(plugin.stat().st_mode | stat.S_IXUSR)
        with connect(self.database) as connection:
            connection.execute(
                "INSERT INTO plugin_registry(plugin_name,type,executable_path,is_active) VALUES ('Envelope','test',?,1)",
                (str(plugin),),
            )
            connection.execute(
                "INSERT INTO event_routes(event_type,target_plugin_name) VALUES ('TEST','Envelope')"
            )
            connection.commit()
            self.assertTrue(process_one(connection, self.database, "integration-worker", 30, 1))
            row = connection.execute(
                "SELECT status,payload,claim_token FROM events_queue WHERE id=?", (event_id,)
            ).fetchone()
            self.assertEqual(row["status"], "COMPLETED")
            self.assertEqual(json.loads(row["payload"])["artifact"], "done.md")
            self.assertIsNone(row["claim_token"])

    def test_pro_publisher_missing_auth_becomes_blocked_auth(self) -> None:
        event_id = self.enqueue({"content": "must not be published", "topic": "Safety"})
        plugin = Path(__file__).resolve().parents[1] / "plugins" / "channels" / "pub_medium_pro.py"
        with connect(self.database) as connection:
            connection.execute(
                "INSERT INTO plugin_registry(plugin_name,type,executable_path,is_active) VALUES ('Medium test','channel',?,1)",
                (str(plugin),),
            )
            connection.execute(
                "INSERT INTO event_routes(event_type,target_plugin_name) VALUES ('TEST','Medium test')"
            )
            connection.commit()
            self.assertTrue(process_one(connection, self.database, "auth-worker", 30, 1))
            row = connection.execute(
                "SELECT status,retry_count,payload FROM events_queue WHERE id=?", (event_id,)
            ).fetchone()
            self.assertEqual(row["status"], "BLOCKED_AUTH")
            self.assertEqual(row["retry_count"], 0)
            self.assertFalse(json.loads(row["payload"])["plugin_result"]["published"])


class ProtocolTests(unittest.TestCase):
    def test_envelope_round_trip(self) -> None:
        with patch("builtins.print") as output:
            emit_result("SIMULATED", result={"published": False}, payload_patch={"dry_run": True})
        line = output.call_args.args[0]
        parsed = parse_result(f"diagnostic\n{line}\n")
        self.assertEqual(parsed.outcome, "SIMULATED")
        self.assertTrue(parsed.payload_patch["dry_run"])


class MigrationTests(unittest.TestCase):
    def test_legacy_queue_is_preserved_and_processing_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """CREATE TABLE events_queue(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        payload JSON NOT NULL CHECK(json_valid(payload)),
                        status TEXT NOT NULL DEFAULT 'PENDING'
                            CHECK(status IN ('PENDING','PROCESSING','COMPLETED','FAILED')),
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        error_log TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                connection.execute(
                    "INSERT INTO events_queue(event_type,payload,status) VALUES ('LEGACY','{}','PROCESSING')"
                )
            initialize_database(database)
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT id,event_type,status,claimed_by,claim_token FROM events_queue"
                ).fetchone()
                self.assertEqual(row[:3], (1, "LEGACY", "PENDING"))
                self.assertIsNone(row[3])
                self.assertIsNone(row[4])
                connection.execute(
                    "INSERT INTO events_queue(event_type,payload,status) VALUES ('AUTH','{}','BLOCKED_AUTH')"
                )


class PublicationLedgerTests(DatabaseCase):
    def test_unknown_attempt_requires_reconciliation(self) -> None:
        event_id = self.enqueue({"content": "same publication"})
        payload = {"content": "same publication"}
        with connect(self.database) as connection:
            first = begin_submission(
                connection,
                event_id=event_id,
                channel="LINKEDIN",
                payload=payload,
                target="personal-profile",
            )
            self.assertEqual(first["status"], "SUBMITTED")
            update_publication(connection, event_id, "LINKEDIN", "UNKNOWN", detail="connection lost after submit")
            with self.assertRaisesRegex(RuntimeError, "reconciliation"):
                begin_submission(
                    connection,
                    event_id=event_id,
                    channel="LINKEDIN",
                    payload=payload,
                    target="personal-profile",
                )
            count = connection.execute(
                "SELECT count(*) FROM publication_attempts WHERE event_id=? AND channel='LINKEDIN'",
                (event_id,),
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_confirmed_attempt_is_idempotently_reused(self) -> None:
        event_id = self.enqueue()
        payload = {"content": "hello"}
        with connect(self.database) as connection:
            begin_submission(connection, event_id=event_id, channel="MEDIUM", payload=payload, target="story")
            update_publication(
                connection, event_id, "MEDIUM", "CONFIRMED",
                platform_id="post-123", platform_url="https://medium.example/post-123",
            )
            replay = begin_submission(connection, event_id=event_id, channel="MEDIUM", payload=payload, target="story")
            self.assertEqual(replay["status"], "CONFIRMED")
            self.assertEqual(replay["platform_id"], "post-123")


class TelegramSecurityTests(DatabaseCase):
    def test_missing_allowlist_rejects_every_chat(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEGRAM_ALLOWED_CHAT_IDS", None)
            self.assertFalse(allowed_chat(12345))

    def test_update_and_message_identity_are_unique(self) -> None:
        with connect(self.database) as connection:
            connection.execute(
                "INSERT INTO telegram_updates(update_id,chat_id,message_id) VALUES (1,10,20)"
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO telegram_updates(update_id,chat_id,message_id) VALUES (1,10,21)"
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO telegram_updates(update_id,chat_id,message_id) VALUES (2,10,20)"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
