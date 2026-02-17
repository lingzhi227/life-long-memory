"""Parser for Codex CLI session JSONL files."""

from __future__ import annotations

import json
from pathlib import Path

from src.parsers.base import (
    ParsedMessage,
    ParsedSession,
    SessionParser,
    infer_project_from_cwd,
    iso_to_epoch,
    truncate,
)


class CodexParser(SessionParser):
    """Parses Codex session JSONL files.

    Codex sessions are stored as:
      ~/.codex/sessions/{year}/{month}/{date}/rollout-{timestamp}-{uuid}.jsonl

    Each line has: {"timestamp": str, "type": str, "payload": {...}}
    Types: session_meta, turn_context, response_item, event_msg
    """

    def discover_files(self, base_paths: list[Path]) -> list[Path]:
        files = []
        for base in base_paths:
            base = base.expanduser()
            if not base.exists():
                continue
            files.extend(sorted(base.rglob("rollout-*.jsonl")))
        return files

    def parse(self, file_path: Path) -> ParsedSession | None:
        records = self.read_jsonl(file_path)
        if not records:
            return None

        # Extract session metadata
        session_id = None
        cwd = None
        model = None
        tools_used: list[str] = []
        total_tokens = 0
        compaction_count = 0

        messages: list[ParsedMessage] = []
        ordinal = 0
        first_ts = 0
        last_ts = 0
        user_msg_count = 0
        title = None

        for rec in records:
            ts_str = rec.get("timestamp", "")
            ts = iso_to_epoch(ts_str) if ts_str else 0
            if ts and (first_ts == 0 or ts < first_ts):
                first_ts = ts
            if ts and ts > last_ts:
                last_ts = ts

            rec_type = rec.get("type", "")
            payload = rec.get("payload", {})

            if rec_type == "session_meta":
                session_id = payload.get("id", "")
                cwd = payload.get("cwd")

            elif rec_type == "turn_context":
                if not cwd:
                    cwd = payload.get("cwd")
                if not model:
                    model = payload.get("model")

            elif rec_type == "response_item":
                msg = self._parse_response_item(payload, ordinal, ts)
                if msg:
                    messages.append(msg)
                    ordinal += 1
                    if msg.role == "user" and msg.content_type == "text":
                        user_msg_count += 1
                        if title is None and msg.content_text:
                            # Use first non-context, non-system user message as title
                            text = msg.content_text.strip()
                            if (not text.startswith("<environment_context>")
                                    and not text.startswith("# AGENTS.md")
                                    and not text.startswith("# Context from my IDE")
                                    and not text.startswith("<INSTRUCTIONS>")
                                    and not text.startswith("<permissions")
                                    and len(text) < 2000):  # Skip large instruction blocks
                                title = text[:200]
                    if msg.tool_name:
                        tools_used.append(msg.tool_name)

            elif rec_type == "event_msg":
                payload_type = payload.get("type", "")

                if payload_type == "user_message":
                    text = payload.get("message", "")
                    if text:
                        messages.append(
                            ParsedMessage(
                                ordinal=ordinal,
                                role="user",
                                content_type="text",
                                content_text=text,
                                created_at=ts,
                            )
                        )
                        ordinal += 1
                        user_msg_count += 1
                        if title is None:
                            title = text[:200]

                elif payload_type == "token_count":
                    info = payload.get("info")
                    if info and isinstance(info, dict):
                        usage = info.get("total_token_usage", {})
                        if usage:
                            total_tokens = usage.get("total_tokens", total_tokens)

        if not session_id:
            # Derive session ID from filename
            session_id = file_path.stem.replace("rollout-", "")

        if not first_ts:
            first_ts = int(file_path.stat().st_mtime)
        if not last_ts:
            last_ts = first_ts

        project_path, project_name = infer_project_from_cwd(cwd)

        return ParsedSession(
            id=session_id,
            source="codex",
            project_path=project_path,
            project_name=project_name,
            cwd=cwd,
            model=model,
            first_message_at=first_ts,
            last_message_at=last_ts,
            message_count=len(messages),
            user_message_count=user_msg_count,
            total_tokens=total_tokens,
            compaction_count=compaction_count,
            tools_used=tools_used,
            raw_path=str(file_path),
            title=title,
            messages=messages,
        )

    def _parse_response_item(
        self, payload: dict, ordinal: int, ts: int
    ) -> ParsedMessage | None:
        ptype = payload.get("type", "")

        if ptype == "message":
            role = payload.get("role", "user")
            content_parts = payload.get("content", [])
            text_parts = []
            for part in content_parts:
                if isinstance(part, dict):
                    t = part.get("text", "")
                    if t:
                        text_parts.append(t)
                elif isinstance(part, str):
                    text_parts.append(part)
            text = "\n".join(text_parts)
            if not text:
                return None
            return ParsedMessage(
                ordinal=ordinal,
                role=role,
                content_type="text",
                content_text=text,
                created_at=ts,
            )

        elif ptype == "reasoning":
            summary_parts = payload.get("summary", [])
            text_parts = []
            for part in summary_parts:
                if isinstance(part, dict):
                    text_parts.append(part.get("text", ""))
            text = "\n".join(text_parts)
            if not text:
                return None
            return ParsedMessage(
                ordinal=ordinal,
                role="assistant",
                content_type="thinking",
                content_text=text,
                created_at=ts,
            )

        elif ptype == "function_call":
            name = payload.get("name", "")
            args = payload.get("arguments", "")
            return ParsedMessage(
                ordinal=ordinal,
                role="assistant",
                content_type="tool_call",
                content_text=truncate(args, 500),
                content_json=json.dumps(
                    {"name": name, "arguments": args, "call_id": payload.get("call_id")}
                ),
                tool_name=name,
                created_at=ts,
            )

        elif ptype == "function_call_output":
            output = payload.get("output", "")
            return ParsedMessage(
                ordinal=ordinal,
                role="tool",
                content_type="tool_result",
                content_text=truncate(output),
                content_json=json.dumps(
                    {"call_id": payload.get("call_id"), "output": truncate(output, 1000)}
                ),
                created_at=ts,
            )

        elif ptype == "custom_tool_call":
            name = payload.get("name", "")
            inp = payload.get("input", "")
            return ParsedMessage(
                ordinal=ordinal,
                role="assistant",
                content_type="tool_call",
                content_text=truncate(str(inp), 500),
                content_json=json.dumps(
                    {"name": name, "input": truncate(str(inp), 1000), "call_id": payload.get("call_id")}
                ),
                tool_name=name,
                created_at=ts,
            )

        elif ptype == "custom_tool_call_output":
            output = payload.get("output", "")
            return ParsedMessage(
                ordinal=ordinal,
                role="tool",
                content_type="tool_result",
                content_text=truncate(str(output)),
                created_at=ts,
            )

        return None
