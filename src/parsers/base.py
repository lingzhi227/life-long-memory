"""Base parser interface for session JSONL files."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TOOL_OUTPUT_TRUNCATE = 500


@dataclass
class ParsedMessage:
    """A normalized message from any CLI tool."""

    ordinal: int
    role: str  # 'user' | 'assistant' | 'system' | 'tool'
    content_type: str  # 'text' | 'tool_call' | 'tool_result' | 'thinking'
    content_text: str
    content_json: str | None = None
    tool_name: str | None = None
    token_count: int = 0
    created_at: int = 0

    def to_dict(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "ordinal": self.ordinal,
            "role": self.role,
            "content_type": self.content_type,
            "content_text": self.content_text,
            "content_json": self.content_json,
            "tool_name": self.tool_name,
            "token_count": self.token_count,
            "created_at": self.created_at,
        }


@dataclass
class ParsedSession:
    """Normalized session metadata + messages from any CLI tool."""

    id: str
    source: str  # 'codex' | 'claude_code' | 'gemini'
    project_path: str | None = None
    project_name: str | None = None
    cwd: str | None = None
    model: str | None = None
    git_branch: str | None = None
    first_message_at: int = 0
    last_message_at: int = 0
    message_count: int = 0
    user_message_count: int = 0
    total_tokens: int = 0
    compaction_count: int = 0
    tools_used: list[str] = field(default_factory=list)
    raw_path: str | None = None
    title: str | None = None
    messages: list[ParsedMessage] = field(default_factory=list)

    def to_session_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "project_path": self.project_path,
            "project_name": self.project_name,
            "cwd": self.cwd,
            "model": self.model,
            "git_branch": self.git_branch,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
            "message_count": self.message_count,
            "user_message_count": self.user_message_count,
            "total_tokens": self.total_tokens,
            "compaction_count": self.compaction_count,
            "tools_used": json.dumps(sorted(set(self.tools_used))),
            "tier": "L3",
            "raw_path": self.raw_path,
            "ingested_at": int(time.time()),
            "title": self.title,
        }


def truncate(text: str, max_len: int = TOOL_OUTPUT_TRUNCATE) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…[truncated]"


def iso_to_epoch(ts: str) -> int:
    """Convert ISO8601 timestamp string to unix epoch seconds."""
    from datetime import datetime, timezone

    ts = ts.rstrip("Z").split("+")[0]
    # Handle both formats: with and without microseconds
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


def infer_project_from_cwd(cwd: str | None) -> tuple[str | None, str | None]:
    """Infer project_path and project_name from cwd."""
    if not cwd:
        return None, None
    path = Path(cwd)
    # Walk up to find a likely project root (has .git, package.json, pyproject.toml, etc.)
    # For simplicity, use the last meaningful directory component
    home = Path.home()
    if path == home or not str(path).startswith(str(home)):
        return str(path), path.name

    # Use the first directory under ~/Code/ or similar
    parts = path.parts
    for i, part in enumerate(parts):
        if part in ("Code", "Projects", "src", "repos", "workspace"):
            if i + 1 < len(parts):
                project_path = str(Path(*parts[: i + 2]))
                project_name = parts[i + 1]
                return project_path, project_name

    return str(path), path.name


class SessionParser(ABC):
    """Abstract base for session file parsers."""

    @abstractmethod
    def parse(self, file_path: Path) -> ParsedSession | None:
        """Parse a JSONL session file into a ParsedSession."""
        ...

    @abstractmethod
    def discover_files(self, base_paths: list[Path]) -> list[Path]:
        """Find all session files under the given base paths."""
        ...

    def read_jsonl(self, file_path: Path) -> list[dict]:
        """Read a JSONL file, skipping malformed lines."""
        lines = []
        with open(file_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return lines
