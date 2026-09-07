#!/usr/bin/env python3
"""Validate LinkedIn Pro bio and analytics safe mock fallbacks."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Final


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
PLUGIN: Final = PROJECT_ROOT / "plugins" / "channels" / "pub_linkedin_pro.py"
MOCK_AUTH: Final = Path(tempfile.gettempdir()) / "core01-linkedin-no-auth.json"
AUTH_FILE: Final = PROJECT_ROOT / "config" / "linkedin_auth.json"
RESEARCH_DIR: Final = PROJECT_ROOT / "vault" / "research"
ANALYTICS_DIR: Final = PROJECT_ROOT / "vault" / "analytics"


def snapshot(directory: Path, pattern: str) -> set[Path]:
    """Return resolved artifacts matching a pattern."""
    directory.mkdir(parents=True, exist_ok=True)
    return {path.resolve() for path in directory.glob(pattern)}


def run(command: list[str]) -> None:
    """Run a plugin mode and mirror exact terminal output."""
    print("$ " + " ".join(command), flush=True)
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"command failed with exit status {result.returncode}")


def exactly_one_new(before: set[Path], directory: Path, pattern: str) -> Path:
    """Require exactly one newly created artifact."""
    created = snapshot(directory, pattern) - before
    if len(created) != 1:
        raise AssertionError(
            f"expected one new {pattern} artifact in {directory}, found {len(created)}"
        )
    return created.pop()


def load_mock(path: Path, expected_mode: str) -> dict[str, Any]:
    """Load and validate shared safe-fallback fields."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AssertionError(f"artifact is not a JSON object: {path}")
    expected = {
        "mode": expected_mode,
        "mock": True,
        "linkedin_contacted": False,
        "status": "AUTH_REQUIRED",
    }
    for key, value in expected.items():
        if data.get(key) != value:
            raise AssertionError(f"unexpected {key} in {path}: {data.get(key)!r}")
    return data


def test_bio() -> tuple[Path, Path]:
    """Execute and validate the profile-reader mock fallback."""
    before_json = snapshot(RESEARCH_DIR, "linkedin_fetch_bio_mock_*.json")
    before_md = snapshot(RESEARCH_DIR, "linkedin_fetch_bio_mock_*.md")
    run(
        [
            sys.executable,
            str(PLUGIN),
            "--fetch-bio",
            "--db",
            str(DATABASE),
            "--auth",
            str(MOCK_AUTH),
        ]
    )
    json_path = exactly_one_new(
        before_json, RESEARCH_DIR, "linkedin_fetch_bio_mock_*.json"
    )
    md_path = exactly_one_new(before_md, RESEARCH_DIR, "linkedin_fetch_bio_mock_*.md")
    data = load_mock(json_path, "fetch_bio")
    for field in ("name", "headline", "bio", "experience"):
        if field not in data:
            raise AssertionError(f"mock bio is missing {field!r}")
    if not md_path.read_text(encoding="utf-8").strip():
        raise AssertionError("mock bio Markdown artifact is empty")
    print(f"[OK] Mock bio JSON: {json_path}")
    print(f"[OK] Mock bio Markdown: {md_path}")
    return json_path, md_path


def test_analytics() -> tuple[Path, Path]:
    """Execute and validate structured analytics mock engagement."""
    before_json = snapshot(ANALYTICS_DIR, "linkedin_analytics_mock_*.json")
    before_md = snapshot(ANALYTICS_DIR, "linkedin_analytics_mock_*.md")
    run(
        [
            sys.executable,
            str(PLUGIN),
            "--analytics",
            "--limit",
            "5",
            "--db",
            str(DATABASE),
            "--auth",
            str(MOCK_AUTH),
        ]
    )
    json_path = exactly_one_new(
        before_json, ANALYTICS_DIR, "linkedin_analytics_mock_*.json"
    )
    md_path = exactly_one_new(
        before_md, ANALYTICS_DIR, "linkedin_analytics_mock_*.md"
    )
    data = load_mock(json_path, "analytics")
    posts = data.get("posts")
    if not isinstance(posts, list) or not posts:
        raise AssertionError("mock analytics contains no posts")
    for post in posts:
        if not isinstance(post, dict) or not all(
            key in post for key in ("content", "likes", "views", "comments")
        ):
            raise AssertionError("mock analytics post has an invalid schema")
        if not isinstance(post["comments"], list):
            raise AssertionError("mock comments must be a list")
    if not md_path.read_text(encoding="utf-8").strip():
        raise AssertionError("mock analytics Markdown artifact is empty")
    print(f"[OK] Mock analytics JSON: {json_path}")
    print(f"[OK] Mock analytics Markdown: {md_path}")
    return json_path, md_path


def main() -> int:
    """Run both safe mock integration tests."""
    if AUTH_FILE.exists():
        print(
            f"[FAIL] Mock test vereist ontbrekende auth-state, maar bestaat: {AUTH_FILE}",
            file=sys.stderr,
        )
        return 2
    if not DATABASE.is_file():
        print(f"[FAIL] Database ontbreekt: {DATABASE}", file=sys.stderr)
        return 2
    try:
        test_bio()
        test_analytics()
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("[PASS] LinkedIn Pro bio- en analytics-mocktests voltooid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
