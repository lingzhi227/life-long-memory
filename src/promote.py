"""L1 knowledge promotion from cross-session consolidation."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from src.db import MemoryDB


def _normalize(text: str) -> set[str]:
    """Normalize text to a set of lowercase words for similarity comparison."""
    return set(re.sub(r'[^\w\s]', '', text.lower()).split())


def _word_similarity(a: str, b: str) -> float:
    """Word-level Jaccard similarity between two strings."""
    words_a = _normalize(a)
    words_b = _normalize(b)
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


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
    model: str | None = None,
    backend: str | None = None,
) -> list[dict[str, Any]]:
    """Consolidate session summaries into L1 project knowledge using source-appropriate CLI backend."""
    from src.llm import call_llm

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

    # Determine the dominant source for this project's sessions
    source_counts: dict[str, int] = {}
    for s in sessions:
        src = s.get("source", "claude_code")
        source_counts[src] = source_counts.get(src, 0) + 1
    dominant_source = max(source_counts, key=source_counts.get)

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

    text = call_llm(prompt, source=dominant_source, model=model, backend=backend)

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
    existing_entries = db.get_project_knowledge(project_path)
    results = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        confidence = entry.get("confidence", 0.5)
        if confidence < 0.5:
            continue

        content = entry.get("content", "")
        ktype = entry.get("knowledge_type", "pattern")

        # Check for similar existing entry (fuzzy match)
        matched = None
        for ex in existing_entries:
            if _word_similarity(content, ex["content"]) >= 0.7:
                matched = ex
                break

        if matched:
            # Confirm existing entry — bump evidence_count, update confidence
            db.confirm_knowledge(matched["id"], confidence=confidence)
            results.append(matched)
        else:
            # Insert new entry
            knowledge = {
                "project_path": project_path,
                "knowledge_type": ktype,
                "content": content,
                "confidence": confidence,
                "evidence_count": 1,
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
