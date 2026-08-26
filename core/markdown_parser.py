#!/usr/bin/env python3
"""Strict YAML-frontmatter parser and safe Markdown prompt renderer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

import yaml


FRONTMATTER_PATTERN: Final = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*\r?\n?(?P<body>.*)\Z",
    re.DOTALL,
)
PLACEHOLDER_PATTERN: Final = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*}}")


class MarkdownTemplateError(ValueError):
    """Raised when a skill template or its inputs are invalid."""


@dataclass(frozen=True)
class MarkdownTemplate:
    """Parsed skill frontmatter and Markdown body."""

    frontmatter: dict[str, Any]
    body: str


def load_template(path: Path) -> MarkdownTemplate:
    """Read a UTF-8 Markdown template containing YAML frontmatter."""
    try:
        raw_text = path.expanduser().resolve(strict=True).read_text(encoding="utf-8")
    except OSError as exc:
        raise MarkdownTemplateError(f"cannot read skill template: {exc}") from exc

    match = FRONTMATTER_PATTERN.fullmatch(raw_text)
    if match is None:
        raise MarkdownTemplateError("skill template must start with YAML frontmatter")

    try:
        parsed_header = yaml.safe_load(match.group("header")) or {}
    except yaml.YAMLError as exc:
        raise MarkdownTemplateError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(parsed_header, dict):
        raise MarkdownTemplateError("YAML frontmatter must be a mapping")
    return MarkdownTemplate(frontmatter=parsed_header, body=match.group("body"))


def required_inputs(template: MarkdownTemplate) -> tuple[str, ...]:
    """Validate and return the declared input names."""
    raw_required = template.frontmatter.get("required_inputs", [])
    if not isinstance(raw_required, list):
        raise MarkdownTemplateError("required_inputs must be a YAML list")
    if any(not isinstance(item, str) or not item.strip() for item in raw_required):
        raise MarkdownTemplateError("required_inputs entries must be non-empty strings")
    normalized = tuple(item.strip() for item in raw_required)
    if len(normalized) != len(set(normalized)):
        raise MarkdownTemplateError("required_inputs contains duplicate entries")
    return normalized


def render_template(path: Path, payload: Mapping[str, Any]) -> str:
    """Validate required payload inputs and safely replace placeholders."""
    template = load_template(path)
    values = payload.get("inputs", payload)
    if not isinstance(values, Mapping):
        raise MarkdownTemplateError("payload 'inputs' must be an object")

    required = required_inputs(template)
    missing = [name for name in required if name not in values or values[name] is None]
    if missing:
        raise MarkdownTemplateError(
            "missing required input(s): " + ", ".join(sorted(missing))
        )

    placeholders = set(PLACEHOLDER_PATTERN.findall(template.body))
    undeclared = sorted(placeholders.difference(required))
    if undeclared:
        raise MarkdownTemplateError(
            "template contains undeclared placeholder(s): " + ", ".join(undeclared)
        )

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = values[name]
        if isinstance(value, (dict, list, tuple, set)):
            raise MarkdownTemplateError(f"input {name!r} must be a scalar value")
        return str(value)

    rendered = PLACEHOLDER_PATTERN.sub(replace, template.body).strip()
    if not rendered:
        raise MarkdownTemplateError("rendered prompt is empty")
    return rendered
