#!/usr/bin/env python3
"""Shared result-envelope and publication-ledger contract for event plugins."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Final


ENVELOPE_PREFIX: Final = "EVENT_RESULT_JSON:"
OUTCOMES: Final = frozenset(
    {"COMPLETED", "SIMULATED", "BLOCKED_AUTH", "FAILED", "UNKNOWN"}
)
LEDGER_STATUSES: Final = frozenset(
    {"PREPARED", "SUBMITTED", "CONFIRMED", "UNKNOWN", "FAILED", "NEEDS_OPERATOR"}
)


@dataclass(frozen=True)
class PluginResult:
    """Validated result returned by a plugin subprocess."""

    outcome: str
    result: dict[str, Any]
    payload_patch: dict[str, Any]
    error: str | None = None
    retryable: bool = False


def emit_result(
    outcome: str,
    *,
    result: dict[str, Any] | None = None,
    payload_patch: dict[str, Any] | None = None,
    error: str | None = None,
    retryable: bool = False,
) -> None:
    """Emit exactly one machine-readable result line on stdout."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unsupported plugin outcome: {outcome}")
    envelope = {
        "version": 1,
        "outcome": outcome,
        "result": result or {},
        "payload_patch": payload_patch or {},
        "error": error,
        "retryable": bool(retryable),
    }
    print(ENVELOPE_PREFIX + json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), flush=True)


def parse_result(stdout: str) -> PluginResult:
    """Parse and validate the last envelope in plugin stdout."""
    lines = [line for line in stdout.splitlines() if line.startswith(ENVELOPE_PREFIX)]
    if not lines:
        raise ValueError("plugin emitted no EVENT_RESULT_JSON envelope")
    raw = json.loads(lines[-1][len(ENVELOPE_PREFIX) :])
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("plugin result envelope has an unsupported version")
    outcome = raw.get("outcome")
    if outcome not in OUTCOMES:
        raise ValueError(f"plugin returned unsupported outcome: {outcome!r}")
    result = raw.get("result", {})
    patch = raw.get("payload_patch", {})
    if not isinstance(result, dict) or not isinstance(patch, dict):
        raise ValueError("plugin result and payload_patch must be JSON objects")
    error = raw.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError("plugin error must be a string or null")
    return PluginResult(outcome, result, patch, error, bool(raw.get("retryable", False)))


def content_hash(payload: dict[str, Any]) -> str:
    """Create a stable SHA-256 digest of publication-relevant payload data."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prepare_publication(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    channel: str,
    payload: dict[str, Any],
    target: str,
) -> sqlite3.Row:
    """Create a ledger attempt or return the existing idempotency record."""
    digest = content_hash(payload)
    with connection:
        connection.execute(
            """
            INSERT INTO publication_attempts (
                event_id, channel, content_hash, target, attempt_number, status
            ) VALUES (?, ?, ?, ?, 1, 'PREPARED')
            ON CONFLICT(event_id, channel) DO NOTHING
            """,
            (event_id, channel, digest, target),
        )
    row = connection.execute(
        "SELECT * FROM publication_attempts WHERE event_id=? AND channel=?",
        (event_id, channel),
    ).fetchone()
    if row is None:
        raise RuntimeError("publication ledger record could not be created")
    if row["content_hash"] != digest or row["target"] != target:
        raise ValueError("event publication payload or target changed after preparation")
    return row


def update_publication(
    connection: sqlite3.Connection,
    event_id: int,
    channel: str,
    status: str,
    *,
    platform_id: str | None = None,
    platform_url: str | None = None,
    detail: str | None = None,
) -> None:
    """Transition a publication ledger record with bounded diagnostics."""
    if status not in LEDGER_STATUSES:
        raise ValueError(f"unsupported publication status: {status}")
    with connection:
        cursor = connection.execute(
            """
            UPDATE publication_attempts
               SET status=?, platform_id=COALESCE(?, platform_id),
                   platform_url=COALESCE(?, platform_url), detail=?,
                   updated_at=CURRENT_TIMESTAMP
             WHERE event_id=? AND channel=?
            """,
            (status, platform_id, platform_url, detail[:8000] if detail else None, event_id, channel),
        )
        if cursor.rowcount != 1:
            raise LookupError("publication ledger record does not exist")


def begin_submission(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    channel: str,
    payload: dict[str, Any],
    target: str,
) -> sqlite3.Row:
    """Prepare an idempotent attempt and mark it submitted before external I/O."""
    row = prepare_publication(
        connection, event_id=event_id, channel=channel, payload=payload, target=target
    )
    status = str(row["status"])
    if status == "CONFIRMED":
        return row
    if status in {"SUBMITTED", "UNKNOWN"}:
        raise RuntimeError(
            "publication outcome requires reconciliation before another submit"
        )
    with connection:
        if status == "FAILED":
            connection.execute(
                """UPDATE publication_attempts
                      SET status='PREPARED', attempt_number=attempt_number+1,
                          detail=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE event_id=? AND channel=? AND status='FAILED'""",
                (event_id, channel),
            )
        connection.execute(
            """UPDATE publication_attempts
                  SET status='SUBMITTED', updated_at=CURRENT_TIMESTAMP
                WHERE event_id=? AND channel=? AND status='PREPARED'""",
            (event_id, channel),
        )
    return connection.execute(
        "SELECT * FROM publication_attempts WHERE event_id=? AND channel=?",
        (event_id, channel),
    ).fetchone()
