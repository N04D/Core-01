from __future__ import annotations
from pathlib import Path
from core.database import connect_database
from core.event_protocol import prepare_publication
from core.reconciler import reconcile_all, unresolved_attempts
from core.setup_database import initialize_database

def test_unknown_never_retries_and_becomes_operator_action(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        row = prepare_publication(conn, event_id=7, channel="PUBLISH_TEST", payload={"x": 1}, target="test")
        conn.execute("UPDATE publication_attempts SET status='UNKNOWN' WHERE id=?", (row["id"],))
        conn.commit()
    result = reconcile_all(db)
    assert result[0].status == "NEEDS_OPERATOR"
    assert unresolved_attempts(db)[0]["status"] == "NEEDS_OPERATOR"
