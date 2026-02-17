"""L1 knowledge promotion from cross-session consolidation."""

from __future__ import annotations

import json
import time
from typing import Any

from src.db import MemoryDB


PROMOTE_PROMPT = """You are analyzing multiple coding session summaries for the same project.
Extract stable patterns, preferences, architectural decisions, and gotchas.

Project: {project_path}

Session summaries:
{summaries}

Existing knowledge entries (if any):
{existing}

---

Return a JSON array of knowledge entries. Each entry should be a pattern that appears across
multiple sessions (not one-off observations). Types: pattern, preference, architecture, gotcha, workflow.

[
  {{
    "knowledge_type": "pattern | preference | architecture | gotcha | workflow",
    "content": "Concise description of the knowledge entry",
    "confidence": 0.5
  }},
  ...
]

Only include entries with confidence >= 0.5. Return empty array [] if nothing is stable enough."""


def promote_project_knowledge(
    db: MemoryDB,
    project_path: str,
    model: str = "haiku",
) -> list[dict[str, Any]]:
    """Consolidate session summaries into L1 project knowledge using local Claude CLI."""
    from src.llm import call_claude

    # Get all summarized sessions for this project
    sessions = db.list_sessions(project_path=project_path, limit=100)
    summaries = []
    for s in sessions:
        summary = db.get_summary(s["id"])
        if summary:
            summaries.append(
                f"Session {s['id']} ({s.get('title', 'untitled')}):\n"
                f"{summary['summary_text']}\n"
                f"Decisions: {summary.get('key_decisions', '[]')}\n"
            )

    if len(summaries) < 2:
        return []

    existing = db.get_project_knowledge(project_path)
    existing_text = "\n".join(
        f"- [{e['knowledge_type']}] {e['content']} (confidence: {e['confidence']})"
        for e in existing
    ) or "None yet."

    prompt = PROMOTE_PROMPT.format(
        project_path=project_path,
        summaries="\n---\n".join(summaries),
        existing=existing_text,
    )

    text = call_claude(prompt, model=model)

    try:
        entries = json.loads(text)
    except json.JSONDecodeError:
        import re
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            entries = json.loads(match.group(0))
        else:
            return []

    if not isinstance(entries, list):
        return []

    now = int(time.time())
    session_ids = [s["id"] for s in sessions if db.get_summary(s["id"])]
    results = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        confidence = entry.get("confidence", 0.5)
        if confidence < 0.5:
            continue

        knowledge = {
            "project_path": project_path,
            "knowledge_type": entry.get("knowledge_type", "pattern"),
            "content": entry.get("content", ""),
            "confidence": confidence,
            "evidence_count": len(summaries),
            "source_sessions": json.dumps(session_ids[:10]),
            "first_seen_at": now,
            "last_confirmed_at": now,
        }
        db.upsert_project_knowledge(knowledge)
        results.append(knowledge)

    return results


def select_l1_context(db: MemoryDB, project_path: str, budget_tokens: int = 2000) -> str:
    """Select L1 knowledge entries to inject into agent system prompt.

    Returns formatted text within the token budget.
    """
    entries = db.get_project_knowledge(project_path)
    if not entries:
        return ""

    lines = ["## Project Knowledge (from previous sessions)\n"]
    estimated_tokens = 10  # header

    for entry in entries:
        line = f"- **[{entry['knowledge_type']}]** {entry['content']}"
        # Rough estimate: 1 token ~= 4 chars
        line_tokens = len(line) // 4
        if estimated_tokens + line_tokens > budget_tokens:
            break
        lines.append(line)
        estimated_tokens += line_tokens

    return "\n".join(lines)
