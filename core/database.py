#!/usr/bin/env python3
"""Central SQLite connection policy for the local event-driven system."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_BUSY_TIMEOUT_MS = 30_000


def connect_database(
    path: str | Path,
    *,
    timeout: float = 30.0,
    busy_timeout_ms: int | None = None,
    row_factory: Any = sqlite3.Row,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open SQLite with one consistent safety and concurrency configuration."""
    resolved = Path(path).expanduser().resolve()
    if timeout <= 0:
        raise ValueError("SQLite timeout must be positive")
    configured_busy = busy_timeout_ms
    if configured_busy is None:
        configured_busy = int(
            os.getenv("SQLITE_BUSY_TIMEOUT_MS", str(DEFAULT_BUSY_TIMEOUT_MS))
        )
    if configured_busy < 0:
        raise ValueError("SQLite busy timeout must be non-negative")

    if read_only:
        connection = sqlite3.connect(
            f"file:{resolved}?mode=ro", uri=True, timeout=timeout
        )
    else:
        connection = sqlite3.connect(resolved, timeout=timeout)
    try:
        connection.row_factory = row_factory
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={configured_busy:d}")
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        if not read_only:
            if mode.lower() != "wal":
                mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        if mode.lower() != "wal":
            raise sqlite3.OperationalError(
                f"SQLite connection is not in required WAL mode for {resolved}: {mode}"
            )
        return connection
    except Exception:
        connection.close()
        raise
