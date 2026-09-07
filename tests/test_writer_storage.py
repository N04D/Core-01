from pathlib import Path

from core.setup_database import initialize_database


def test_writer_file_api_uses_runtime_editor_roots_and_atomic_markdown(tmp_path, monkeypatch):
    from dashboard import app as dashboard_app

    database = tmp_path / "events.db"
    initialize_database(database)
    concepts = tmp_path / "concepts"
    outgoing = tmp_path / "outgoing"
    legacy = tmp_path / "vault" / "concepten"
    legacy.mkdir(parents=True)
    (legacy / "legacy.md").write_text("legacy", encoding="utf-8")
    monkeypatch.setitem(dashboard_app.EDITOR_AREAS, "concepten", concepts)
    monkeypatch.setitem(dashboard_app.EDITOR_AREAS, "uitgaand", outgoing)
    client = dashboard_app.create_app(database).test_client()

    response = client.put(
        "/api/files/concepten/nested/article.md",
        json={"content": "---\ntitle: Keep\n---\n\n# Body"},
    )
    assert response.status_code == 200
    assert (concepts / "nested/article.md").read_text(encoding="utf-8").startswith("---\n")
    assert not (legacy / "nested/article.md").exists()
    assert client.get("/api/files/concepten/nested/article.md").get_json()["content"].endswith("# Body")

    assert client.put("/api/files/concepten/../escape.md", json={"content": "x"}).status_code == 400
    assert client.put("/api/files/concepten/not-text.txt", json={"content": "x"}).status_code == 400
