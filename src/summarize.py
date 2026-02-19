"""LLM-based session summarization."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from src.db import MemoryDB

logger = logging.getLogger(__name__)


SUMMARIZE_PROMPT = """You are analyzing a CLI coding session transcript. Generate a structured summary.

The session used {model} via {source} in project "{project}" (cwd: {cwd}).

Here are the messages (user/assistant conversation):

{conversation}

---

Respond with a JSON object (no markdown, just raw JSON):
{{
  "summary_text": "A 200-500 word summary of what happened in this session. Include the problem being solved, approaches tried, and final outcome.",
  "key_decisions": ["decision 1", "decision 2", ...],
  "files_touched": ["/path/to/file1.py", ...],
  "commands_run": ["notable command 1", ...],
  "outcome": "completed | partial | error"
}}"""


def format_conversation(messages: list[dict], max_messages: int = 200) -> str:
    """Format messages into a readable conversation string."""
    lines = []
    count = 0
    for msg in messages:
        if count >= max_messages:
            lines.append(f"... ({len(messages) - count} more messages)")
            break
        role = msg.get("role", "?")
        ctype = msg.get("content_type", "text")
        text = msg.get("content_text", "")

        if not text or not text.strip():
            continue

        if ctype == "thinking":
            continue  # skip thinking blocks for summary

        if ctype == "tool_call":
            tool = msg.get("tool_name", "unknown")
            lines.append(f"[{role} → {tool}]: {text[:300]}")
        elif ctype == "tool_result":
            lines.append(f"[tool result]: {text[:200]}")
        else:
            lines.append(f"[{role}]: {text[:500]}")

        count += 1

    return "\n".join(lines)


def _parse_json_response(text: str) -> dict | None:
    """Parse JSON from LLM response, handling markdown code blocks."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extracting from markdown code block
    import re
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Try finding first { ... } block
    match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def summarize_session(
    db: MemoryDB,
    session_id: str,
    model: str | None = None,
    backend: str | None = None,
) -> dict[str, Any] | None:
    """Generate a summary for a session using the source-appropriate CLI backend."""
    from src.llm import call_llm

    session = db.get_session(session_id)
    if not session:
        return None

    messages = db.get_session_messages(session_id)
    if not messages:
        return None

    conversation = format_conversation(messages)
    if len(conversation) < 100:
        return None

    source = session.get("source", "claude_code")

    prompt = SUMMARIZE_PROMPT.format(
        model=session.get("model", "unknown"),
        source=source,
        project=session.get("project_name", "unknown"),
        cwd=session.get("cwd", "unknown"),
        conversation=conversation,
    )

    text = call_llm(prompt, source=source, model=model, backend=backend)
    data = _parse_json_response(text)
    if not data:
        return None

    summary = {
        "session_id": session_id,
        "summary_text": data.get("summary_text", ""),
        "key_decisions": json.dumps(data.get("key_decisions", [])),
        "files_touched": json.dumps(data.get("files_touched", [])),
        "commands_run": json.dumps(data.get("commands_run", [])),
        "outcome": data.get("outcome", "unknown"),
        "generated_at": int(time.time()),
        "generator_model": model or "default",
    }

    db.upsert_summary(summary)
    return summary


async def summarize_session_anthropic(
    db: MemoryDB,
    session_id: str,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Generate a summary using call_claude_full(), capturing thinking and usage metadata."""
    from src.llm import call_claude_full

    session = db.get_session(session_id)
    if not session:
        return None

    messages = db.get_session_messages(session_id)
    if not messages:
        return None

    conversation = format_conversation(messages)
    if len(conversation) < 100:
        return None

    source = session.get("source", "claude_code")

    prompt = SUMMARIZE_PROMPT.format(
        model=session.get("model", "unknown"),
        source=source,
        project=session.get("project_name", "unknown"),
        cwd=session.get("cwd", "unknown"),
        conversation=conversation,
    )

    response = call_claude_full(prompt, model=model or "haiku")
    data = _parse_json_response(response.text)
    if not data:
        logger.warning(f"Failed to parse JSON from Claude response for session {session_id}")
        return None

    summary = {
        "session_id": session_id,
        "summary_text": data.get("summary_text", ""),
        "key_decisions": json.dumps(data.get("key_decisions", [])),
        "files_touched": json.dumps(data.get("files_touched", [])),
        "commands_run": json.dumps(data.get("commands_run", [])),
        "outcome": data.get("outcome", "unknown"),
        "generated_at": int(time.time()),
        "generator_model": model or "haiku",
        "thinking": json.dumps(response.thinking) if response.thinking else None,
        "usage": json.dumps(response.usage) if response.usage else None,
        "claude_session_id": response.session_id,
    }

    db.upsert_summary(summary)
    return summary
