from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from core.paths import CORE_ROOT
from core.setup_database import initialize_database
from scripts.migrate_runtime_data import migrate

def test_runtime_paths_can_be_selected(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_DATA", str(tmp_path / "runtime"))
    # module-level constants are intentionally immutable per process; validate
    # the public resolver in a fresh interpreter to mirror deployment startup.
    script = "from core.paths import CORE_DATA, DATABASE_PATH; print(CORE_DATA); print(DATABASE_PATH)"
    import subprocess, sys
    out = subprocess.check_output([sys.executable, "-c", script], env={**os.environ, "CORE_DATA": str(tmp_path / "runtime")}, text=True)
    assert str(tmp_path / "runtime") in out
    assert str(tmp_path / "runtime" / "db" / "events.db") in out

def test_legacy_migration_preserves_existing_destination(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "runtime"
    (source / "db").mkdir(parents=True)
    (source / "db" / "events.db").write_text("legacy", encoding="utf-8")
    (destination / "db").mkdir(parents=True)
    (destination / "db" / "events.db").write_text("new", encoding="utf-8")
    actions = migrate(source, destination)
    assert "SKIP exists" in actions[0]
    assert (destination / "db" / "events.db").read_text(encoding="utf-8") == "new"


def test_migration_populates_empty_destination(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "runtime"
    (source / "vault/media").mkdir(parents=True)
    (source / "vault/media/image.jpg").write_bytes(b"image")
    actions = migrate(source, destination)
    assert (destination / "media/image.jpg").read_bytes() == b"image"
    assert any(action.startswith("CREATE DIR") for action in actions)


def test_migration_merges_nested_trees_and_is_idempotent(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "runtime"
    (source / "vault/media/nightcafe/nested").mkdir(parents=True)
    (source / "vault/media/a.jpg").write_text("a", encoding="utf-8")
    (source / "vault/media/b.jpg").write_text("b", encoding="utf-8")
    (source / "vault/media/nightcafe/nested/c.jpg").write_text("c", encoding="utf-8")
    (source / "vault/media/nightcafe/nested/d.jpg").write_text("d", encoding="utf-8")
    (destination / "media/nightcafe/nested").mkdir(parents=True)
    (destination / "media/a.jpg").write_text("existing", encoding="utf-8")
    (destination / "media/nightcafe/nested/c.jpg").write_text("existing", encoding="utf-8")

    actions = migrate(source, destination)
    assert (destination / "media/a.jpg").read_text(encoding="utf-8") == "existing"
    assert (destination / "media/b.jpg").read_text(encoding="utf-8") == "b"
    assert (destination / "media/nightcafe/nested/c.jpg").read_text(encoding="utf-8") == "existing"
    assert (destination / "media/nightcafe/nested/d.jpg").read_text(encoding="utf-8") == "d"
    assert any(action.startswith("SKIP exists") for action in actions)
    before = sorted(str(path.relative_to(destination)) for path in destination.rglob("*"))
    second = migrate(source, destination)
    after = sorted(str(path.relative_to(destination)) for path in destination.rglob("*"))
    assert before == after
    assert not any(action.startswith("COPY") for action in second)


def test_migration_dry_run_and_missing_source_do_not_modify(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "runtime"
    (source / "vault/media/nested").mkdir(parents=True)
    (source / "vault/media/nested/file.jpg").write_text("data", encoding="utf-8")
    actions = migrate(source, destination, dry_run=True)
    assert any(action.startswith("CREATE DIR") for action in actions)
    assert any(action.startswith("COPY") for action in actions)
    assert not destination.exists()
    missing = migrate(tmp_path / "missing", tmp_path / "other", dry_run=True)
    assert any(action.startswith("MISSING source") for action in missing)


def test_bootstrap_only_creates_source_controlled_vault_skills():
    script = Path("bootstrap_env.sh").read_text(encoding="utf-8")
    assert '"${SCRIPT_DIR}/vault/skills"' in script
    for legacy_runtime in ("vault/concepten", "vault/gepubliceerd", "vault/research", "vault/logs", "vault/uitgaand"):
        assert legacy_runtime not in script

def test_nightcafe_dashboard_enqueue_and_persistent_status(tmp_path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO plugin_registry(plugin_name,type,executable_path,is_active) VALUES (?,?,?,1)", ("NightCafe Daily Stock Generator", "media", "plugin",))
        conn.execute("INSERT INTO event_routes(event_type,target_plugin_name) VALUES (?,?)", ("NIGHTCAFE_GENERATE", "NightCafe Daily Stock Generator"))
        conn.commit()
    from dashboard.app import create_app
    app = create_app(db)
    client = app.test_client()
    response = client.post("/api/nightcafe/generations", json={"prompt": "test", "simulate": True})
    assert response.status_code == 202
    event_id = response.get_json()["event_id"]
    status = client.get(f"/api/nightcafe/generations/{event_id}")
    assert status.status_code == 200
    assert status.get_json()["status"] == "PENDING"
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT payload FROM events_queue WHERE id=?", (event_id,)).fetchone()
    assert json.loads(row[0])["mode"] == "SIMULATED"

def test_fastapi_nightcafe_adapter_is_durable(tmp_path, monkeypatch):
    db = tmp_path / "events.db"
    initialize_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO event_routes(event_type,target_plugin_name) VALUES (?,?)", ("NIGHTCAFE_GENERATE", "NightCafe Daily Stock Generator"))
        conn.commit()
    import importlib.util
    spec = importlib.util.spec_from_file_location("nightcafe_api", Path(__file__).resolve().parents[1] / "app.py")
    nightcafe_api = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(nightcafe_api)
    monkeypatch.setattr(nightcafe_api, "DB", db)
    created = nightcafe_api.start_generation(nightcafe_api.GenerationRequest(prompt="durable", live=False))
    assert created["status"] == "PENDING"
    state = nightcafe_api.generation_status(str(created["event_id"]))
    assert state["status"] == "PENDING"
