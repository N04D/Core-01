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

## Analytics & Feedback Loop

Analytics is an optional event-bus capability. It stores historical normalized
snapshots, distinguishes unavailable metrics (`NULL`) from measured zero, and
attributes website metrics to publication ledger records when possible.

```bash
./venv/bin/python3 plugins/analytics/website_analytics.py --register --db "$CORE_DATA/db/events.db"
```

Use `mode: SIMULATED` for deterministic offline collection. Configure real
Plausible collection with `PLAUSIBLE_SITE_ID` and `PLAUSIBLE_API_KEY` in the
runtime environment. Dashboard analytics defaults to `REAL`; simulated data is
explicitly filtered and never feeds evergreen decisions. The feedback output
event is consumed by a separate feedback plugin, not by Website Analytics.
See `wiki/Analytics.md` for events, API endpoints and troubleshooting.

LinkedIn Analytics is an optional second provider behind the same dispatcher.
It reuses the LinkedIn Pro storage state under `CORE_DATA/sessions/`, collects
read-only lifetime post metrics, and keeps simulated fixtures isolated from
REAL feedback and evergreen decisions. See `wiki/LinkedIn-Analytics.md`.

## Markdown website publishing

The optional `Markdown Website Git Publisher` consumes `PUBLISH_MARKDOWN_GIT`
events and writes channel variants into a configured local website checkout.
Copy `config/markdown_git.example.json` to the ignored
`config/markdown_git.json`, set `repository_path` and the desired content/media
directories, then register it:

```bash
./venv/bin/python3 plugins/channels/pub_markdown_git.py \
  --register --db "$CORE_DATA/db/events.db"
```

`commit_enabled` and `push_enabled` are conservative opt-ins. Git credentials
remain in the host SSH agent or Git credential manager; they are never stored
in Core-01 configuration. See `wiki/Markdown-Git-Publisher.md` for payload,
dry-run, ledger and reconciliation details.
