#!/usr/bin/env python3
"""Explicit playbook entrypoint for a single Google Drive folder sync."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins.io.gdrive_sync import DEFAULT_AUTH, DEFAULT_DB, DEFAULT_STAGING, sync_once


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH)
    parser.add_argument("--folder-id", default=os.getenv("GDRIVE_TARGET_FOLDER_ID"))
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.folder_id:
        print(json.dumps({"status": "FAILED", "error": "GDRIVE_TARGET_FOLDER_ID is required; no Drive-wide sync is permitted"}))
        return 1
    try:
        result = sync_once(args.db.expanduser().resolve(), args.auth, args.folder_id, args.staging_dir, args.dry_run)
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED_AUTH" if "auth" in str(exc).lower() else "FAILED", "error": str(exc)}))
        return 1
    print(json.dumps({"status": "COMPLETED", "folder_id": args.folder_id, "files": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
