#!/usr/bin/env python3
"""Offline self-test for local vector retrieval and the editorial role gate."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.editorial_loop import EditorialLoopError, run_editorial_loop  # noqa: E402
from core.rag_index import refresh_index, search  # noqa: E402


def mock_agent(role: str, _prompt: str) -> str:
    if role == "SCHRIJVER":
        return "# Soevereine AI\n\nLokale modellen houden kennis onder eigen beheer."
    if role == "FACTCHECKER":
        return "# Soevereine AI\n\nLokale modellen houden kennis onder eigen beheer.\n\nFACTCHECK_STATUS: APPROVED"
    if role == "REDACTEUR":
        return "# Soevereine AI\n\nLokale modellen houden kennis veilig onder eigen beheer."
    raise AssertionError(role)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rag_editorial_test_") as temporary:
        root = Path(temporary)
        notes = root / "research"
        notes.mkdir()
        (notes / "soeverein.md").write_text(
            "# Lokale AI\n\nSoevereine AI verwerkt private kennis lokaal en beperkt datalekken.",
            encoding="utf-8",
        )
        database = root / "rag.db"
        indexed, skipped, chunks = refresh_index(database, [notes])
        results = search(database, "soevereine private AI kennis", limit=3)
        assert indexed == 1 and skipped == 0 and chunks >= 1
        assert results and results[0].source_path.endswith("soeverein.md")
        editorial = run_editorial_loop(
            "Schrijf over lokale AI.", results[0].content, mock_agent
        )
        assert editorial.roles == ("SCHRIJVER", "FACTCHECKER", "REDACTEUR")
        assert "veilig onder eigen beheer" in editorial.final_text
        try:
            run_editorial_loop(
                "Onveilige claim",
                results[0].content,
                lambda role, prompt: (
                    "FACTCHECK_STATUS: REJECTED" if role == "FACTCHECKER" else mock_agent(role, prompt)
                ),
            )
        except EditorialLoopError:
            rejected = True
        else:
            rejected = False
        assert rejected
        print(f"TEST_OK rag indexed={indexed} chunks={chunks} top_score={results[0].score:.4f}")
        print(f"TEST_OK source={results[0].source_path}")
        print("TEST_OK editorial_roles=" + "->".join(editorial.roles))
        print("TEST_OK factcheck=APPROVED final_saved_gate=true")
        print("TEST_OK rejection_gate=BLOCKED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
