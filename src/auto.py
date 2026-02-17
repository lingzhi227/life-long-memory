"""Auto-processing: ingest/summarize/promote triggered by usage."""

from __future__ import annotations

import threading
import time
from pathlib import Path


COOLDOWN_PATH = Path.home() / ".tactical" / ".last_auto_run"
COOLDOWN_SECONDS = 3600  # 1 hour


def _get_db():
    from src.config import default_config
    from src.db import MemoryDB

    config = default_config()
    db = MemoryDB(config.db_path)
    db.initialize()
    return db


def auto_ingest(db=None) -> dict:
    """Ingest new sessions from all configured sources.

    Fast (no LLM calls) — safe to call synchronously before every query.
    Returns {"sessions": int, "messages": int}.
    """
    from src.config import default_config
    from src.entities import extract_entities_for_session
    from src.parsers.codex import CodexParser
    from src.parsers.claude_code import ClaudeCodeParser
    from src.parsers.gemini import GeminiParser

    if db is None:
        db = _get_db()

    config = default_config()
    sources = []
    if config.codex_enabled:
        sources.append(("codex", CodexParser(), config.codex_paths))
    if config.claude_code_enabled:
        sources.append(("claude_code", ClaudeCodeParser(), config.claude_code_paths))
    if config.gemini_enabled:
        sources.append(("gemini", GeminiParser(), config.gemini_paths))

    sessions = 0
    messages = 0
    for _source_name, parser, paths in sources:
        files = parser.discover_files(paths)
        for fpath in files:
            try:
                parsed = parser.parse(fpath)
            except Exception:
                continue
            if not parsed or db.session_exists(parsed.id):
                continue
            db.upsert_session(parsed.to_session_dict())
            msg_dicts = [m.to_dict(parsed.id) for m in parsed.messages]
            db.insert_messages(msg_dicts)
            extract_entities_for_session(db, parsed.id)
            sessions += 1
            messages += len(parsed.messages)

    return {"sessions": sessions, "messages": messages}


def _should_run_full() -> bool:
    """Check if enough time has passed since the last full pipeline run."""
    if not COOLDOWN_PATH.exists():
        return True
    try:
        last_run = float(COOLDOWN_PATH.read_text().strip())
        return (time.time() - last_run) > COOLDOWN_SECONDS
    except (ValueError, OSError):
        return True


def _mark_full_run() -> None:
    """Record that a full pipeline run just completed."""
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_PATH.write_text(str(time.time()))


def auto_process(db=None, model=None, backend=None, force=False) -> dict | None:
    """Run full pipeline: ingest -> summarize -> promote.

    Respects a 1-hour cooldown unless force=True.
    Returns stats dict, or None if skipped due to cooldown.
    """
    if not force and not _should_run_full():
        return None

    from src.summarize import summarize_session
    from src.promote import promote_project_knowledge

    if db is None:
        db = _get_db()

    # Ingest
    ingest_stats = auto_ingest(db)

    # Summarize
    sessions = db.get_unsummarized_sessions(min_user_messages=3)
    summarized = 0
    for session in sessions:
        try:
            result = summarize_session(db, session["id"], model=model, backend=backend)
            if result:
                summarized += 1
        except Exception:
            pass

    # Promote
    rows = db.conn.execute(
        "SELECT DISTINCT project_path, project_name FROM sessions "
        "WHERE project_path IS NOT NULL"
    ).fetchall()
    promoted = 0
    for project_path, _project_name in rows:
        try:
            entries = promote_project_knowledge(db, project_path, model=model, backend=backend)
            if entries:
                promoted += len(entries)
        except Exception:
            pass

    _mark_full_run()

    return {
        "ingested": ingest_stats["sessions"],
        "summarized": summarized,
        "promoted": promoted,
    }


# --- Background processing for long-running servers (MCP) ---

_bg_lock = threading.Lock()
_bg_running = False


def auto_process_background(model=None) -> None:
    """Kick off auto_process in a background thread.

    No-op if already running or cooldown hasn't expired.
    Uses its own DB connection for thread safety.
    """
    global _bg_running

    if not _should_run_full():
        return

    with _bg_lock:
        if _bg_running:
            return
        _bg_running = True

    def _run():
        global _bg_running
        try:
            # Create a fresh DB connection for this thread
            auto_process(db=None, model=model)
        finally:
            with _bg_lock:
                _bg_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
