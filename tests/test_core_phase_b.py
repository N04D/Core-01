#!/usr/bin/env python3
"""Focused coverage for Phase B reliability infrastructure."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.database import DEFAULT_BUSY_TIMEOUT_MS, connect_database
from core.processes import popen_process_group, terminate_process_group
from core.setup_database import initialize_database
from daemon.worker import backoff_seconds, claim_event, execute_plugin, record_failure


class DatabaseFactoryTests(unittest.TestCase):
    def test_factory_enforces_connection_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "factory.db"
            with connect_database(database) as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0],
                    DEFAULT_BUSY_TIMEOUT_MS,
                )
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO sample(value) VALUES ('row-factory')")
                row = connection.execute("SELECT * FROM sample").fetchone()
                self.assertEqual(row["value"], "row-factory")

    def test_read_only_factory_keeps_safety_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "readonly.db"
            with connect_database(database) as connection:
                connection.execute("CREATE TABLE sample(id INTEGER)")
            with connect_database(database, read_only=True) as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                with self.assertRaises(Exception):
                    connection.execute("INSERT INTO sample VALUES (1)")


class BackoffTests(unittest.TestCase):
    def test_exponential_backoff_and_jitter_are_bounded(self) -> None:
        self.assertEqual(backoff_seconds(1, base=2, maximum=300, jitter=0.25, random_value=0.5), 2)
        self.assertEqual(backoff_seconds(4, base=2, maximum=300, jitter=0.25, random_value=0.5), 16)
        self.assertEqual(backoff_seconds(20, base=2, maximum=300, jitter=0.25, random_value=1), 300)
        self.assertEqual(backoff_seconds(2, base=10, maximum=300, jitter=0.25, random_value=0), 15)
        self.assertEqual(backoff_seconds(2, base=10, maximum=300, jitter=0.25, random_value=1), 25)

    def test_future_retry_is_not_claimable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "queue.db"
            initialize_database(database)
            with connect_database(database) as connection:
                cursor = connection.execute(
                    "INSERT INTO events_queue(event_type,payload) VALUES ('TEMP','{}')"
                )
                event_id = int(cursor.lastrowid)
                connection.commit()
                event = claim_event(connection, "worker-a", 60)
                assert event is not None
                with patch.dict(
                    os.environ,
                    {"RETRY_BACKOFF_BASE_SECONDS": "60", "RETRY_BACKOFF_JITTER": "0"},
                ):
                    record_failure(connection, event, "temporary network failure")
                row = connection.execute(
                    "SELECT status,retry_count,next_attempt_at,claim_token FROM events_queue WHERE id=?",
                    (event_id,),
                ).fetchone()
                self.assertEqual(row["status"], "PENDING")
                self.assertEqual(row["retry_count"], 1)
                self.assertIsNone(row["claim_token"])
                self.assertIsNone(claim_event(connection, "worker-b", 60))


class ProcessGroupTests(unittest.TestCase):
    def test_timeout_cleanup_kills_parent_and_descendant_group(self) -> None:
        child_code = (
            "import os,signal,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print(os.getpid(), flush=True);"
            "time.sleep(60)"
        )
        parent_code = (
            "import signal,subprocess,sys,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.PIPE,text=True);"
            "print(p.stdout.readline().strip(), flush=True);"
            "time.sleep(60)"
        )
        process = popen_process_group(
            [sys.executable, "-c", parent_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        child_pid = int(process.stdout.readline().strip())
        terminate_process_group(process, grace_seconds=0.1)
        process.communicate()
        self.assertIsNotNone(process.returncode)
        self.assertLess(process.returncode or 0, 0)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            stat_path = Path(f"/proc/{child_pid}/stat")
            if not stat_path.exists() or stat_path.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            self.fail("descendant process survived process-group cleanup")

    def test_worker_timeout_cleans_plugin_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "timeout.db"
            pid_file = root / "child.pid"
            plugin = root / "slow_plugin.py"
            plugin.write_text(
                "#!/usr/bin/env python3\n"
                "import subprocess,sys,time\n"
                f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                f"open({str(pid_file)!r},'w').write(str(p.pid))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            plugin.chmod(0o700)
            initialize_database(database)
            with connect_database(database) as connection:
                connection.execute(
                    "INSERT INTO events_queue(event_type,payload) VALUES ('SLOW','{}')"
                )
                connection.commit()
                event = claim_event(connection, "timeout-worker", 10)
                assert event is not None
            with patch.dict(os.environ, {"PLUGIN_TIMEOUT_SECONDS": "0.2"}):
                with self.assertRaises(subprocess.TimeoutExpired):
                    execute_plugin(plugin, event, database, 10, 0.05)
            child_pid = int(pid_file.read_text())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                stat_path = Path(f"/proc/{child_pid}/stat")
                if not stat_path.exists() or stat_path.read_text().split()[2] == "Z":
                    break
                time.sleep(0.02)
            else:
                self.fail("plugin descendant survived worker timeout cleanup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
