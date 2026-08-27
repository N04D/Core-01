#!/usr/bin/env python3
"""Unit tests for Markdown-driven subject configuration and cursor state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.md_subject_parser import load_subject_document, select_next_subject
from core.setup_database import initialize_database


class MarkdownSubjectParserTests(unittest.TestCase):
    def test_rules_and_subjects_are_loaded_from_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "architecture.md"
            config.write_text(
                "---\nstyle: brutalist\npalette: monochrome\nlighting: overcast\n"
                "rules:\n  - no people\n  - no logos\n---\n# Backlog\n"
                "1. Transit hub | concrete and glass\n2. Solar tower | geometric facade\n",
                encoding="utf-8",
            )
            document = load_subject_document(config)
            self.assertEqual(document.style, "brutalist")
            self.assertEqual(document.rules, ("no people", "no logos"))
            self.assertEqual([item.title for item in document.subjects], ["Transit hub", "Solar tower"])
            self.assertIn("no people", document.prompt_constraints())

    def test_cursor_advances_and_wraps_transactionally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "subjects.md"
            config.write_text("---\nstyle: minimal\n---\n1. Alpha\n2. Beta\n", encoding="utf-8")
            database = root / "events.db"
            initialize_database(database)
            document = load_subject_document(config)
            self.assertEqual(select_next_subject(database, document, "test").title, "Alpha")
            self.assertEqual(select_next_subject(database, document, "test").title, "Beta")
            self.assertEqual(select_next_subject(database, document, "test").title, "Alpha")


if __name__ == "__main__":
    unittest.main(verbosity=2)
