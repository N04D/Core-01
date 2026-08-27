#!/usr/bin/env python3
"""Parse Markdown generator backlogs and provide a transactional cursor."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from core.database import connect_database
from core.markdown_parser import MarkdownTemplateError, load_template


@dataclass(frozen=True)
class Subject:
    sequence: int
    key: str
    title: str
    context: str = ""


@dataclass(frozen=True)
class SubjectDocument:
    path: Path
    rules: tuple[str, ...]
    style: str
    palette: str
    lighting: str
    subjects: tuple[Subject, ...]

    def prompt_constraints(self) -> str:
        rules = "; ".join(self.rules)
        fields = [f"Style: {self.style}" if self.style else "", f"Palette: {self.palette}" if self.palette else "",
                  f"Lighting: {self.lighting}" if self.lighting else "", f"Rules: {rules}" if rules else ""]
        return "\n".join(item for item in fields if item)


_ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*(?:\*\*)?([^|:#]+?)(?:\*\*)?\s*(?:\|\s*(.+))?\s*$")


def load_subject_document(path: Path) -> SubjectDocument:
    """Load frontmatter rules and a numbered/bulleted Markdown subject backlog."""
    template = load_template(path)
    front = template.frontmatter
    raw_rules = front.get("rules", front.get("style_rules", []))
    if isinstance(raw_rules, str):
        raw_rules = [raw_rules]
    if not isinstance(raw_rules, list) or any(not isinstance(value, str) for value in raw_rules):
        raise MarkdownTemplateError("rules/style_rules must be a list of strings")
    subjects: list[Subject] = []
    for line in template.body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ITEM.match(line)
        if not match:
            continue
        title = match.group(1).strip()
        context = (match.group(2) or "").strip()
        sequence = len(subjects) + 1
        subjects.append(Subject(sequence, re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or str(sequence), title, context))
    if not subjects:
        raise MarkdownTemplateError(f"no subjects found in {path}")
    return SubjectDocument(
        path=path.expanduser().resolve(), rules=tuple(raw_rules),
        style=str(front.get("style", "")).strip(), palette=str(front.get("palette", "")).strip(),
        lighting=str(front.get("lighting", "")).strip(), subjects=tuple(subjects),
    )


def select_next_subject(db_path: Path, document: SubjectDocument, generator_key: str) -> Subject:
    """Atomically reserve the next subject and advance the cyclic cursor."""
    with connect_database(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT next_sequence FROM subject_generator_state WHERE generator_key=?", (generator_key,)).fetchone()
        sequence = int(row[0]) if row else 1
        if sequence < 1 or sequence > len(document.subjects):
            sequence = ((sequence - 1) % len(document.subjects)) + 1
        subject = document.subjects[sequence - 1]
        following = (sequence % len(document.subjects)) + 1
        db.execute(
            """INSERT INTO subject_generator_state(generator_key,next_sequence,last_item_key,updated_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(generator_key) DO UPDATE SET
               next_sequence=excluded.next_sequence,last_item_key=excluded.last_item_key,updated_at=CURRENT_TIMESTAMP""",
            (generator_key, following, subject.key),
        )
        db.commit()
        return subject


def seed_state(db_path: Path, generator_key: str, next_sequence: int = 1) -> None:
    if next_sequence < 1:
        raise ValueError("next_sequence must be positive")
    with connect_database(db_path) as db:
        db.execute(
            "INSERT INTO subject_generator_state(generator_key,next_sequence) VALUES (?,?) ON CONFLICT(generator_key) DO NOTHING",
            (generator_key, next_sequence),
        )
        db.commit()
