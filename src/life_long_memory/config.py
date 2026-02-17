"""Configuration management for life-long memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SourceConfig:
    paths: list[Path]
    enabled: bool = True


@dataclass
class Config:
    """Configuration for the life-long memory system."""

    codex_paths: list[Path] = field(
        default_factory=lambda: [Path.home() / ".codex" / "sessions"]
    )
    claude_code_paths: list[Path] = field(
        default_factory=lambda: [Path.home() / ".claude" / "projects"]
    )
    codex_enabled: bool = True
    claude_code_enabled: bool = True

    db_path: Path = field(
        default_factory=lambda: Path.home() / ".tactical" / "memory.sqlite"
    )

    # Tier thresholds
    l1_max_tokens_per_project: int = 2000
    l2_archive_after_days: int = 90
    l3_delete_embeddings_after_days: int = 180

    # Processing
    summarization_model: str = "claude-haiku-4-5-20251001"
    max_concurrent_jobs: int = 4


def default_config() -> Config:
    return Config()
