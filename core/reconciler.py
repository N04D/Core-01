"""Safe publication-ledger reconciliation primitives.

Channel adapters may implement ``reconcile`` using strong platform evidence.
This module never retries an unresolved external side effect: absent proof it
transitions the attempt to NEEDS_OPERATOR for explicit human resolution.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Any
from core.database import connect_database
from core.event_protocol import update_publication

UNRESOLVED = ("SUBMITTED", "UNKNOWN")

class ChannelReconciler(Protocol):
    def reconcile(self, attempt: Any) -> tuple[str, dict[str, Any]]: ...

@dataclass(frozen=True)
class ReconciliationResult:
    attempt_id: int
    status: str
    detail: str

def unresolved_attempts(database: str) -> list[dict[str, Any]]:
    with connect_database(database, read_only=True) as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM publication_attempts WHERE status IN ('SUBMITTED','UNKNOWN','NEEDS_OPERATOR') ORDER BY updated_at,id"
        )]

def reconcile_one(database: str, attempt: dict[str, Any], adapter: ChannelReconciler | None = None) -> ReconciliationResult:
    if adapter is None:
        status, detail = "NEEDS_OPERATOR", "No channel reconciliation adapter configured; verify platform manually."
    else:
        status, evidence = adapter.reconcile(attempt)
        if status not in {"CONFIRMED", "FAILED", "NEEDS_OPERATOR"}:
            status, evidence = "NEEDS_OPERATOR", {"detail": "Adapter returned no conclusive outcome."}
        detail = str(evidence.get("detail", evidence))[:8000]
    with connect_database(database) as db:
        update_publication(db, int(attempt["event_id"]), str(attempt["channel"]), status, platform_id=attempt.get("platform_id"), platform_url=attempt.get("platform_url"), detail=detail)
    return ReconciliationResult(int(attempt["id"]), status, detail)

def reconcile_all(database: str, adapter_by_channel: dict[str, ChannelReconciler] | None = None) -> list[ReconciliationResult]:
    adapters = adapter_by_channel or {}
    return [reconcile_one(database, attempt, adapters.get(str(attempt["channel"]))) for attempt in unresolved_attempts(database)]
