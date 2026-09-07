#!/usr/bin/env python3
"""Report required and optional Core-01 deployment capabilities."""
from __future__ import annotations
import argparse
import importlib.util
import os
import platform
import shutil
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.paths import CORE_DATA, CORE_ROOT, DATABASE_PATH, SESSIONS_DIR

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DATABASE_PATH)
    args = parser.parse_args()
    db = args.db.expanduser().resolve()
    print(f"architecture: {platform.machine()}")
    print(f"python: {sys.executable} ({platform.python_version()})")
    print(f"CORE_HOME: {CORE_ROOT}")
    print(f"CORE_DATA: {CORE_DATA}")
    print(f"database: {'OK' if db.is_file() else 'MISSING'} ({db})")
    print(f"worker: {'OK' if (CORE_ROOT/'daemon/worker.py').is_file() else 'MISSING'}")
    gpu = shutil.which("nvidia-smi")
    print(f"GPU (optional): {'AVAILABLE' if gpu else 'UNAVAILABLE'}")
    print(f"local LLM (optional): {'AVAILABLE' if shutil.which('ollama') else 'UNAVAILABLE'}")
    print(f"Playwright (optional): {'AVAILABLE' if importlib.util.find_spec('playwright') else 'UNAVAILABLE'}")
    print(f"Chromium (optional): {'AVAILABLE' if shutil.which('chromium') or shutil.which('chromium-browser') else 'UNAVAILABLE'}")
    if db.is_file():
        try:
            with sqlite3.connect(db) as conn:
                rows = conn.execute("SELECT plugin_name FROM plugin_registry WHERE is_active=1").fetchall()
            print("configured sessions:")
            for name, filename in (("linkedin", "linkedin_auth.json"), ("substack", "substack_auth.json"), ("medium", "medium_auth.json")):
                print(f"  {name}: {'CONNECTED' if (SESSIONS_DIR/filename).is_file() else 'AUTH_REQUIRED'}")
            print(f"active plugins: {len(rows)}")
        except sqlite3.Error as exc:
            print(f"database state: ERROR ({exc})")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
