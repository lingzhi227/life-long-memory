"""Regex-based entity extraction from messages."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.db import MemoryDB

# Entity extraction patterns
PATTERNS: dict[str, re.Pattern] = {
    "file_path": re.compile(
        r'(?:^|[\s"`\'(])(/[\w./\-]+\.\w{1,10})', re.MULTILINE
    ),
    "function": re.compile(
        r'(?:fn |def |function |class |async def )\s*(\w+)', re.MULTILINE
    ),
    "error_type": re.compile(
        r'((?:Error|Exception|Panic|FAIL|TypeError|ValueError|KeyError|RuntimeError|'
        r'ImportError|ModuleNotFoundError|FileNotFoundError|PermissionError|'
        r'SyntaxError|AttributeError|NameError|IndexError|OSError)'
        r'[\w:]*)',
        re.MULTILINE,
    ),
    "package": re.compile(
        r'(?:import |from |require\([\'""]|use )(\w[\w./\-]*)', re.MULTILINE
    ),
    "command": re.compile(
        r'(?:^\$ |^> )\s*(\w[\w\-]+ [^\n]{0,80})', re.MULTILINE
    ),
}

# Values to ignore (too generic or noisy)
IGNORE_VALUES: dict[str, set[str]] = {
    "file_path": {"/dev/null", "/tmp", "/usr", "/bin", "/etc"},
    "function": {"self", "cls", "main", "test", "init", "new", "get", "set"},
    "package": {"os", "sys", "re", "json", "time", "typing", "io"},
}


@dataclass
class ExtractedEntity:
    entity_type: str
    value: str
    context: str  # snippet around the match


def extract_entities(text: str) -> list[ExtractedEntity]:
    """Extract entities from a text string using regex patterns."""
    results: list[ExtractedEntity] = []
    seen: set[tuple[str, str]] = set()

    for entity_type, pattern in PATTERNS.items():
        ignore = IGNORE_VALUES.get(entity_type, set())
        for match in pattern.finditer(text):
            value = match.group(1).strip()
            if not value or len(value) < 2:
                continue
            if value in ignore:
                continue
            key = (entity_type, value)
            if key in seen:
                continue
            seen.add(key)

            # Extract context snippet (50 chars before and after)
            start = max(0, match.start() - 50)
            end = min(len(text), match.end() + 50)
            context = text[start:end].replace("\n", " ").strip()

            results.append(ExtractedEntity(entity_type, value, context))

    return results


def extract_entities_for_session(db: MemoryDB, session_id: str) -> int:
    """Extract entities from all messages in a session and store them."""
    messages = db.get_session_messages(session_id)
    session = db.get_session(session_id)
    if not session:
        return 0

    count = 0
    for msg in messages:
        if msg["role"] not in ("user", "assistant"):
            continue
        text = msg.get("content_text", "")
        if not text:
            continue

        entities = extract_entities(text)
        for ent in entities:
            entity_id = db.upsert_entity(
                ent.entity_type,
                ent.value,
                msg["created_at"],
            )
            db.insert_entity_occurrence(
                entity_id, session_id, msg["id"], ent.context
            )
            count += 1

    db.conn.commit()
    return count
