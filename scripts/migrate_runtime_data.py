#!/usr/bin/env python3
"""Non-destructive migration from the legacy vault/db layout to CORE_DATA."""
from __future__ import annotations
import argparse
import shutil
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.paths import CORE_DATA, CORE_ROOT, ensure_runtime_dirs

MAPPING = {
    "db": "db", "vault/media": "media", "vault/analytics": "analytics",
    "vault/research": "research", "vault/concepten": "concepts",
    "vault/gepubliceerd": "published", "vault/logs": "logs",
}

def migrate(source_root: Path, destination: Path, *, dry_run: bool = False) -> list[str]:
    destination.mkdir(parents=True, exist_ok=True)
    actions: list[str] = []
    for source_name, target_name in MAPPING.items():
        source = source_root / source_name
        target = destination / target_name
        if not source.exists():
            continue
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            dest = target / item.name
            if dest.exists():
                actions.append(f"SKIP exists: {dest}")
                continue
            actions.append(f"COPY {item} -> {dest}")
            if not dry_run:
                if item.is_dir(): shutil.copytree(item, dest)
                else: shutil.copy2(item, dest)
    if not dry_run: ensure_runtime_dirs()
    return actions

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=CORE_ROOT)
    p.add_argument("--destination", type=Path, default=CORE_DATA)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    for action in migrate(args.source.resolve(), args.destination.expanduser().resolve(), dry_run=args.dry_run):
        print(action)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
