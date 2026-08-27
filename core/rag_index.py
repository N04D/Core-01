#!/usr/bin/env python3
"""Dependency-free local vector index for Markdown knowledge retrieval."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Iterable


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
DEFAULT_ROOTS: Final = (PROJECT_ROOT / "vault" / "research", PROJECT_ROOT / "vault")
VECTOR_DIMENSIONS: Final = 384
TOKEN_PATTERN: Final = re.compile(r"[\wÀ-ÿ]{2,}", re.UNICODE)
LOGGER: Final = logging.getLogger("rag_index")


@dataclass(frozen=True)
class SearchResult:
    source_path: str
    chunk_index: int
    content: str
    score: float


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.expanduser().resolve(), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS rag_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_path TEXT NOT NULL UNIQUE,
            content_hash TEXT NOT NULL, modified_at TEXT NOT NULL,
            indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS rag_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL, content TEXT NOT NULL,
            vector JSON NOT NULL CHECK (json_valid(vector)), token_count INTEGER NOT NULL,
            UNIQUE(document_id,chunk_index)
        );
        """
    )


def tokens(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN_PATTERN.findall(text)]


def vectorize(text: str) -> list[float]:
    """Build a normalized signed feature-hashing vector."""
    vector = [0.0] * VECTOR_DIMENSIONS
    for token in tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % VECTOR_DIMENSIONS
        vector[index] += -1.0 if value & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def chunk_markdown(text: str, maximum: int = 1600, overlap: int = 180) -> list[str]:
    """Split Markdown on paragraph boundaries with bounded character overlap."""
    if maximum < 200 or overlap < 0 or overlap >= maximum:
        raise ValueError("invalid chunk dimensions")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        pieces = [paragraph[index:index + maximum] for index in range(0, len(paragraph), maximum)] or [paragraph]
        for piece in pieces:
            candidate = f"{current}\n\n{piece}".strip()
            if current and len(candidate) > maximum:
                chunks.append(current)
                current = f"{current[-overlap:]}\n\n{piece}".strip() if overlap else piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def markdown_paths(roots: Iterable[Path]) -> list[Path]:
    paths: set[Path] = set()
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved.is_file() and resolved.suffix.lower() == ".md":
            paths.add(resolved)
        elif resolved.is_dir():
            paths.update(path.resolve() for path in resolved.rglob("*.md") if path.is_file())
    return sorted(paths)


def refresh_index(database: Path, roots: Iterable[Path] = DEFAULT_ROOTS) -> tuple[int, int, int]:
    """Incrementally index changed files and remove entries no longer in scope."""
    paths = markdown_paths(roots)
    canonical = {str(path) for path in paths}
    indexed = skipped = chunks_written = 0
    with connect(database) as connection:
        ensure_schema(connection)
        existing = {row["source_path"]: row for row in connection.execute("SELECT id,source_path,content_hash FROM rag_documents")}
        for path in paths:
            content = path.read_text(encoding="utf-8", errors="replace")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            prior = existing.get(str(path))
            if prior and prior["content_hash"] == digest:
                skipped += 1
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            with connection:
                connection.execute(
                    """INSERT INTO rag_documents(source_path,content_hash,modified_at,indexed_at)
                       VALUES (?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(source_path) DO UPDATE SET
                       content_hash=excluded.content_hash,modified_at=excluded.modified_at,indexed_at=CURRENT_TIMESTAMP""",
                    (str(path), digest, modified),
                )
                document_id = connection.execute("SELECT id FROM rag_documents WHERE source_path=?", (str(path),)).fetchone()[0]
                connection.execute("DELETE FROM rag_chunks WHERE document_id=?", (document_id,))
                for index, chunk in enumerate(chunk_markdown(content)):
                    connection.execute(
                        "INSERT INTO rag_chunks(document_id,chunk_index,content,vector,token_count) VALUES (?,?,?,?,?)",
                        (document_id, index, chunk, json.dumps(vectorize(chunk)), len(tokens(chunk))),
                    )
                    chunks_written += 1
            indexed += 1
        stale = [row["id"] for source, row in existing.items() if source not in canonical]
        if stale:
            with connection:
                connection.executemany("DELETE FROM rag_documents WHERE id=?", [(item,) for item in stale])
    return indexed, skipped, chunks_written


def search(database: Path, query: str, limit: int = 5, minimum_score: float = 0.05) -> list[SearchResult]:
    if not query.strip() or limit < 1:
        return []
    query_vector = vectorize(query)
    results: list[SearchResult] = []
    with connect(database) as connection:
        ensure_schema(connection)
        rows = connection.execute(
            """SELECT rd.source_path,rc.chunk_index,rc.content,rc.vector
                 FROM rag_chunks rc JOIN rag_documents rd ON rd.id=rc.document_id"""
        ).fetchall()
    for row in rows:
        stored = json.loads(row["vector"])
        score = sum(left * right for left, right in zip(query_vector, stored))
        if score >= minimum_score:
            results.append(SearchResult(row["source_path"], row["chunk_index"], row["content"], score))
    return sorted(results, key=lambda item: item.score, reverse=True)[:limit]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index or query the local Obsidian RAG database.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--root", action="append", type=Path, dest="roots")
    parser.add_argument("--query")
    parser.add_argument("--limit", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    try:
        indexed, skipped, chunks = refresh_index(args.db, args.roots or DEFAULT_ROOTS)
        LOGGER.info("RAG index ready: indexed=%s unchanged=%s chunks_written=%s", indexed, skipped, chunks)
        if args.query:
            for position, result in enumerate(search(args.db, args.query, args.limit), 1):
                print(f"{position}. score={result.score:.4f} source={result.source_path} chunk={result.chunk_index}")
                print(result.content[:400].replace("\n", " "))
        return 0
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
        LOGGER.exception("RAG indexing failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
