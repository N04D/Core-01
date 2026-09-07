#!/usr/bin/env python3
"""Publish Core-01 Markdown variants to a user-owned Git website repository.

The plugin is intentionally a small event-bus adapter.  It never runs shell
commands, stages ``git add .`` or changes files outside the configured website
repository.  Configuration is supplied by JSON (or environment variables) and
event payload values may override non-secret publication options.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.database import connect_database
from core.event_protocol import begin_submission, emit_result, update_publication
from core.paths import DATABASE_PATH

try:
    import yaml
except ImportError as exc:  # pragma: no cover - bootstrap installs PyYAML
    raise RuntimeError("PyYAML is required for Markdown Git frontmatter") from exc


PLUGIN_NAME: Final = "Markdown Website Git Publisher"
PLUGIN_TYPE: Final = "channel"
PLUGIN_ICON: Final = "📝"
EVENT_TYPE: Final = "PUBLISH_MARKDOWN_GIT"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE: Final = DATABASE_PATH
DEFAULT_CONFIG: Final = PROJECT_ROOT / "config" / "markdown_git.json"
LOGGER: Final = logging.getLogger("pub_markdown_git")
ALLOWED_MEDIA: Final = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".mp3", ".mp4"}
SLUG_SEPARATOR: Final = re.compile(r"[^a-z0-9]+")


class MarkdownGitError(RuntimeError):
    """A safe, user-actionable publication error."""


@dataclass(frozen=True)
class GitConfig:
    enabled: bool = True
    repository_path: Path = PROJECT_ROOT / "website"
    content_directory: str = "content"
    media_directory: str = "static/media"
    base_url: str = ""
    url_strategy: str = "posts-slug"
    default_branch: str = "main"
    push_enabled: bool = False
    commit_enabled: bool = False
    author_name: str = "Core-01"
    author_email: str = "core-01@localhost"
    frontmatter_format: str = "yaml"
    filename_strategy: str = "slug.md"
    git_remote: str = "origin"


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _env_config() -> dict[str, Any]:
    values: dict[str, Any] = {}
    mapping = {
        "MARKDOWN_GIT_REPOSITORY_PATH": "repository_path",
        "MARKDOWN_GIT_CONTENT_DIRECTORY": "content_directory",
        "MARKDOWN_GIT_MEDIA_DIRECTORY": "media_directory",
        "MARKDOWN_GIT_BASE_URL": "base_url",
        "MARKDOWN_GIT_URL_STRATEGY": "url_strategy",
        "MARKDOWN_GIT_BRANCH": "default_branch",
        "MARKDOWN_GIT_AUTHOR_NAME": "author_name",
        "MARKDOWN_GIT_AUTHOR_EMAIL": "author_email",
        "MARKDOWN_GIT_FILENAME_STRATEGY": "filename_strategy",
        "MARKDOWN_GIT_REMOTE": "git_remote",
    }
    for env_name, key in mapping.items():
        if os.getenv(env_name):
            values[key] = os.environ[env_name]
    for env_name, key in (("MARKDOWN_GIT_ENABLED", "enabled"), ("MARKDOWN_GIT_PUSH_ENABLED", "push_enabled"), ("MARKDOWN_GIT_COMMIT_ENABLED", "commit_enabled")):
        if os.getenv(env_name) is not None:
            values[key] = _bool(os.environ[env_name])
    return values


def load_config(path: Path | None = None, payload: dict[str, Any] | None = None) -> GitConfig:
    """Load non-secret plugin configuration, then apply event overrides."""
    source = path or Path(os.getenv("MARKDOWN_GIT_CONFIG", DEFAULT_CONFIG))
    values: dict[str, Any] = {}
    if source.is_file():
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Markdown Git configuration must be a JSON object")
        values.update(raw)
    values.update(_env_config())
    if payload:
        for key in GitConfig.__dataclass_fields__:
            if key in payload and key != "repository_path":
                values[key] = payload[key]
        if "repository_path" in payload:
            values["repository_path"] = payload["repository_path"]
    defaults = GitConfig()
    repository = Path(str(values.get("repository_path", defaults.repository_path))).expanduser()
    if not repository.is_absolute():
        repository = PROJECT_ROOT / repository
    return GitConfig(
        enabled=_bool(values.get("enabled"), defaults.enabled),
        repository_path=repository.resolve(),
        content_directory=str(values.get("content_directory", defaults.content_directory)),
        media_directory=str(values.get("media_directory", defaults.media_directory)),
        base_url=str(values.get("base_url", defaults.base_url)).rstrip("/"),
        url_strategy=str(values.get("url_strategy", defaults.url_strategy)),
        default_branch=str(values.get("default_branch", defaults.default_branch)),
        push_enabled=_bool(values.get("push_enabled"), defaults.push_enabled),
        commit_enabled=_bool(values.get("commit_enabled"), defaults.commit_enabled),
        author_name=str(values.get("author_name", defaults.author_name)),
        author_email=str(values.get("author_email", defaults.author_email)),
        frontmatter_format=str(values.get("frontmatter_format", defaults.frontmatter_format)),
        filename_strategy=str(values.get("filename_strategy", defaults.filename_strategy)),
        git_remote=str(values.get("git_remote", defaults.git_remote)),
    )


def safe_relative(value: str, *, label: str) -> Path:
    """Validate a repository-relative path and reject traversal/sensitive roots."""
    if not isinstance(value, str) or not value.strip():
        raise MarkdownGitError(f"{label} must be a non-empty relative path")
    candidate = Path(value)
    if candidate.is_absolute() or "~" in candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise MarkdownGitError(f"{label} must remain relative and cannot contain traversal")
    if any(part in {".git", ".ssh", ".gnupg"} for part in candidate.parts):
        raise MarkdownGitError(f"{label} targets a protected repository path")
    if any("\\" in part or any(ord(char) < 32 for char in part) for part in candidate.parts):
        raise MarkdownGitError(f"{label} contains unsafe characters")
    return candidate


def under(root: Path, relative: str, *, label: str) -> Path:
    root = root.resolve()
    relative_path = safe_relative(relative, label=label)
    target = (root / relative_path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise MarkdownGitError(f"{label} escapes the repository") from exc
    return target


def slugify(value: str) -> str:
    """Generate a deterministic ASCII slug with deliberate Unicode handling."""
    normalized = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
    slug = SLUG_SEPARATOR.sub("-", normalized).strip("-")
    if not slug:
        raise MarkdownGitError("title/slug produces an empty slug")
    return slug


def parse_source(raw: str) -> tuple[dict[str, Any], str]:
    """Parse optional YAML frontmatter from a Markdown source."""
    if not raw.lstrip().startswith("---"):
        return {}, raw.strip()
    match = re.match(r"\A\s*---\s*\n(.*?)\n---\s*\n?(.*)\Z", raw, re.DOTALL)
    if not match:
        raise MarkdownGitError("invalid Markdown frontmatter")
    metadata = yaml.safe_load(match.group(1)) or {}
    if not isinstance(metadata, dict):
        raise MarkdownGitError("frontmatter must be a YAML object")
    return metadata, match.group(2).strip()


def read_content(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    raw = payload.get("body_markdown", payload.get("content"))
    if not isinstance(raw, str) or not raw.strip():
        path_value = payload.get("variant_path", payload.get("draft_file", payload.get("filepath")))
        if not isinstance(path_value, str) or not path_value.strip():
            raise MarkdownGitError("payload requires body_markdown, content, variant_path or draft_file")
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.resolve().is_file():
            raise MarkdownGitError(f"Markdown source does not exist: {path}")
        raw = path.resolve().read_text(encoding="utf-8")
    metadata, body = parse_source(raw)
    if not body:
        raise MarkdownGitError("Markdown body is empty")
    merged = dict(metadata)
    merged.update({key: value for key, value in payload.items() if value is not None})
    return merged, body


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    values = value.split(",") if isinstance(value, str) else value
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise MarkdownGitError("tags must be a string or list of strings")
    result: list[str] = []
    for item in values:
        tag = " ".join(item.split()).strip()
        if tag and tag.casefold() not in {existing.casefold() for existing in result}:
            result.append(tag)
    return result


def render_frontmatter(document: dict[str, Any], *, title: str, slug: str, published_at: str, base_url: str, url_strategy: str) -> str:
    tags = normalize_tags(document.get("tags"))
    summary = document.get("summary", document.get("description", ""))
    status = str(document.get("status", "published")).lower()
    if status not in {"draft", "published"}:
        raise MarkdownGitError("status must be draft or published")
    frontmatter: dict[str, Any] = {
        "title": title,
        "slug": slug,
        "date": str(document.get("published_at", published_at)),
        "summary": str(summary) if summary is not None else "",
        "tags": tags,
        "draft": status == "draft",
    }
    if base_url:
        frontmatter["canonical_url"] = canonical_url(base_url, slug, url_strategy)
    return "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n\n"


def canonical_url(base_url: str, slug: str, strategy: str) -> str:
    if strategy == "posts-slug":
        suffix = f"/posts/{slug}/"
    elif strategy == "slug":
        suffix = f"/{slug}/"
    else:
        raise MarkdownGitError(f"unsupported URL strategy: {strategy}")
    return base_url.rstrip("/") + suffix


def filename_for(slug: str, strategy: str, date_value: str) -> str:
    if strategy == "slug.md":
        return f"{slug}.md"
    if strategy == "date-slug.md":
        return f"{date_value[:10]}-{slug}.md"
    if strategy == "slug/index.md":
        return f"{slug}/index.md"
    raise MarkdownGitError(f"unsupported filename strategy: {strategy}")


def git(repo: Path, *args: str, timeout: float = 60.0, check: bool = True) -> str | None:
    if shutil.which("git") is None:
        raise MarkdownGitError("git executable is unavailable")
    result = subprocess.run(["git", *args], cwd=str(repo), shell=False, text=True, capture_output=True, timeout=timeout, check=False)
    output = (result.stdout or "").strip()
    if result.returncode and check:
        detail = (result.stderr or output).strip()[:2000]
        raise MarkdownGitError(f"git {' '.join(args[:2])} failed: {detail}")
    return output if result.returncode == 0 else None


def git_bytes(repo: Path, *args: str, timeout: float = 60.0, check: bool = True) -> bytes | None:
    result = subprocess.run(["git", *args], cwd=str(repo), shell=False, capture_output=True, timeout=timeout, check=False)
    if result.returncode and check:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()[:2000]
        raise MarkdownGitError(f"git {' '.join(args[:2])} failed: {detail}")
    return result.stdout if result.returncode == 0 else None


def find_complete_commit(repo: Path, manifest: list[dict[str, Any]]) -> str | None:
    """Find a commit whose tree proves every expected publication file hash."""
    paths = [item["target"] for item in manifest]
    candidates = (git(repo, "log", "--all", "--format=%H", "--", *paths, check=False) or "").splitlines()
    expected = {item["target"]: item["expected_hash"] for item in manifest}
    for candidate in candidates:
        names = set((git(repo, "show", "--format=", "--name-only", candidate, check=False) or "").splitlines())
        if not set(expected).issubset(names):
            continue
        if all(
            _sha256_bytes(git_bytes(repo, "show", f"{candidate}:{path}") or b"") == digest
            for path, digest in expected.items()
        ):
            return candidate
    return None


def validate_repository(config: GitConfig) -> None:
    repo = config.repository_path
    if not repo.is_dir() or not (repo / ".git").exists():
        raise MarkdownGitError(f"repository is missing or not a Git worktree: {repo}")
    if not shutil.which("git"):
        raise MarkdownGitError("git executable is unavailable")
    branch = git(repo, "branch", "--show-current") or ""
    if branch != config.default_branch:
        raise MarkdownGitError(f"repository is on branch {branch!r}, expected {config.default_branch!r}")
    if git(repo, "rev-parse", "--verify", f"refs/heads/{config.default_branch}", check=False) is None:
        raise MarkdownGitError(f"configured branch does not exist: {config.default_branch}")
    safe_relative(config.content_directory, label="content_directory")
    safe_relative(config.media_directory, label="media_directory")


def media_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("media", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise MarkdownGitError("media must be a list")
    items: list[dict[str, Any]] = []
    for entry in raw:
        item = {"path": entry} if isinstance(entry, str) else entry
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise MarkdownGitError("each media item requires a path")
        source = Path(item["path"]).expanduser()
        if not source.is_absolute():
            source = PROJECT_ROOT / source
        source = source.resolve()
        if source.suffix.lower() not in ALLOWED_MEDIA:
            raise MarkdownGitError(f"unsupported media type: {source.suffix}")
        if not source.is_file():
            raise MarkdownGitError(f"media file does not exist: {source}")
        header = source.read_bytes()[:16]
        valid_signature = {
            ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
            ".jpg": header.startswith(b"\xff\xd8\xff"),
            ".jpeg": header.startswith(b"\xff\xd8\xff"),
            ".gif": header.startswith((b"GIF87a", b"GIF89a")),
            ".webp": len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP",
            ".mp3": header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0),
            ".mp4": len(header) >= 8 and header[4:8] == b"ftyp",
            ".svg": b"<svg" in header.lower() or b"<?xml" in header.lower(),
        }.get(source.suffix.lower(), False)
        if not valid_signature:
            raise MarkdownGitError(f"media signature does not match extension: {source.name}")
        items.append({**item, "path": source})
    return items


def build_publication(config: GitConfig, payload: dict[str, Any], event_id: int) -> dict[str, Any]:
    document, body = read_content(payload)
    title = str(document.get("title", document.get("topic", ""))).strip()
    if not title:
        heading = re.match(r"^#\s+(.+)$", body, re.MULTILINE)
        title = heading.group(1).strip() if heading else ""
    if not title:
        raise MarkdownGitError("payload requires a non-empty title or topic")
    raw_slug = document.get("slug")
    if raw_slug is not None and (not isinstance(raw_slug, str) or Path(raw_slug).is_absolute() or any(part in {"..", "~"} for part in Path(raw_slug).parts) or "/" in raw_slug or "\\" in raw_slug):
        raise MarkdownGitError("slug must be a single safe path component")
    slug = slugify(str(raw_slug if raw_slug is not None else title))
    date_value = str(document.get("published_at", datetime.now(timezone.utc).date().isoformat()))
    relative_name = filename_for(slug, config.filename_strategy, date_value)
    relative_markdown = str(safe_relative(posixpath.join(config.content_directory, relative_name), label="target file").as_posix())
    markdown_path = under(config.repository_path, relative_markdown, label="target file")
    media_actions: list[dict[str, Any]] = []
    rendered_body = body.rstrip() + "\n"
    for item in media_items(payload):
        source: Path = item["path"]
        raw_name = str(item.get("filename", source.name))
        name = Path(raw_name).name
        if name in {"", ".", ".."} or name != raw_name or "/" in raw_name or "\\" in raw_name:
            raise MarkdownGitError("media filename is unsafe")
        relative_media = safe_relative(posixpath.join(config.media_directory, name), label="media target")
        target = under(config.repository_path, relative_media.as_posix(), label="media target")
        link = posixpath.relpath(relative_media.as_posix(), Path(relative_markdown).parent.as_posix() or ".")
        alt = str(item.get("alt", title))
        if source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}:
            rendered_body += f"\n![{alt}]({link})\n"
        else:
            rendered_body += f"\n[{name}]({link})\n"
        media_actions.append({"source": str(source), "target": relative_media.as_posix(), "path": target})
    markdown = render_frontmatter(document, title=title, slug=slug, published_at=date_value, base_url=config.base_url, url_strategy=config.url_strategy) + rendered_body
    return {"title": title, "slug": slug, "date": date_value, "relative_path": relative_markdown, "path": markdown_path, "markdown": markdown, "media": media_actions, "canonical_url": canonical_url(config.base_url, slug, config.url_strategy) if config.base_url else None}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def publication_manifest(publication: dict[str, Any]) -> list[dict[str, Any]]:
    """Classify every publication target before any filesystem mutation."""
    desired: list[tuple[str, bytes, Path | None]] = [
        (publication["relative_path"], publication["markdown"].encode("utf-8"), publication["path"])
    ]
    desired.extend(
        (item["target"], Path(item["source"]).read_bytes(), item["path"])
        for item in publication["media"]
    )
    manifest: list[dict[str, Any]] = []
    for relative_path, content, target in desired:
        existing_hash = _sha256(target) if target is not None and target.is_file() else None
        expected_hash = _sha256_bytes(content)
        state = "UNCHANGED" if existing_hash == expected_hash else ("CHANGED" if existing_hash is not None else "MISSING")
        manifest.append({"target": relative_path, "state": state, "expected_hash": expected_hash, "existing_hash": existing_hash})
    return manifest


def _detail(publication: dict[str, Any], config: GitConfig, *, commit_sha: str | None, commit_requested: bool, push_requested: bool, manifest: list[dict[str, Any]]) -> str:
    return json.dumps({"repository": str(config.repository_path), "relative_path": publication["relative_path"], "content_hash": hashlib.sha256(publication["markdown"].encode()).hexdigest(), "files": {item["target"]: item["expected_hash"] for item in manifest}, "changed_files": [item["target"] for item in manifest if item["state"] != "UNCHANGED"], "commit_sha": commit_sha, "commit_requested": commit_requested, "push_requested": push_requested, "branch": config.default_branch, "remote": config.git_remote, "canonical_url": publication.get("canonical_url")}, ensure_ascii=False, sort_keys=True)


def register_plugin(connection, *, enabled: bool | None = None) -> None:
    active = int(True if enabled is None else enabled)
    with connection:
        connection.execute("""INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active) VALUES (?,?,?,?,?) ON CONFLICT(plugin_name) DO UPDATE SET type=excluded.type, executable_path=excluded.executable_path, icon=excluded.icon""", (PLUGIN_NAME, PLUGIN_TYPE, str(Path(__file__).resolve()), PLUGIN_ICON, active))
        connection.execute("""INSERT INTO event_routes(event_type,target_plugin_name) VALUES (?,?) ON CONFLICT(event_type) DO UPDATE SET target_plugin_name=excluded.target_plugin_name""", (EVENT_TYPE, PLUGIN_NAME))


def _load_event(connection, event_id: int) -> dict[str, Any]:
    row = connection.execute("SELECT event_type,payload FROM events_queue WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise LookupError(f"event {event_id} does not exist")
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise MarkdownGitError("event payload must be a JSON object")
    return payload


def publish_event(database: Path, event_id: int, *, config_path: Path | None = None, dry_run: bool = False, commit: bool | None = None, push: bool | None = None) -> tuple[str, dict[str, Any], dict[str, Any]]:
    with connect_database(database) as connection:
        payload = _load_event(connection, event_id)
        config = load_config(config_path, payload)
    dry_run = dry_run or _bool(payload.get("dry_run"), False) or str(payload.get("mode", "")).upper() == "SIMULATED"
    if not config.enabled:
        raise MarkdownGitError("Markdown Git publisher is disabled")
    publication = build_publication(config, payload, event_id)
    commit_requested = config.commit_enabled if commit is None else commit
    push_requested = config.push_enabled if push is None else push
    if push_requested and not commit_requested:
        raise MarkdownGitError("push requires commit mode")
    preview = {"target_file": publication["relative_path"], "slug": publication["slug"], "frontmatter": publication["markdown"].split("---", 2)[1].strip(), "markdown": publication["markdown"], "media_actions": [{key: value for key, value in item.items() if key != "path"} for item in publication["media"]], "git_actions": ["git add -- " + publication["relative_path"]] + (["git commit"] if commit_requested else []) + ([f"git push {config.git_remote} {config.default_branch}"] if push_requested else [])}
    if dry_run:
        validate_repository(config)
        return "SIMULATED", {"preview": preview, "relative_path": publication["relative_path"]}, {}
    validate_repository(config)
    manifest = publication_manifest(publication)
    expected_paths = [item["target"] for item in manifest]
    changed_paths = [item["target"] for item in manifest if item["state"] != "UNCHANGED"]
    with connect_database(database) as connection:
        ledger = begin_submission(connection, event_id=event_id, channel=EVENT_TYPE, payload=payload, target=f"{config.repository_path}:{publication['relative_path']}")
        if ledger["status"] == "CONFIRMED":
            evidence = json.loads(ledger["detail"] or "{}") if ledger["detail"] else {}
            return "COMPLETED", {"idempotent": True, "relative_path": publication["relative_path"], "commit_sha": evidence.get("commit_sha"), "canonical_url": evidence.get("canonical_url")}, {}
        if ledger["status"] == "NEEDS_OPERATOR":
            raise MarkdownGitError("publication attempt requires operator reconciliation before retry")

    if not changed_paths:
        evidence_commit = find_complete_commit(config.repository_path, manifest) if commit_requested else None
        if commit_requested and evidence_commit is None:
            detail = _detail(publication, config, commit_sha=None, commit_requested=commit_requested, push_requested=push_requested, manifest=manifest)
            with connect_database(database) as connection:
                update_publication(connection, event_id, EVENT_TYPE, "UNKNOWN", detail=detail)
            return "UNKNOWN", {"relative_path": publication["relative_path"], "reason": "complete publication commit evidence not found"}, {"markdown_git": {"manifest": {item["target"]: item["expected_hash"] for item in manifest}}}
        detail = _detail(publication, config, commit_sha=evidence_commit, commit_requested=commit_requested, push_requested=push_requested, manifest=manifest)
        with connect_database(database) as connection:
            update_publication(connection, event_id, EVENT_TYPE, "CONFIRMED", platform_id=evidence_commit, platform_url=publication.get("canonical_url"), detail=detail)
        return "COMPLETED", {"idempotent": True, "relative_path": publication["relative_path"], "commit_sha": evidence_commit, "canonical_url": publication.get("canonical_url"), "manifest": {item["target"]: item["expected_hash"] for item in manifest}}, {"markdown_git": {"relative_path": publication["relative_path"], "commit_sha": evidence_commit, "manifest": {item["target"]: item["expected_hash"] for item in manifest}}}

    existing_status = git(config.repository_path, "status", "--porcelain", "--", *expected_paths) or ""
    if existing_status.strip():
        raise MarkdownGitError("publication target has unrelated uncommitted changes")
    staged_before = git(config.repository_path, "diff", "--cached", "--name-only") or ""
    if staged_before.strip():
        raise MarkdownGitError("repository has pre-staged changes; publish after resolving them")
    try:
        if publication["relative_path"] in changed_paths:
            publication["path"].parent.mkdir(parents=True, exist_ok=True)
            publication["path"].write_bytes(publication["markdown"].encode("utf-8"))
        for item in publication["media"]:
            if item["target"] in changed_paths:
                target: Path = item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.resolve() != Path(item["source"]).resolve():
                    shutil.copy2(item["source"], target)
        after_manifest = publication_manifest(publication)
        expected_hashes = {item["target"]: item["expected_hash"] for item in manifest}
        actual_hashes = {item["target"]: item["expected_hash"] for item in after_manifest}
        if actual_hashes != expected_hashes or any(item["state"] != "UNCHANGED" for item in after_manifest):
            raise MarkdownGitError("publication file set does not match its pre-write manifest")
        commit_sha: str | None = None
        if commit_requested:
            git(config.repository_path, "add", "--", *expected_paths)
            staged = set((git(config.repository_path, "diff", "--cached", "--name-only") or "").splitlines())
            if staged != set(changed_paths):
                raise MarkdownGitError("staged file set differs from this publication")
            git(config.repository_path, "-c", f"user.name={config.author_name}", "-c", f"user.email={config.author_email}", "commit", "-m", f"Publish: {publication['title']}", "--", *changed_paths)
            commit_sha = git(config.repository_path, "rev-parse", "HEAD")
            if not commit_sha:
                raise MarkdownGitError("Git commit SHA could not be resolved")
            remaining = git(config.repository_path, "status", "--porcelain", "--", *expected_paths) or ""
            if remaining.strip():
                raise MarkdownGitError("publication paths remain modified after commit")
        detail = _detail(publication, config, commit_sha=commit_sha, commit_requested=commit_requested, push_requested=push_requested, manifest=manifest)
        if push_requested:
            try:
                git(config.repository_path, "push", config.git_remote, f"HEAD:{config.default_branch}")
            except Exception:
                with connect_database(database) as connection:
                    update_publication(connection, event_id, EVENT_TYPE, "UNKNOWN", platform_id=commit_sha, platform_url=publication.get("canonical_url"), detail=detail)
                return "UNKNOWN", {"relative_path": publication["relative_path"], "commit_sha": commit_sha, "push": "unknown"}, {"markdown_git": {"relative_path": publication["relative_path"], "commit_sha": commit_sha}}
        with connect_database(database) as connection:
            update_publication(connection, event_id, EVENT_TYPE, "CONFIRMED", platform_id=commit_sha, platform_url=publication.get("canonical_url"), detail=detail)
        return "COMPLETED", {"relative_path": publication["relative_path"], "commit_sha": commit_sha, "canonical_url": publication.get("canonical_url"), "media": [item["target"] for item in publication["media"]], "manifest": {item["target"]: item["expected_hash"] for item in manifest}}, {"markdown_git": {"relative_path": publication["relative_path"], "commit_sha": commit_sha, "canonical_url": publication.get("canonical_url"), "manifest": {item["target"]: item["expected_hash"] for item in manifest}}}
    except Exception as exc:
        with connect_database(database) as connection:
            update_publication(connection, event_id, EVENT_TYPE, "UNKNOWN", detail=str(exc)[:8000])
        raise


def reconcile_markdown_attempt(attempt: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Reconcile file/commit evidence without publishing or changing Git."""
    try:
        detail = json.loads(attempt.get("detail") or "{}")
        repository = Path(detail["repository"]).resolve()
        target = under(repository, str(detail["relative_path"]), label="ledger target")
        expected_hash = str(detail["content_hash"])
        files = detail.get("files")
        if not isinstance(files, dict):
            files = {str(detail["relative_path"]): expected_hash}
        for relative_path, digest in files.items():
            path = under(repository, str(relative_path), label="ledger target")
            if not path.is_file():
                if detail.get("commit_sha"):
                    return "NEEDS_OPERATOR", {"detail": f"expected publication file is absent: {relative_path}"}
                return "FAILED", {"detail": f"publication file is absent: {relative_path}"}
            if _sha256(path) != str(digest):
                return "NEEDS_OPERATOR", {"detail": f"publication file hash differs from ledger: {relative_path}"}
        if detail.get("commit_sha"):
            commit = git(repository, "cat-file", "-e", f"{detail['commit_sha']}^{{commit}}", check=False)
            if commit is None:
                return "NEEDS_OPERATOR", {"detail": "recorded commit cannot be found locally"}
            committed = git(repository, "show", "--format=", "--name-only", str(detail["commit_sha"])) or ""
            changed_files = set(detail.get("changed_files", files.keys()))
            if not changed_files.issubset(set(committed.splitlines())):
                return "NEEDS_OPERATOR", {"detail": "recorded commit does not contain all changed publication files"}
        if detail.get("push_requested"):
            return "NEEDS_OPERATOR", {"detail": "local publication is proven; remote push outcome requires operator verification"}
        return "CONFIRMED", {"detail": "target file and local commit evidence verified"}
    except Exception as exc:
        return "NEEDS_OPERATOR", {"detail": str(exc)[:2000]}


class MarkdownGitReconciler:
    def reconcile(self, attempt: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return reconcile_markdown_attempt(attempt)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--event_id", type=int)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--commit", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--push", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    if not args.register and args.event_id is None:
        parser.error("choose --register or --event_id")
    return args


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    args = parse_args()
    database = args.db.expanduser().resolve()
    try:
        with connect_database(database) as connection:
            if args.register:
                cfg = load_config(args.config)
                register_plugin(connection, enabled=cfg.enabled)
        if args.event_id is not None:
            outcome, result, patch = publish_event(database, args.event_id, config_path=args.config, dry_run=args.dry_run, commit=args.commit, push=args.push)
            emit_result(outcome, result=result, payload_patch=patch)
        return 0
    except Exception as exc:
        LOGGER.exception("Markdown Git publication failed")
        if args.event_id is not None:
            emit_result("UNKNOWN", error=str(exc)[:8000], retryable=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
