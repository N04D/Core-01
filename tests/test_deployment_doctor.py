from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from core.setup_database import initialize_database


DOCTOR = Path("scripts/deployment_doctor.py")


def _run(tmp_path: Path, *args: str):
    data = tmp_path / "runtime"
    data.mkdir()
    initialize_database(data / "db" / "events.db")
    env = {**os.environ, "CORE_DATA": str(data), "CORE_HOME": str(Path.cwd())}
    return subprocess.run([sys.executable, str(DOCTOR), *args], env=env, capture_output=True, text=True)


def test_healthy_installation_uses_canonical_core_data_and_returns_zero(tmp_path: Path):
    result = _run(tmp_path)
    assert result.returncode == 0
    assert f"CORE_DATA: OK ({tmp_path / 'runtime'})" in result.stdout
    assert "database: OK" in result.stdout
    assert "database schema: OK" in result.stdout
    assert "GPU (optional):" in result.stdout


def test_missing_database_is_required_failure(tmp_path: Path):
    data = tmp_path / "runtime"
    data.mkdir()
    env = {**os.environ, "CORE_DATA": str(data), "CORE_HOME": str(Path.cwd())}
    result = subprocess.run([sys.executable, str(DOCTOR)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "database: MISSING" in result.stdout


def test_invalid_schema_is_required_failure(tmp_path: Path):
    data = tmp_path / "runtime/db"
    data.mkdir(parents=True)
    (data / "events.db").write_text("not sqlite", encoding="utf-8")
    env = {**os.environ, "CORE_DATA": str(tmp_path / "runtime"), "CORE_HOME": str(Path.cwd())}
    result = subprocess.run([sys.executable, str(DOCTOR)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "database schema: ERROR" in result.stdout


def test_explicit_db_override_works(tmp_path: Path):
    data = tmp_path / "runtime"
    data.mkdir()
    override = tmp_path / "debug.db"
    initialize_database(override)
    env = {**os.environ, "CORE_DATA": str(data), "CORE_HOME": str(Path.cwd())}
    result = subprocess.run([sys.executable, str(DOCTOR), "--db", str(override)], env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert f"database: OK ({override}" in result.stdout
