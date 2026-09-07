from __future__ import annotations

from pathlib import Path

from core.database import connect_database
from core.event_protocol import prepare_publication
from core.reconciler import (
    automatic_reconciliation_candidates,
    operator_required_attempts,
    reconcile_all,
    unresolved_attempts,
)
from core.setup_database import initialize_database


def _attempt(db: Path, status: str, event_id: int) -> int:
    with connect_database(db) as conn:
        row = prepare_publication(conn, event_id=event_id, channel="PUBLISH_TEST", payload={"x": event_id}, target="test")
        conn.execute("UPDATE publication_attempts SET status=? WHERE id=?", (status, row["id"]))
        conn.commit()
        return int(row["id"])


def test_unknown_without_adapter_becomes_operator_and_is_not_reprocessed(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    attempt_id = _attempt(db, "UNKNOWN", 7)
    result = reconcile_all(db)
    assert result[0].status == "NEEDS_OPERATOR"
    assert automatic_reconciliation_candidates(db) == []
    assert operator_required_attempts(db)[0]["id"] == attempt_id
    assert unresolved_attempts(db)[0]["status"] == "NEEDS_OPERATOR"
    assert reconcile_all(db) == []


def test_confirmed_and_failed_attempts_are_not_candidates(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    _attempt(db, "CONFIRMED", 1)
    _attempt(db, "FAILED", 2)
    assert automatic_reconciliation_candidates(db) == []
    assert operator_required_attempts(db) == []


def test_submitted_adapter_confirmation_does_not_republish(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    _attempt(db, "SUBMITTED", 3)

    class Adapter:
        def __init__(self):
            self.calls = 0

        def reconcile(self, attempt):
            self.calls += 1
            return "CONFIRMED", {"detail": "platform URL verified"}

    adapter = Adapter()
    result = reconcile_all(db, {"PUBLISH_TEST": adapter})
    assert result[0].status == "CONFIRMED"
    assert adapter.calls == 1
    assert reconcile_all(db, {"PUBLISH_TEST": adapter}) == []
    assert adapter.calls == 1
