"""Canonical source and runtime-data paths for Core-01.

Source code lives below :data:`CORE_ROOT`; mutable databases, media, logs and
sessions live below :data:`CORE_DATA`.  Existing checkouts remain compatible:
when ``CORE_DATA`` is unset the historical repository layout is used until the
operator runs ``scripts/migrate_runtime_data.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

CORE_ROOT = Path(os.getenv("CORE_HOME", Path(__file__).resolve().parents[1])).expanduser().resolve()
_configured_data = os.getenv("CORE_DATA")
# Keep mutable data out of the checkout by default while allowing an explicit
# CORE_DATA for production. Legacy <repo>/db and <repo>/vault remain readable
# until migrate_runtime_data.py is run.
CORE_DATA = Path(_configured_data).expanduser().resolve() if _configured_data else (CORE_ROOT / "runtime")

DB_DIR = CORE_DATA / "db"
MEDIA_DIR = CORE_DATA / "media"
ANALYTICS_DIR = CORE_DATA / "analytics"
RESEARCH_DIR = CORE_DATA / "research"
CONCEPTS_DIR = CORE_DATA / "concepts"
PUBLISHED_DIR = CORE_DATA / "published"
LOGS_DIR = CORE_DATA / "logs"
SESSIONS_DIR = CORE_DATA / "sessions"
TMP_DIR = CORE_DATA / "tmp"
DATABASE_PATH = DB_DIR / "events.db"

def ensure_runtime_dirs() -> None:
    """Create mutable runtime directories without touching existing files."""
    for path in (DB_DIR, MEDIA_DIR, ANALYTICS_DIR, RESEARCH_DIR, CONCEPTS_DIR,
                 PUBLISHED_DIR, LOGS_DIR, SESSIONS_DIR, TMP_DIR):
        path.mkdir(parents=True, exist_ok=True)

def legacy_path(name: str) -> Path:
    """Return a historical repository path for migration/compatibility checks."""
    return CORE_ROOT / name
