#!/usr/bin/env python3
"""Merge legacy ``db/`` and ``vault/`` data into the runtime data root.

The migration is deliberately copy-only: existing files are never replaced or
deleted. Directory trees are merged recursively, making it safe to resume or
run repeatedly after a partial migration.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.paths import CORE_DATA, CORE_ROOT, ensure_runtime_dirs

MAPPING = {
    "db": "db",
    "vault/media": "media",
    "vault/analytics": "analytics",
    "vault/research": "research",
    "vault/concepten": "concepts",
    "vault/gepubliceerd": "published",
    "vault/logs": "logs",
}


def _merge_tree(source: Path, destination: Path, actions: list[str], *, dry_run: bool) -> None:
    """Recursively merge *source* into *destination* without overwrites."""
    if not source.exists():
        actions.append(f"MISSING source: {source}")
        return
    if not source.is_dir():
        if destination.exists():
            actions.append(f"SKIP exists: {destination}")
            return
        actions.append(f"COPY {source} -> {destination}")
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        return

    if destination.exists() and not destination.is_dir():
        actions.append(f"SKIP exists: {destination}")
        return
    if not destination.exists():
        actions.append(f"CREATE DIR: {destination}")
        if not dry_run:
            destination.mkdir(parents=True, exist_ok=True)

    for item in sorted(source.iterdir(), key=lambda path: path.name):
        target = destination / item.name
        if item.is_dir():
            _merge_tree(item, target, actions, dry_run=dry_run)
        elif target.exists():
            actions.append(f"SKIP exists: {target}")
        else:
            actions.append(f"COPY {item} -> {target}")
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)


def migrate(source_root: Path, destination: Path, *, dry_run: bool = False) -> list[str]:
    """Merge all legacy mappings and return a deterministic action log."""
    source_root = source_root.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if source_root == destination:
        raise ValueError("source and destination must be different directories")
    actions: list[str] = []
    if not destination.exists():
        actions.append(f"CREATE DIR: {destination}")
        if not dry_run:
            destination.mkdir(parents=True, exist_ok=True)
    for source_name, target_name in MAPPING.items():
        _merge_tree(source_root / source_name, destination / target_name, actions, dry_run=dry_run)
    if not dry_run:
        ensure_runtime_dirs()
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=CORE_ROOT)
    parser.add_argument("--destination", type=Path, default=CORE_DATA)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for action in migrate(args.source, args.destination, dry_run=args.dry_run):
        print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
