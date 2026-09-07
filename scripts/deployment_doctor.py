#!/usr/bin/env python3
"""Check required Core-01 capabilities and report optional integrations."""
from __future__ import annotations

import argparse
import importlib.util
import platform
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.database import connect_database
from core.paths import CORE_DATA, CORE_ROOT, DATABASE_PATH, SESSIONS_DIR

REQUIRED_TABLES = {
    "events_queue",
    "event_routes",
    "plugin_registry",
    "publication_attempts",
}


def _status(label: str, state: str, detail: str = "") -> None:
    suffix = f" ({detail})" if detail else ""
    print(f"{label}: {state}{suffix}")


def check_database(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "MISSING"
    try:
        with connect_database(path, read_only=True) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = sorted(REQUIRED_TABLES - tables)
            if missing:
                return False, f"INVALID schema; missing {', '.join(missing)}"
            connection.execute("SELECT 1 FROM events_queue LIMIT 1").fetchone()
        return True, "OK"
    except (OSError, sqlite3.Error, ValueError) as exc:
        return False, f"ERROR: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DATABASE_PATH, help="debug/test database override")
    args = parser.parse_args()
    db = args.db.expanduser().resolve()
    failures = 0

    _status("architecture", "OK", platform.machine())
    python_ok = sys.version_info >= (3, 10)
    _status("python", "OK" if python_ok else "ERROR", f"{sys.executable} ({platform.python_version()})")
    failures += not python_ok
    _status("CORE_HOME", "OK" if CORE_ROOT.is_dir() else "MISSING", str(CORE_ROOT))
    failures += not CORE_ROOT.is_dir()
    data_ok = CORE_DATA.is_dir() and CORE_DATA.exists()
    writable = data_ok and _is_writable(CORE_DATA)
    _status("CORE_DATA", "OK" if data_ok else "MISSING", str(CORE_DATA))
    _status("CORE_DATA writable", "OK" if writable else "ERROR")
    failures += not data_ok or not writable

    db_ok, db_detail = check_database(db)
    _status("database", "OK" if db_ok else ("MISSING" if db_detail == "MISSING" else "ERROR"), str(db))
    _status("database schema", "OK" if db_ok else "ERROR", db_detail if not db_ok else "required tables present")
    failures += not db_ok

    worker = CORE_ROOT / "daemon" / "worker.py"
    _status("worker", "OK" if worker.is_file() else "MISSING", str(worker))
    failures += not worker.is_file()

    if db_ok:
        try:
            with connect_database(db, read_only=True) as connection:
                count = connection.execute("SELECT COUNT(*) FROM plugin_registry WHERE is_active=1").fetchone()[0]
            _status("active plugins", "OK", str(count))
        except sqlite3.Error as exc:
            _status("active plugins", "ERROR", str(exc))
            failures += 1
    else:
        _status("active plugins", "ERROR", "database unavailable")

    _status("GPU (optional)", "AVAILABLE" if shutil.which("nvidia-smi") else "UNAVAILABLE")
    _status("local LLM (optional)", "AVAILABLE" if shutil.which("ollama") else "UNAVAILABLE")
    _status("Playwright (optional)", "AVAILABLE" if importlib.util.find_spec("playwright") else "UNAVAILABLE")
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    _status("Chromium (optional)", "AVAILABLE" if chromium else "UNAVAILABLE")
    for name, filename in (("LinkedIn", "linkedin_auth.json"), ("Substack", "substack_auth.json"), ("Medium", "medium_auth.json")):
        _status(f"{name} session (optional)", "CONNECTED" if (SESSIONS_DIR / filename).is_file() else "AUTH_REQUIRED")
    return int(bool(failures))


def _is_writable(path: Path) -> bool:
    try:
        probe = path / ".core_doctor_write_test"
        probe.touch(exist_ok=False)
        probe.unlink()
        return True
    except (OSError, FileExistsError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
