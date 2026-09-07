# Core-01

Core-01 is a local-first event-driven AI OS. It deliberately keeps the control
plane small: SQLite is the durable event bus, the worker owns queue state, and
plugins return structured result envelopes.

```text
inputs → SQLite event bus → worker → route → plugin → result envelope
                                      ↓
                              publication ledger
                                      ↓
                                  reconciler
```

## Development

```bash
python3 -m venv venv
./venv/bin/pip install -e '.[dev,browser,media]'
./venv/bin/python3 core/setup_database.py --database db/events.db
./venv/bin/python3 -m pytest -q
./venv/bin/ruff check .
```

Runtime data is separate from source. Set `CORE_HOME` to the checkout and
`CORE_DATA` to a writable data directory (for production, for example
`/var/lib/core-01`). Existing `vault/` and `db/` files are never deleted;
preview and migrate them with:

```bash
CORE_DATA=/var/lib/core-01 ./venv/bin/python3 scripts/migrate_runtime_data.py --dry-run
CORE_DATA=/var/lib/core-01 ./venv/bin/python3 scripts/migrate_runtime_data.py
```

Run the deployment doctor for optional capability diagnostics:

```bash
./venv/bin/python3 scripts/deployment_doctor.py
```

See `wiki/Architecture.md` and `wiki/Deployment.md` for the complete event,
session, reconciliation and systemd model.
