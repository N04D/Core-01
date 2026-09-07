"""Minimal analytics provider contract."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CollectionTarget:
    canonical_url: str | None
    external_id: str
    window: str = "lifetime"
    publication_attempt_id: int | None = None
    metadata: dict[str, Any] | None = None


class AnalyticsProvider(Protocol):
    name: str

    def collect(self, target: CollectionTarget) -> dict[str, Any]:
        """Return provider data containing a ``metrics`` object."""
