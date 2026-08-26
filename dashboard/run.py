#!/usr/bin/env python3
"""Development/production entrypoint for the local dashboard."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from app import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local AI OS dashboard.")
    parser.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8080"))
    )
    parser.add_argument("--db", type=Path)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = create_app(args.db)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()

