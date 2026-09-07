from __future__ import annotations

import json
import subprocess
from pathlib import Path

from core.database import connect_database
from core.reconciler import reconcile_all
from core.setup_database import initialize_database
from plugins.channels.pub_markdown_git import (
    EVENT_TYPE,
    PLUGIN_NAME,
    MarkdownGitReconciler,
    build_publication,
    load_config,
    publish_event,
    register_plugin,
    slugify,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "site"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repo, check=True)
    (repo / "README.md").write_text("site\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def make_event(db: Path, payload: dict) -> int:
    with connect_database(db) as conn:
        cursor = conn.execute("INSERT INTO events_queue(event_type,payload) VALUES (?,?)", (EVENT_TYPE, json.dumps(payload)))
        conn.commit()
        return int(cursor.lastrowid)


def png(path: Path, body: bytes = b"fixture") -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + body)
    return path


def config_for(repo: Path, **overrides):
    values = {"repository_path": str(repo), "content_directory": "content/posts", "media_directory": "static/media", "default_branch": "main", **overrides}
    return load_config(payload=values)


def test_slug_and_frontmatter_are_deterministic_and_safe(tmp_path: Path):
    assert slugify("Café: Een Nieuwe Wereld!") == "cafe-een-nieuwe-wereld"
    config = config_for(tmp_path / "site")
    publication = build_publication(config, {"title": "Quotes: \"safe\"", "body_markdown": "Body", "summary": "A: multiline\nsummary", "tags": ["one", "two"]}, 1)
    assert publication["relative_path"] == "content/posts/quotes-safe.md"
    assert 'title: "Quotes: \'safe\'"' in publication["markdown"] or "title: 'Quotes: \"safe\"'" in publication["markdown"]
    assert "tags:" in publication["markdown"]
    for unsafe in ("../../etc", "/etc/passwd", "~/secret"):
        try:
            build_publication(config, {"title": "Safe", "slug": unsafe, "body_markdown": "body"}, 1)
        except Exception:
            pass
        else:
            raise AssertionError(f"unsafe slug accepted: {unsafe}")


def test_dry_run_has_preview_without_files_or_git_changes(tmp_path: Path):
    repo = make_repo(tmp_path)
    db = tmp_path / "events.db"
    initialize_database(db)
    event_id = make_event(db, {"title": "Dry Run", "body_markdown": "Hello", "repository_path": str(repo), "dry_run": True})
    outcome, result, _ = publish_event(db, event_id)
    assert outcome == "SIMULATED"
    assert result["preview"]["target_file"] == "content/dry-run.md"
    assert not (repo / "content/dry-run.md").exists()
    assert git(repo, "status", "--porcelain") == ""


def test_local_write_media_and_commit_stage_only_explicit_files(tmp_path: Path):
    repo = make_repo(tmp_path)
    db = tmp_path / "events.db"
    initialize_database(db)
    media = tmp_path / "hero.png"
    media.write_bytes(b"\x89PNG\r\n\x1a\nPNG fixture")
    (repo / "unrelated.txt").write_text("keep", encoding="utf-8")
    event_id = make_event(db, {"title": "Owned Article", "body_markdown": "Hello", "repository_path": str(repo), "commit_enabled": True, "media": [{"path": str(media), "alt": "hero"}]})
    outcome, result, _ = publish_event(db, event_id)
    assert outcome == "COMPLETED"
    assert result["commit_sha"] == git(repo, "rev-parse", "HEAD")
    assert (repo / "content/owned-article.md").is_file()
    assert (repo / "static/media/hero.png").read_bytes() == b"\x89PNG\r\n\x1a\nPNG fixture"
    assert "unrelated.txt" not in git(repo, "show", "--format=", "--name-only", "HEAD").splitlines()
    with connect_database(db, read_only=True) as conn:
        ledger = conn.execute("SELECT status,platform_id,detail FROM publication_attempts WHERE event_id=?", (event_id,)).fetchone()
    assert ledger["status"] == "CONFIRMED"
    assert ledger["platform_id"] == result["commit_sha"]


def test_worker_route_registration_and_idempotent_event(tmp_path: Path):
    repo = make_repo(tmp_path)
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        register_plugin(conn, enabled=True)
    event_id = make_event(db, {"title": "Worker Article", "body_markdown": "Body", "repository_path": str(repo), "commit_enabled": True})
    from daemon.worker import process_one
    with connect_database(db) as conn:
        assert process_one(conn, db, "test-worker", 60, 10)
    with connect_database(db, read_only=True) as conn:
        row = conn.execute("SELECT status FROM events_queue WHERE id=?", (event_id,)).fetchone()
        assert row["status"] == "COMPLETED"
        assert conn.execute("SELECT status FROM publication_attempts WHERE event_id=?", (event_id,)).fetchone()["status"] == "CONFIRMED"


def test_idempotent_second_publish_does_not_create_second_commit(tmp_path: Path):
    repo = make_repo(tmp_path)
    db = tmp_path / "events.db"
    initialize_database(db)
    event_id = make_event(db, {"title": "Once", "body_markdown": "Body", "repository_path": str(repo), "commit_enabled": True})
    first, result, _ = publish_event(db, event_id)
    head = git(repo, "rev-parse", "HEAD")
    second, second_result, _ = publish_event(db, event_id)
    assert first == second == "COMPLETED"
    assert result["commit_sha"] == second_result["commit_sha"] == head
    assert len(git(repo, "log", "--oneline").splitlines()) == 2


def test_disabled_and_missing_repository_fail_before_mutation(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    disabled_event = make_event(db, {"title": "Disabled", "body_markdown": "Body", "enabled": False})
    try:
        publish_event(db, disabled_event)
    except Exception as exc:
        assert "disabled" in str(exc).lower()
    else:
        raise AssertionError("disabled publisher was allowed")
    missing_event = make_event(db, {"title": "Missing", "body_markdown": "Body", "repository_path": str(tmp_path / "missing")})
    try:
        publish_event(db, missing_event)
    except Exception as exc:
        assert "repository" in str(exc).lower()
    else:
        raise AssertionError("missing repository was allowed")


def test_push_to_local_bare_remote_when_explicitly_enabled(tmp_path: Path):
    repo = make_repo(tmp_path)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True)
    db = tmp_path / "events.db"
    initialize_database(db)
    event_id = make_event(db, {"title": "Pushed", "body_markdown": "Body", "repository_path": str(repo), "commit_enabled": True, "push_enabled": True})
    outcome, result, _ = publish_event(db, event_id)
    assert outcome == "COMPLETED"
    assert result["commit_sha"] == git(repo, "rev-parse", "HEAD")
    assert git(bare, "rev-parse", "refs/heads/main") == result["commit_sha"]


def test_reconciliation_confirms_expected_local_commit_without_publishing(tmp_path: Path):
    repo = make_repo(tmp_path)
    db = tmp_path / "events.db"
    initialize_database(db)
    event_id = make_event(db, {"title": "Reconcile", "body_markdown": "Body", "repository_path": str(repo), "commit_enabled": True})
    publish_event(db, event_id)
    with connect_database(db, read_only=True) as conn:
        attempt = dict(conn.execute("SELECT * FROM publication_attempts WHERE event_id=?", (event_id,)).fetchone())
    result = MarkdownGitReconciler().reconcile(attempt)
    assert result[0] == "CONFIRMED"
    assert reconcile_all(db, {EVENT_TYPE: MarkdownGitReconciler()}) == []


def test_markdown_unchanged_media_missing_creates_media_commit(tmp_path: Path):
    repo = make_repo(tmp_path)
    db = tmp_path / "events.db"
    initialize_database(db)
    first = make_event(db, {"title": "Media Later", "body_markdown": "Body", "repository_path": str(repo), "commit_enabled": True})
    publish_event(db, first)
    source = png(tmp_path / "hero.png", b"new")
    second = make_event(db, {"title": "Media Later", "body_markdown": "Body", "repository_path": str(repo), "commit_enabled": True, "media": [{"path": str(source)}]})
    outcome, result, _ = publish_event(db, second)
    assert outcome == "COMPLETED"
    assert "static/media/hero.png" in git(repo, "show", "--format=", "--name-only", result["commit_sha"]).splitlines()
    with connect_database(db, read_only=True) as conn:
        files = json.loads(conn.execute("SELECT detail FROM publication_attempts WHERE event_id=?", (second,)).fetchone()[0])["files"]
    assert files["static/media/hero.png"] == __import__("hashlib").sha256(source.read_bytes()).hexdigest()


def test_markdown_unchanged_media_changed_creates_media_commit(tmp_path: Path):
    repo = make_repo(tmp_path)
    db = tmp_path / "events.db"
    initialize_database(db)
    source = png(tmp_path / "hero.png", b"old")
    payload = {"title": "Changed Media", "body_markdown": "Body", "repository_path": str(repo), "commit_enabled": True, "media": [{"path": str(source)}]}
    first = make_event(db, payload)
    publish_event(db, first)
    old_head = git(repo, "rev-parse", "HEAD")
    png(source, b"new")
    second = make_event(db, payload)
    outcome, result, _ = publish_event(db, second)
    assert outcome == "COMPLETED"
    assert result["commit_sha"] != old_head
    assert git(repo, "show", "--format=", "--name-only", result["commit_sha"]).splitlines() == ["static/media/hero.png"]


def test_markdown_changed_media_unchanged_commits_only_markdown(tmp_path: Path):
    repo = make_repo(tmp_path)
    db = tmp_path / "events.db"
    initialize_database(db)
    source = png(tmp_path / "hero.png")
    first_payload = {"title": "Changed Markdown", "body_markdown": "old", "repository_path": str(repo), "commit_enabled": True, "media": [{"path": str(source)}]}
    publish_event(db, make_event(db, first_payload))
    second_payload = {**first_payload, "body_markdown": "new"}
    outcome, result, _ = publish_event(db, make_event(db, second_payload))
    assert outcome == "COMPLETED"
    assert git(repo, "show", "--format=", "--name-only", result["commit_sha"]).splitlines() == ["content/changed-markdown.md"]


def test_all_unchanged_creates_no_commit_and_does_not_repush(tmp_path: Path):
    repo = make_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True)
    db = tmp_path / "events.db"
    initialize_database(db)
    source = png(tmp_path / "hero.png")
    payload = {"title": "No Op", "body_markdown": "body", "repository_path": str(repo), "commit_enabled": True, "push_enabled": True, "media": [{"path": str(source)}]}
    first, first_result, _ = publish_event(db, make_event(db, payload))
    remote_head = git(remote, "rev-parse", "refs/heads/main")
    second, second_result, _ = publish_event(db, make_event(db, payload))
    assert first == second == "COMPLETED"
    assert second_result["commit_sha"] == first_result["commit_sha"]
    assert git(repo, "rev-parse", "HEAD") == remote_head == git(remote, "rev-parse", "refs/heads/main")


def test_multiple_media_mixed_states_and_local_write(tmp_path: Path):
    repo = make_repo(tmp_path)
    db = tmp_path / "events.db"
    initialize_database(db)
    hero = png(tmp_path / "hero.png", b"same")
    chart = png(tmp_path / "chart.png", b"old")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"ID3 audio")
    initial = {"title": "Mixed", "body_markdown": "body", "repository_path": str(repo), "commit_enabled": True, "media": [{"path": str(hero)}, {"path": str(chart)}, {"path": str(audio)}]}
    publish_event(db, make_event(db, initial))
    png(chart, b"changed")
    audio.unlink()
    # Recreate the missing source and perform a local-write (no commit).
    audio.write_bytes(b"ID3 replacement")
    local = {**initial, "commit_enabled": False}
    outcome, _, _ = publish_event(db, make_event(db, local))
    assert outcome == "COMPLETED"
    assert (repo / "static/media/chart.png").read_bytes().endswith(b"changed")
    assert (repo / "static/media/audio.mp3").read_bytes() == b"ID3 replacement"
    assert git(repo, "status", "--porcelain", "--", "content/posts/mixed.md", "static/media/chart.png", "static/media/audio.mp3")


def test_unrelated_staged_changes_are_rejected_and_preserved(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / "unrelated.txt").write_text("keep", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=repo, check=True)
    db = tmp_path / "events.db"
    initialize_database(db)
    event_id = make_event(db, {"title": "Staged Guard", "body_markdown": "body", "repository_path": str(repo), "commit_enabled": True})
    try:
        publish_event(db, event_id)
    except Exception as exc:
        assert "staged" in str(exc).lower()
    else:
        raise AssertionError("unrelated staged change was committed")
    assert "unrelated.txt" in (git(repo, "diff", "--cached", "--name-only").splitlines())
    assert not (repo / "content/staged-guard.md").exists()


def test_reconciliation_detects_changed_media_after_publication(tmp_path: Path):
    repo = make_repo(tmp_path)
    db = tmp_path / "events.db"
    initialize_database(db)
    source = png(tmp_path / "hero.png")
    event_id = make_event(db, {"title": "Detect Media", "body_markdown": "body", "repository_path": str(repo), "commit_enabled": True, "media": [{"path": str(source)}]})
    publish_event(db, event_id)
    (repo / "static/media/hero.png").write_bytes(b"\x89PNG\r\n\x1a\nchanged")
    with connect_database(db, read_only=True) as conn:
        attempt = dict(conn.execute("SELECT * FROM publication_attempts WHERE event_id=?", (event_id,)).fetchone())
    result = MarkdownGitReconciler().reconcile(attempt)
    assert result[0] == "NEEDS_OPERATOR"


def test_dashboard_exposes_plugin_settings_and_ledger_history(tmp_path: Path):
    db = tmp_path / "events.db"
    initialize_database(db)
    with connect_database(db) as conn:
        register_plugin(conn, enabled=True)
    from dashboard.app import create_app
    client = create_app(db).test_client()
    settings = client.get(f"/api/plugins/{PLUGIN_NAME}/settings")
    assert settings.status_code == 200
    assert settings.get_json()["plugin"]["plugin_name"] == PLUGIN_NAME
    assert "config" in settings.get_json()
    history = client.get("/api/publication-attempts?channel=PUBLISH_MARKDOWN_GIT")
    assert history.status_code == 200
