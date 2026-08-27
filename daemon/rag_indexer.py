#!/usr/bin/env python3
"""Incrementally maintain the vault RAG index outside the LLM request path."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.rag_index import DEFAULT_DATABASE, DEFAULT_ROOTS, refresh_index

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover
    FileSystemEventHandler = object  # type: ignore[assignment]
    Observer = None  # type: ignore[assignment]


LOGGER: Final = logging.getLogger("rag_indexer")


class MarkdownChangeHandler(FileSystemEventHandler):
    """Coalesce Markdown filesystem events into a single refresh signal."""

    def __init__(self, changed: threading.Event) -> None:
        self.changed = changed

    def on_any_event(self, event: Any) -> None:
        paths = (getattr(event, "src_path", ""), getattr(event, "dest_path", ""))
        if any(str(path).lower().endswith(".md") for path in paths):
            self.changed.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Maintain the local vault RAG index.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--root", action="append", type=Path, dest="roots")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--debounce", type=float, default=0.75)
    parser.add_argument("--chunk-tokens", type=int, default=320)
    parser.add_argument("--overlap-tokens", type=int, default=32)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval <= 0 or args.debounce < 0:
        parser.error("interval must be positive and debounce non-negative")
    if args.chunk_tokens < 32 or not 0 <= args.overlap_tokens < args.chunk_tokens:
        parser.error("invalid chunk/overlap token budget")
    return args


def run_refresh(database: Path, roots: list[Path], chunk_tokens: int, overlap_tokens: int) -> tuple[int, int, int]:
    result = refresh_index(
        database, roots, maximum_chunk_tokens=chunk_tokens,
        overlap_tokens=overlap_tokens,
    )
    LOGGER.info("RAG refresh indexed=%s unchanged=%s chunks=%s", *result)
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    roots = [path.expanduser().resolve() for path in (args.roots or DEFAULT_ROOTS)]
    stop = threading.Event()
    changed = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    observer = None
    try:
        run_refresh(args.db, roots, args.chunk_tokens, args.overlap_tokens)
        if args.once:
            return 0
        if Observer is not None:
            observer = Observer()
            handler = MarkdownChangeHandler(changed)
            watched: set[Path] = set()
            for root in roots:
                directory = root if root.is_dir() else root.parent
                if directory.exists() and directory not in watched:
                    observer.schedule(handler, str(directory), recursive=True)
                    watched.add(directory)
            observer.start()
            LOGGER.info("Watching %s RAG root(s) with watchdog", len(watched))
        else:
            LOGGER.warning("watchdog unavailable; periodic incremental reconciliation active")
        while not stop.wait(args.interval):
            if observer is None or changed.is_set():
                if changed.is_set() and args.debounce and stop.wait(args.debounce):
                    break
                changed.clear()
                run_refresh(args.db, roots, args.chunk_tokens, args.overlap_tokens)
        return 0
    except Exception:
        LOGGER.exception("RAG indexer terminated")
        return 1
    finally:
        if observer is not None:
            observer.stop()
            observer.join(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
