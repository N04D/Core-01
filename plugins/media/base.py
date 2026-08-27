"""Stable provider contract shared by Media Store adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class MediaAsset:
    """Normalized asset emitted by any external media provider."""

    external_id: str
    filename: str
    file_path: Path
    media_type: str
    mime_type: str | None = None
    thumbnail_path: Path | None = None
    file_size: int = 0
    modified_at: str | None = None
    prompt: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class MediaProvider(ABC):
    """Interface implemented by local and remote media sources."""

    source_key: str
    display_name: str
    provider_name: str

    @abstractmethod
    def discover(self) -> Iterator[MediaAsset]:
        """Yield normalized, currently available assets."""

    @abstractmethod
    def source_config(self) -> dict[str, Any]:
        """Return non-secret source configuration for persistence."""
