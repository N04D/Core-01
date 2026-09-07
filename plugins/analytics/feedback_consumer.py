#!/usr/bin/env python3
"""Downstream consumer for performance feedback events.

It deliberately does not collect or aggregate analytics.  The aggregation
plugin has already persisted the evidence; this consumer provides a distinct
event lifecycle for future editorial/evergreen consumers.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.database import connect_database
from core.event_protocol import emit_result
from core.paths import DATABASE_PATH
from core.setup_database import initialize_database

EVENT_TYPE = "CONTENT_PERFORMANCE_UPDATED"
PLUGIN_NAME = "Analytics Feedback / Evergreen Feedback"
DEFAULT_DB = DATABASE_PATH


def register_plugin(connection: Any, enabled: bool = True) -> None:
    executable = str(Path(__file__).resolve())
    with connection:
        connection.execute("INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active) VALUES (?,?,?,?,?) ON CONFLICT(plugin_name) DO UPDATE SET executable_path=excluded.executable_path,type=excluded.type,icon=excluded.icon,is_active=excluded.is_active", (PLUGIN_NAME, "analytics", executable, "🧭", int(enabled)))
        connection.execute("INSERT INTO event_routes(event_type,target_plugin_name) VALUES (?,?) ON CONFLICT(event_type) DO UPDATE SET target_plugin_name=excluded.target_plugin_name", (EVENT_TYPE, PLUGIN_NAME))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    initialize_database(args.db)
    if args.register:
        with connect_database(args.db) as connection:
            register_plugin(connection)
        print(json.dumps({"status": "REGISTERED", "plugin": PLUGIN_NAME}))
        return 0
    if args.event_id is None:
        parser.error("--event_id is required unless --register is used")
    with connect_database(args.db, read_only=True) as connection:
        row = connection.execute("SELECT event_type,payload FROM events_queue WHERE id=?", (args.event_id,)).fetchone()
    if row is None or row["event_type"] != EVENT_TYPE:
        emit_result("FAILED", error="feedback event does not exist or has an invalid type")
        return 0
    payload = json.loads(row["payload"])
    mode = str(payload.get("mode", "REAL")).upper()
    emit_result("SIMULATED" if mode == "SIMULATED" else "COMPLETED", result={"consumed": True, "mode": mode})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
