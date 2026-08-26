#!/usr/bin/env python3
"""Operational status dashboard for the Event-Driven AI OS."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE: Final = PROJECT_ROOT / "db" / "events.db"
REQUIRED_TABLES: Final = {
    "events_queue",
    "dead_letter_queue",
    "event_routes",
    "plugin_registry",
}


@dataclass(frozen=True)
class Palette:
    """ANSI styling palette, optionally disabled for redirected output."""

    reset: str = ""
    bold: str = ""
    dim: str = ""
    cyan: str = ""
    green: str = ""
    yellow: str = ""
    red: str = ""

    @classmethod
    def ansi(cls) -> "Palette":
        return cls(
            reset="\033[0m",
            bold="\033[1m",
            dim="\033[2m",
            cyan="\033[36m",
            green="\033[32m",
            yellow="\033[33m",
            red="\033[31m",
        )


def parse_args() -> argparse.Namespace:
    """Parse dashboard configuration."""
    parser = argparse.ArgumentParser(
        description="Show Event-Driven AI OS health and queue status."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum recent queue/dead-letter rows to show (default: 10).",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors.",
    )
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 100:
        parser.error("--limit must be between 1 and 100")
    return args


def connect(database: Path) -> sqlite3.Connection:
    """Open the database in read-only mode without creating missing files."""
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def validate_schema(connection: sqlite3.Connection) -> None:
    """Fail with a useful message when the database is not initialized."""
    present = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = REQUIRED_TABLES - present
    if missing:
        raise RuntimeError("missing database tables: " + ", ".join(sorted(missing)))


def visible_width(value: object) -> int:
    """Return a practical terminal width for Unicode text."""
    return len(str(value))


def shorten(value: object, width: int) -> str:
    """Render a bounded single-line cell."""
    text = "—" if value is None or value == "" else str(value)
    text = " ".join(text.splitlines())
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def render_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    maximum_widths: Sequence[int] | None = None,
) -> str:
    """Render a compact Unicode table without third-party packages."""
    materialized = [[str(cell) if cell is not None else "—" for cell in row] for row in rows]
    if not materialized:
        return "  Geen resultaten."
    limits = list(maximum_widths or [40] * len(headers))
    widths = []
    for index, header in enumerate(headers):
        column_values = [row[index] for row in materialized]
        natural = max([visible_width(header), *(visible_width(v) for v in column_values)])
        widths.append(min(natural, limits[index]))

    def row_line(values: Sequence[object]) -> str:
        cells = [shorten(value, widths[i]).ljust(widths[i]) for i, value in enumerate(values)]
        return "│ " + " │ ".join(cells) + " │"

    top = "┌─" + "─┬─".join("─" * width for width in widths) + "─┐"
    divider = "├─" + "─┼─".join("─" * width for width in widths) + "─┤"
    bottom = "└─" + "─┴─".join("─" * width for width in widths) + "─┘"
    lines = [top, row_line(headers), divider]
    lines.extend(row_line(row) for row in materialized)
    lines.append(bottom)
    return "\n".join(lines)


def status_label(status: str, palette: Palette) -> str:
    """Color a queue status according to operational severity."""
    colors = {
        "COMPLETED": palette.green,
        "PENDING": palette.yellow,
        "PROCESSING": palette.cyan,
        "FAILED": palette.red,
    }
    return f"{colors.get(status, '')}{status}{palette.reset}"


def print_dashboard(
    connection: sqlite3.Connection,
    database: Path,
    limit: int,
    palette: Palette,
) -> None:
    """Query and print the complete operational dashboard."""
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    queue_counts = {
        row["status"]: row["amount"]
        for row in connection.execute(
            "SELECT status, COUNT(*) AS amount FROM events_queue GROUP BY status"
        )
    }
    dead_count = connection.execute(
        "SELECT COUNT(*) FROM dead_letter_queue"
    ).fetchone()[0]
    plugin_count = connection.execute(
        "SELECT COUNT(*) FROM plugin_registry WHERE is_active = 1"
    ).fetchone()[0]
    route_count = connection.execute("SELECT COUNT(*) FROM event_routes").fetchone()[0]

    print(f"{palette.bold}{palette.cyan}EVENT-DRIVEN AI OS · SYSTEM STATUS{palette.reset}")
    print(f"{palette.dim}{datetime.now().astimezone().isoformat(timespec='seconds')}{palette.reset}")
    print(f"Database : {database}")
    health_color = palette.green if integrity == "ok" else palette.red
    print(f"Integriteit: {health_color}{integrity.upper()}{palette.reset}\n")

    summary_rows = [
        ("Actieve plugins", plugin_count),
        ("Event-routes", route_count),
        ("Pending", queue_counts.get("PENDING", 0)),
        ("Processing", queue_counts.get("PROCESSING", 0)),
        ("Completed", queue_counts.get("COMPLETED", 0)),
        ("Failed (actief)", queue_counts.get("FAILED", 0)),
        ("Dead letters", dead_count),
    ]
    print(f"{palette.bold}OVERZICHT{palette.reset}")
    print(render_table(("Metriek", "Aantal"), summary_rows, (24, 10)))

    plugins = connection.execute(
        """
        SELECT icon, plugin_name, type,
               CASE is_active WHEN 1 THEN 'ACTIEF' ELSE 'INACTIEF' END AS state,
               executable_path
          FROM plugin_registry
         ORDER BY is_active DESC, type, plugin_name
        """
    ).fetchall()
    print(f"\n{palette.bold}PLUGINS{palette.reset}")
    print(
        render_table(
            ("Icoon", "Naam", "Type", "Status", "Executable"),
            [tuple(row) for row in plugins],
            (5, 32, 10, 10, 52),
        )
    )

    routes = connection.execute(
        """
        SELECT er.event_type, er.target_plugin_name,
               CASE WHEN pr.is_active = 1 THEN 'GEREED' ELSE 'ONGELDIG' END
          FROM event_routes AS er
          LEFT JOIN plugin_registry AS pr
            ON pr.plugin_name = er.target_plugin_name
         ORDER BY er.event_type
        """
    ).fetchall()
    print(f"\n{palette.bold}EVENT-ROUTES{palette.reset}")
    print(
        render_table(
            ("Event type", "Doelplugin", "Status"),
            [tuple(row) for row in routes],
            (30, 36, 10),
        )
    )

    events = connection.execute(
        """
        SELECT id, event_type, status, retry_count, created_at, error_log
          FROM events_queue
         ORDER BY id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    print(f"\n{palette.bold}RECENTE EVENTS{palette.reset}")
    print(
        render_table(
            ("ID", "Type", "Status", "Retries", "Aangemaakt", "Fout"),
            [
                (
                    row["id"],
                    row["event_type"],
                    status_label(row["status"], palette),
                    row["retry_count"],
                    row["created_at"],
                    row["error_log"],
                )
                for row in events
            ],
            (8, 30, 20, 8, 20, 42),
        )
    )

    dead_letters = connection.execute(
        """
        SELECT id, event_type, retry_count, created_at, reason_for_death
          FROM dead_letter_queue
         ORDER BY id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    print(f"\n{palette.bold}DEAD-LETTER QUEUE{palette.reset}")
    print(
        render_table(
            ("ID", "Type", "Retries", "Aangemaakt", "Reden"),
            [tuple(row) for row in dead_letters],
            (8, 30, 8, 20, 56),
        )
    )


def main() -> int:
    """Validate the database and display its current operational state."""
    args = parse_args()
    database = args.db.expanduser().resolve()
    palette = Palette() if args.no_color or not sys.stdout.isatty() else Palette.ansi()

    if not database.is_file():
        print(f"FOUT: database niet gevonden: {database}", file=sys.stderr)
        return 2
    try:
        with connect(database) as connection:
            validate_schema(connection)
            print_dashboard(connection, database, args.limit, palette)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        print(f"FOUT: dashboard kon niet worden opgebouwd: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
