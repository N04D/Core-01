"""SQLite-backed scheduler for recurring playbook jobs."""
from __future__ import annotations
import calendar, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from core.database import connect_database
FREQUENCIES = {"ONCE", "DAILY", "WEEKLY", "MONTHLY"}
def add_job(db_path: str | Path, playbook: str, scheduled_time: str, frequency: str = "ONCE", payload: dict | None = None) -> int:
    frequency = frequency.upper(); datetime.fromisoformat(scheduled_time.replace("Z", "+00:00"))
    if frequency not in FREQUENCIES: raise ValueError("frequency must be ONCE, DAILY, WEEKLY, or MONTHLY")
    with connect_database(db_path) as db:
        cur = db.execute("INSERT INTO scheduled_jobs(playbook,payload,scheduled_time,frequency,next_run_at) VALUES (?,?,?,?,?)", (playbook, json.dumps(payload or {}, ensure_ascii=False), scheduled_time, frequency, scheduled_time)); db.commit(); return int(cur.lastrowid)
def _next(value: datetime, frequency: str) -> datetime | None:
    if frequency == "DAILY": return value + timedelta(days=1)
    if frequency == "WEEKLY": return value + timedelta(days=7)
    if frequency == "MONTHLY":
        month = value.month % 12 + 1; year = value.year + value.month // 12
        return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))
    return None
def dispatch_due_jobs(db_path: str | Path, *, now: datetime | None = None, limit: int = 20) -> list[int]:
    now = now or datetime.now(timezone.utc); now_iso = now.isoformat(timespec="seconds"); dispatched = []
    with connect_database(db_path) as db:
        rows = db.execute("SELECT * FROM scheduled_jobs WHERE status='ACTIVE' AND next_run_at<=? ORDER BY next_run_at,id LIMIT ?", (now_iso, limit)).fetchall()
        for row in rows:
            payload = json.loads(row["payload"]); payload.update(playbook=row["playbook"], scheduled_job_id=row["id"])
            cur = db.execute("INSERT INTO events_queue(event_type,payload,status) VALUES (?,?, 'PENDING')", ("PLAYBOOK_RUN", json.dumps(payload, ensure_ascii=False))); dispatched.append(int(cur.lastrowid))
            nxt = _next(datetime.fromisoformat(row["next_run_at"].replace("Z", "+00:00")), row["frequency"])
            db.execute("UPDATE scheduled_jobs SET last_run_at=?,next_run_at=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (now_iso, nxt.isoformat(timespec="seconds") if nxt else None, "ACTIVE" if nxt else "COMPLETED", row["id"]))
        db.commit()
    return dispatched
