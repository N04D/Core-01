from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from core.scheduler import add_job, dispatch_due_jobs
from core.setup_database import initialize_database
from core.database import connect_database

class SchedulerTests(unittest.TestCase):
    def test_daily_job_dispatches_and_advances(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'events.db'; initialize_database(db)
            add_job(db, 'playbooks/example.py', '2025-01-01T10:00:00+00:00', 'DAILY', {'topic':'x'})
            ids = dispatch_due_jobs(db, now=datetime(2025,1,1,10,1,tzinfo=timezone.utc))
            self.assertEqual(len(ids), 1)
            with connect_database(db) as c:
                self.assertEqual(c.execute('select status from events_queue where id=?',(ids[0],)).fetchone()[0], 'PENDING')
                self.assertEqual(c.execute('select next_run_at from scheduled_jobs').fetchone()[0], '2025-01-02T10:00:00+00:00')

if __name__ == '__main__': unittest.main()
