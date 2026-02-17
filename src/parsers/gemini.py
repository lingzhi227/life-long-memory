"""Parser for Gemini CLI session JSON files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.parsers.base import (
    ParsedMessage,
    ParsedSession,
    SessionParser,
    iso_to_epoch,
    truncate,
)


def _load_trusted_folders() -> dict[str, str]:
    """Load ~/.gemini/trustedFolders.json and build hash->path mapping.

    Gemini CLI stores sessions under a SHA-256 hash of the project path.
    trustedFolders.json maps known project paths to their trust status,
    so we can reverse the hash to recover the original path.
    """
    tf_path = Path.home() / ".gemini" / "trustedFolders.json"
    if not tf_path.exists():
        return {}
    try:
        data = json.loads(tf_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    mapping: dict[str, str] = {}
    for folder_path in data:
        h = hashlib.sha256(folder_path.encode()).hexdigest()
        mapping[h] = folder_path
    return mapping


class GeminiParser(SessionParser):
    """Parses Gemini CLI session JSON files.

    Gemini sessions are stored as:
      ~/.gemini/tmp/{projectHash}/chats/session-*.json

    Each file is a single JSON object with:
      sessionId, projectHash, startTime, lastUpdated, messages[]

    Message types: "user", "gemini", "info"
    """

    def __init__(self) -> None:
        self._hash_to_path: dict[str, str] | None = None

    def _get_hash_map(self) -> dict[str, str]:
        if self._hash_to_path is None:
            self._hash_to_path = _load_trusted_folders()
        return self._hash_to_path

    def discover_files(self, base_paths: list[Path]) -> list[Path]:
        files = []
        for base in base_paths:
            base = base.expanduser()
            if not base.exists():
                continue
            files.extend(sorted(base.rglob("session-*.json")))
        return files

    def parse(self, file_path: Path) -> ParsedSession | None:
        try:
            data = json.loads(file_path.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(data, dict):
            return None

        session_id = data.get("sessionId", file_path.stem)
        project_hash = data.get("projectHash", "")
        start_time = data.get("startTime", "")
        last_updated = data.get("lastUpdated", "")
        raw_messages = data.get("messages", [])

        if not raw_messages:
            return None

        # Reverse project hash to path
        hash_map = self._get_hash_map()
        project_path = hash_map.get(project_hash)
        project_name = Path(project_path).name if project_path else project_hash[:12]

        first_ts = iso_to_epoch(start_time) if start_time else 0
        last_ts = iso_to_epoch(last_updated) if last_updated else 0

        messages: list[ParsedMessage] = []
        ordinal = 0
        user_msg_count = 0
        total_tokens = 0
        tools_used: list[str] = []
        model = None
        title = None

        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type", "")
            ts_str = msg.get("timestamp", "")
            ts = iso_to_epoch(ts_str) if ts_str else 0

            if msg_type == "user":
                text = self._extract_user_text(msg)
                if text:
                    messages.append(ParsedMessage(
                        ordinal=ordinal,
                        role="user",
                        content_type="text",
                        content_text=text,
                        created_at=ts,
                    ))
                    ordinal += 1
                    user_msg_count += 1
                    if title is None:
                        title = text[:200]

            elif msg_type == "gemini":
                if not model:
                    model = msg.get("model")

                # Token accounting
                tokens = msg.get("tokens", {})
                if tokens:
                    total_tokens += tokens.get("total", 0)

                # Thinking / thoughts
                for thought in msg.get("thoughts", []):
                    desc = thought.get("description", "")
                    subject = thought.get("subject", "")
                    thought_text = f"{subject}: {desc}" if subject else desc
                    if thought_text:
                        messages.append(ParsedMessage(
                            ordinal=ordinal,
                            role="assistant",
                            content_type="thinking",
                            content_text=truncate(thought_text, 1000),
                            created_at=ts,
                        ))
                        ordinal += 1

                # Tool calls
                for tc in msg.get("toolCalls", []):
                    tool_name = tc.get("name", "")
                    args = tc.get("args", {})
                    result = tc.get("result", "")
                    if tool_name:
                        tools_used.append(tool_name)
                        messages.append(ParsedMessage(
                            ordinal=ordinal,
                            role="assistant",
                            content_type="tool_call",
                            content_text=truncate(json.dumps(args), 500),
                            content_json=json.dumps({
                                "name": tool_name,
                                "args": truncate(json.dumps(args), 1000),
                                "status": tc.get("status"),
                            }),
                            tool_name=tool_name,
                            created_at=ts,
                        ))
                        ordinal += 1

                        # Tool result
                        result_text = json.dumps(result) if not isinstance(result, str) else result
                        messages.append(ParsedMessage(
                            ordinal=ordinal,
                            role="tool",
                            content_type="tool_result",
                            content_text=truncate(result_text),
                            created_at=ts,
                        ))
                        ordinal += 1

                # Main text content
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    messages.append(ParsedMessage(
                        ordinal=ordinal,
                        role="assistant",
                        content_type="text",
                        content_text=content,
                        created_at=ts,
                    ))
                    ordinal += 1

            elif msg_type == "info":
                content = msg.get("content", "")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        item.get("text", "") for item in content
                        if isinstance(item, dict)
                    )
                if text.strip():
                    messages.append(ParsedMessage(
                        ordinal=ordinal,
                        role="system",
                        content_type="text",
                        content_text=text,
                        created_at=ts,
                    ))
                    ordinal += 1

        if not first_ts:
            first_ts = int(file_path.stat().st_mtime)
        if not last_ts:
            last_ts = first_ts

        return ParsedSession(
            id=session_id,
            source="gemini",
            project_path=project_path,
            project_name=project_name,
            cwd=project_path,
            model=model,
            first_message_at=first_ts,
            last_message_at=last_ts,
            message_count=len(messages),
            user_message_count=user_msg_count,
            total_tokens=total_tokens,
            tools_used=tools_used,
            raw_path=str(file_path),
            title=title,
            messages=messages,
        )

    @staticmethod
    def _extract_user_text(msg: dict) -> str:
        """Extract text from a user message's content field.

        User content can be a string or an array of {text: ...} objects.
        """
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    t = item.get("text", "")
                    if t:
                        parts.append(t)
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return ""
