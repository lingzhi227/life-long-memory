"""Parser for Claude Code session JSONL files."""

from __future__ import annotations

import json
from pathlib import Path

from life_long_memory.parsers.base import (
    ParsedMessage,
    ParsedSession,
    SessionParser,
    infer_project_from_cwd,
    iso_to_epoch,
    truncate,
)


class ClaudeCodeParser(SessionParser):
    """Parses Claude Code session JSONL files.

    Claude Code sessions are stored as:
      ~/.claude/projects/{project-slug}/{session-uuid}.jsonl

    Each line is a JSON object with "type" being one of:
    "user", "assistant", "progress", "file-history-snapshot", "queue-operation"
    """

    def discover_files(self, base_paths: list[Path]) -> list[Path]:
        files = []
        for base in base_paths:
            base = base.expanduser()
            if not base.exists():
                continue
            # Find all JSONL files directly under project directories
            # (not in subagent subdirectories)
            for project_dir in base.iterdir():
                if not project_dir.is_dir():
                    continue
                for f in project_dir.iterdir():
                    if f.suffix == ".jsonl" and f.is_file():
                        files.append(f)
        return sorted(files)

    def parse(self, file_path: Path) -> ParsedSession | None:
        records = self.read_jsonl(file_path)
        if not records:
            return None

        session_id = None
        cwd = None
        model = None
        git_branch = None
        tools_used: list[str] = []
        total_tokens = 0

        messages: list[ParsedMessage] = []
        ordinal = 0
        first_ts = 0
        last_ts = 0
        user_msg_count = 0
        title = None

        for rec in records:
            rec_type = rec.get("type", "")
            ts_str = rec.get("timestamp", "")
            ts = iso_to_epoch(ts_str) if ts_str else 0
            if ts and (first_ts == 0 or ts < first_ts):
                first_ts = ts
            if ts and ts > last_ts:
                last_ts = ts

            # Skip non-message types
            if rec_type in ("file-history-snapshot", "queue-operation", "progress"):
                continue

            # Extract session metadata from any message
            if not session_id:
                session_id = rec.get("sessionId")
            if not cwd:
                cwd = rec.get("cwd")
            if not git_branch:
                git_branch = rec.get("gitBranch")

            message = rec.get("message", {})
            if not message:
                continue

            if not model:
                model = message.get("model")

            role = message.get("role", "")
            content = message.get("content", "")

            # Track token usage
            usage = message.get("usage", {})
            if usage:
                out_tokens = usage.get("output_tokens", 0)
                in_tokens = usage.get("input_tokens", 0)
                total_tokens = max(total_tokens, in_tokens + out_tokens)

            if rec_type == "user":
                parsed = self._parse_user_content(content, ordinal, ts)
                for msg in parsed:
                    messages.append(msg)
                    ordinal += 1
                    if msg.content_type == "text" and msg.role == "user":
                        user_msg_count += 1
                        if title is None and msg.content_text:
                            # Skip tool results for title
                            title = msg.content_text[:200]

            elif rec_type == "assistant":
                parsed = self._parse_assistant_content(content, ordinal, ts)
                for msg in parsed:
                    messages.append(msg)
                    ordinal += 1
                    if msg.tool_name:
                        tools_used.append(msg.tool_name)

        if not session_id:
            # Derive from filename
            session_id = file_path.stem

        if not first_ts:
            first_ts = int(file_path.stat().st_mtime)
        if not last_ts:
            last_ts = first_ts

        project_path, project_name = infer_project_from_cwd(cwd)

        return ParsedSession(
            id=session_id,
            source="claude_code",
            project_path=project_path,
            project_name=project_name,
            cwd=cwd,
            model=model,
            git_branch=git_branch,
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

    def _parse_user_content(
        self, content: str | list | dict, ordinal: int, ts: int
    ) -> list[ParsedMessage]:
        """Parse user message content, which can be string or array."""
        msgs: list[ParsedMessage] = []

        if isinstance(content, str):
            if content.strip():
                msgs.append(
                    ParsedMessage(
                        ordinal=ordinal,
                        role="user",
                        content_type="text",
                        content_text=content,
                        created_at=ts,
                    )
                )
            return msgs

        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")

                if item_type == "text":
                    text = item.get("text", "")
                    if text.strip():
                        msgs.append(
                            ParsedMessage(
                                ordinal=ordinal + len(msgs),
                                role="user",
                                content_type="text",
                                content_text=text,
                                created_at=ts,
                            )
                        )

                elif item_type == "tool_result":
                    result_content = item.get("content", "")
                    if isinstance(result_content, list):
                        # Extract text from content blocks
                        parts = []
                        for block in result_content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block.get("text", ""))
                        result_content = "\n".join(parts)
                    msgs.append(
                        ParsedMessage(
                            ordinal=ordinal + len(msgs),
                            role="tool",
                            content_type="tool_result",
                            content_text=truncate(str(result_content)),
                            content_json=json.dumps(
                                {"tool_use_id": item.get("tool_use_id")}
                            ),
                            created_at=ts,
                        )
                    )

        return msgs

    def _parse_assistant_content(
        self, content: str | list | dict, ordinal: int, ts: int
    ) -> list[ParsedMessage]:
        """Parse assistant message content blocks."""
        msgs: list[ParsedMessage] = []

        if isinstance(content, str):
            if content.strip():
                msgs.append(
                    ParsedMessage(
                        ordinal=ordinal,
                        role="assistant",
                        content_type="text",
                        content_text=content,
                        created_at=ts,
                    )
                )
            return msgs

        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")

                if item_type == "text":
                    text = item.get("text", "")
                    if text.strip():
                        msgs.append(
                            ParsedMessage(
                                ordinal=ordinal + len(msgs),
                                role="assistant",
                                content_type="text",
                                content_text=text,
                                created_at=ts,
                            )
                        )

                elif item_type == "thinking":
                    text = item.get("thinking", "")
                    if text.strip():
                        msgs.append(
                            ParsedMessage(
                                ordinal=ordinal + len(msgs),
                                role="assistant",
                                content_type="thinking",
                                content_text=truncate(text, 1000),
                                created_at=ts,
                            )
                        )

                elif item_type == "tool_use":
                    name = item.get("name", "")
                    inp = item.get("input", {})
                    msgs.append(
                        ParsedMessage(
                            ordinal=ordinal + len(msgs),
                            role="assistant",
                            content_type="tool_call",
                            content_text=truncate(json.dumps(inp), 500),
                            content_json=json.dumps(
                                {
                                    "id": item.get("id"),
                                    "name": name,
                                    "input": truncate(json.dumps(inp), 1000),
                                }
                            ),
                            tool_name=name,
                            created_at=ts,
                        )
                    )

        return msgs
