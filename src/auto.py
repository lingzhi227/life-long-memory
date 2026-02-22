"""Auto-processing: ingest/summarize/promote triggered by usage."""

from __future__ import annotations

import concurrent.futures
import logging
import re
import sys
import threading
import time
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

COOLDOWN_PATH = Path.home() / ".tactical" / ".last_promote_run"
DAILY_AUTO_PATH = Path.home() / ".tactical" / ".last_daily_auto"
PROMOTE_COOLDOWN_SECONDS = 3600  # 1 hour between promote runs


# ── Notification helper ──

def _notify(message: str) -> None:
    """Log and write to stderr (MCP server stderr is visible to user)."""
    logger.info(message)
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:
        pass


# ── Daily trigger detection ──

def _should_run_daily() -> bool:
    """Check if daily auto process has already run today."""
    if not DAILY_AUTO_PATH.exists():
        return True
    try:
        last_date = DAILY_AUTO_PATH.read_text().strip()
        return last_date != str(date.today())
    except (OSError, ValueError):
        return True


def _mark_daily_run() -> None:
    """Record that daily auto process ran today."""
    DAILY_AUTO_PATH.parent.mkdir(parents=True, exist_ok=True)
    DAILY_AUTO_PATH.write_text(str(date.today()))


# ── Existing cooldown helpers ──

def _should_promote() -> bool:
    """Check if enough time has passed since the last promote run."""
    if not COOLDOWN_PATH.exists():
        return True
    try:
        last_run = float(COOLDOWN_PATH.read_text().strip())
        return (time.time() - last_run) > PROMOTE_COOLDOWN_SECONDS
    except (ValueError, OSError):
        return True


def _mark_promote_run() -> None:
    """Record that a promote run just completed."""
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_PATH.write_text(str(time.time()))


# Alias for cli.py compatibility
_mark_full_run = _mark_promote_run


# ── DB helper ──

def _get_db():
    from src.config import default_config
    from src.db import MemoryDB

    config = default_config()
    db = MemoryDB(config.db_path)
    db.initialize()
    return db


# ── Session status detection ──

def _session_status(db, parsed) -> str:
    """Determine if a parsed session is new, updated, or unchanged.

    Returns "new", "updated", or "unchanged".
    """
    existing = db.get_session(parsed.id)
    if existing is None:
        return "new"

    # Compare key fields to detect updates (e.g. user resumed the session)
    if (
        parsed.message_count != existing["message_count"]
        or parsed.user_message_count != existing["user_message_count"]
        or parsed.last_message_at != existing["last_message_at"]
    ):
        return "updated"

    return "unchanged"


# ── Session quality filter ──

_AUTOMATION_TITLE_PATTERNS = [
    re.compile(r"^/[\w/.-]+$"),         # pure file path
    re.compile(r"^\w+$"),               # single word
    re.compile(r"^(y|n|yes|no|ok)$", re.IGNORECASE),  # single-word reply
    re.compile(r"^You are:", re.IGNORECASE),  # automated agent system prompts
    re.compile(r"^\[Request interrupted"),     # interrupted before real content
]

# Prefixes injected by IDE / system, not real human input
_SYSTEM_CONTEXT_PREFIXES = (
    "# AGENTS.md",
    "<environment_context>",
    "# Context from my IDE",
    "<INSTRUCTIONS>",
    "<permissions",
    "Read the file /var/folders",
    "Read the file /tmp",
)


def _has_real_user_messages(db, session_id: str, min_real: int = 2) -> bool:
    """Check if a session has enough real human-authored user messages.

    Codex IDE injects system context (AGENTS.md, environment_context, etc.)
    as user messages, inflating user_message_count. This function checks
    for messages that are actual human input.
    """
    rows = db.conn.execute(
        "SELECT content_text FROM messages "
        "WHERE session_id = ? AND role = 'user' AND content_type = 'text' "
        "ORDER BY ordinal",
        (session_id,),
    ).fetchall()

    real_count = 0
    for row in rows:
        text = (row[0] or "").strip()
        if not text:
            continue
        if any(text.startswith(prefix) for prefix in _SYSTEM_CONTEXT_PREFIXES):
            continue
        real_count += 1
        if real_count >= min_real:
            return True
    return False


def _is_quality_session(session: dict, db=None) -> bool:
    """Filter out low-quality / automated sessions.

    Quality criteria:
    - >= 3 user messages
    - >= 5 total messages
    - session duration >= 60 seconds
    - title doesn't match automation patterns
    - has real human-authored messages (not just IDE context injection)
    """
    if session.get("user_message_count", 0) < 3:
        return False
    if session.get("message_count", 0) < 5:
        return False

    duration = (session.get("last_message_at", 0) - session.get("first_message_at", 0))
    if duration < 60:
        return False

    title = session.get("title", "") or ""
    for pattern in _AUTOMATION_TITLE_PATTERNS:
        if pattern.match(title.strip()):
            return False

    # Deep check: verify real human messages exist (catches codex bot sessions)
    if db is not None:
        if not _has_real_user_messages(db, session["id"]):
            return False

    return True


# ── Ingest (detects new + updated) ──

def auto_ingest(db=None) -> dict:
    """Ingest new and updated sessions from all configured sources.

    Fast (no LLM calls) — safe to call synchronously before every query.
    Returns {"sessions": int, "messages": int, "new_session_ids": [...], "updated_session_ids": [...]}.
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
    new_session_ids = []
    updated_session_ids = []
    for _source_name, parser, paths in sources:
        files = parser.discover_files(paths)
        for fpath in files:
            try:
                parsed = parser.parse(fpath)
            except Exception:
                continue
            if not parsed or parsed.user_message_count == 0:
                continue

            status = _session_status(db, parsed)
            if status == "unchanged":
                continue

            # Upsert session metadata (ON CONFLICT updates key fields)
            db.upsert_session(parsed.to_session_dict())

            if status == "new":
                msg_dicts = [m.to_dict(parsed.id) for m in parsed.messages]
                db.insert_messages(msg_dicts)
                extract_entities_for_session(db, parsed.id)
                sessions += 1
                messages += len(parsed.messages)
                new_session_ids.append(parsed.id)
            elif status == "updated":
                # Re-insert messages (INSERT OR IGNORE handles duplicates)
                msg_dicts = [m.to_dict(parsed.id) for m in parsed.messages]
                db.insert_messages(msg_dicts)
                extract_entities_for_session(db, parsed.id)
                updated_session_ids.append(parsed.id)

    if new_session_ids:
        logger.info(f"Ingested {sessions} new sessions ({messages} messages)")
    if updated_session_ids:
        logger.info(f"Detected {len(updated_session_ids)} updated sessions")

    return {
        "sessions": sessions,
        "messages": messages,
        "new_session_ids": new_session_ids,
        "updated_session_ids": updated_session_ids,
    }


# ── Summarize ──

SUMMARIZE_WORKERS = 8


def _summarize_one(session_id: str, model=None, backend=None) -> bool:
    """Summarize a single session in its own DB connection (thread-safe)."""
    from src.summarize import summarize_session

    thread_db = _get_db()
    try:
        return summarize_session(thread_db, session_id, model=model, backend=backend) is not None
    except Exception as e:
        logger.error(f"Failed to summarize {session_id[:12]}...: {e}")
        return False


def summarize_new_sessions(db=None, session_ids=None, model=None, backend=None, max_workers=SUMMARIZE_WORKERS) -> int:
    """Summarize specific sessions (or all unsummarized ones) in parallel.

    Called immediately when new sessions are ingested — no cooldown.
    Returns count of successfully summarized sessions.
    """
    if db is None:
        db = _get_db()

    if session_ids:
        sessions = []
        for sid in session_ids:
            s = db.get_session(sid)
            if s and _is_quality_session(s, db=db):
                sessions.append(s)
    else:
        sessions = db.get_unsummarized_sessions(min_user_messages=3)
        sessions = [s for s in sessions if _is_quality_session(s, db=db)]

    if not sessions:
        return 0

    total = len(sessions)
    _notify(f"[daily-auto] Summarizing {total} sessions ({max_workers} workers)...")

    summarized = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_summarize_one, s["id"], model, backend): s
            for s in sessions
        }
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            s = futures[future]
            ok = future.result()
            if ok:
                summarized += 1
            title = (s.get("title") or "")[:50].replace("\n", " ")
            status = "OK" if ok else "SKIP"
            _notify(f"[daily-auto]   [{i}/{total}] {status} {s['id'][:12]}... {title}")

    _notify(f"[daily-auto] Summarization complete: {summarized}/{total}")
    return summarized


# ── Self-test ──

def _run_self_test(db) -> bool:
    """Verify DB is healthy: stats queryable, FTS works, summaries exist."""
    try:
        stats = db.stats()
        assert stats["total_sessions"] > 0, "No sessions in DB"

        # Verify FTS is queryable
        db.search_fts("test", limit=1)

        _notify(
            f"[daily-auto] Self-test passed: "
            f"{stats['total_sessions']} sessions, "
            f"{stats['total_summaries']} summaries (L2), "
            f"{stats['total_knowledge_entries']} knowledge entries (L1)"
        )
        return True
    except Exception as e:
        _notify(f"[daily-auto] Self-test FAILED: {e}")
        return False


# ── Daily complete processing flow ──

PROMOTE_WORKERS = 4


def _promote_one(project_path: str, model=None, backend=None) -> dict:
    """Promote a single project in its own DB connection (thread-safe)."""
    from src.promote import promote_project_knowledge

    thread_db = _get_db()
    return promote_project_knowledge(thread_db, project_path, model=model, backend=backend)


def _get_promotable_projects(db) -> list[str]:
    """Get all projects with >= 2 summarized sessions."""
    rows = db.conn.execute(
        """SELECT s.project_path, COUNT(ss.session_id) as cnt
           FROM sessions s
           JOIN session_summaries ss ON s.id = ss.session_id
           WHERE s.project_path IS NOT NULL
           GROUP BY s.project_path
           HAVING cnt >= 2"""
    ).fetchall()
    return [r[0] for r in rows]


def daily_auto_process(db=None, model=None, backend=None) -> dict:
    """Daily auto process: ingest → filter → summarize → backfill → promote → self-test.

    1. Ingest new + updated sessions
    2. New sessions → quality filter → parallel summarize
    3. Updated sessions → delete old summary → parallel re-summarize
    4. Backfill: catch up on ALL historical unsummarized quality sessions
    5. Parallel promote all eligible projects (>= 2 summaries)
    6. Self-test and mark done
    """
    if db is None:
        db = _get_db()

    start = time.time()
    _notify("[daily-auto] Starting daily auto process...")

    # 1. Ingest — detect new + updated sessions
    ingest_stats = auto_ingest(db)
    new_ids = ingest_stats["new_session_ids"]
    updated_ids = ingest_stats["updated_session_ids"]
    _notify(
        f"[daily-auto] Ingested: {len(new_ids)} new, {len(updated_ids)} updated sessions"
    )

    summarized = 0

    # 2. New sessions → quality filter → parallel summarize
    if new_ids:
        quality_new = []
        for sid in new_ids:
            s = db.get_session(sid)
            if s and _is_quality_session(s, db=db):
                quality_new.append(sid)
        _notify(f"[daily-auto] {len(quality_new)}/{len(new_ids)} new sessions pass quality filter")
        if quality_new:
            summarized += summarize_new_sessions(
                db, session_ids=quality_new, model=model, backend=backend
            )

    # 3. Updated sessions → delete old summary → parallel re-summarize
    if updated_ids:
        quality_updated = []
        for sid in updated_ids:
            s = db.get_session(sid)
            if s and _is_quality_session(s, db=db):
                quality_updated.append(sid)
        _notify(f"[daily-auto] {len(quality_updated)}/{len(updated_ids)} updated sessions pass quality filter")
        for sid in quality_updated:
            if db.delete_summary(sid):
                _notify(f"[daily-auto] Deleted old summary for {sid[:12]}...")
        if quality_updated:
            summarized += summarize_new_sessions(
                db, session_ids=quality_updated, model=model, backend=backend
            )

    # 4. Backfill: summarize ALL historical unsummarized quality sessions
    backfill = summarize_new_sessions(db, model=model, backend=backend)
    if backfill:
        _notify(f"[daily-auto] Backfill: summarized {backfill} historical sessions")
    summarized += backfill

    _notify(f"[daily-auto] Total summarized: {summarized}")

    # 5. Parallel promote all eligible projects (>= 2 summaries)
    promoted_projects = 0
    promoted_confirmed = 0
    promoted_new = 0

    promotable = _get_promotable_projects(db)
    if promotable:
        _notify(f"[daily-auto] Promoting {len(promotable)} projects ({PROMOTE_WORKERS} workers)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=PROMOTE_WORKERS) as executor:
            futures = {
                executor.submit(_promote_one, p, model, backend): p
                for p in promotable
            }
            for future in concurrent.futures.as_completed(futures):
                p = futures[future]
                try:
                    result = future.result()
                    if result["entries"]:
                        promoted_projects += 1
                        promoted_confirmed += result["confirmed"]
                        promoted_new += result["new"]
                    _notify(
                        f"[daily-auto]   {p}: "
                        f"{len(result['entries'])} entries (confirmed={result['confirmed']}, new={result['new']})"
                    )
                except Exception as e:
                    _notify(f"[daily-auto]   {p}: FAIL {e}")

        _notify(
            f"[daily-auto] Promoted {promoted_projects} projects "
            f"(confirmed {promoted_confirmed}, new {promoted_new})"
        )

    # 6. Self-test
    _run_self_test(db)

    # 7. Mark daily run complete
    _mark_daily_run()
    _mark_promote_run()

    elapsed = time.time() - start
    _notify(f"[daily-auto] Daily auto process complete. ({elapsed:.0f}s)")

    return {
        "new_sessions": len(new_ids),
        "updated_sessions": len(updated_ids),
        "summarized": summarized,
        "promoted_projects": promoted_projects,
        "promoted_confirmed": promoted_confirmed,
        "promoted_new": promoted_new,
    }


# ── Legacy full pipeline (used by CLI cmd_auto) ──

def auto_process(db=None, model=None, backend=None, force=False) -> dict | None:
    """Run full pipeline: ingest -> summarize -> promote.

    Summarize runs unconditionally for new sessions.
    Promote respects a 1-hour cooldown unless force=True.
    Returns stats dict.
    """
    from src.promote import promote_project_knowledge

    if db is None:
        db = _get_db()

    # Ingest
    ingest_stats = auto_ingest(db)

    # Summarize — always runs for unsummarized sessions
    summarized = summarize_new_sessions(db, model=model, backend=backend)

    # Promote — respects cooldown
    promoted = 0
    promoted_confirmed = 0
    promoted_new = 0
    promoted_projects = 0

    if force or _should_promote():
        thirty_days_ago = int(time.time()) - 30 * 86400
        rows = db.conn.execute(
            "SELECT DISTINCT project_path, project_name FROM sessions "
            "WHERE project_path IS NOT NULL AND last_message_at >= ?",
            (thirty_days_ago,),
        ).fetchall()
        for project_path, _project_name in rows:
            try:
                result = promote_project_knowledge(db, project_path, model=model, backend=backend)
                if result["entries"]:
                    promoted += len(result["entries"])
                    promoted_confirmed += result["confirmed"]
                    promoted_new += result["new"]
                    promoted_projects += 1
            except Exception as e:
                logger.error(f"Failed to promote knowledge for {project_path}: {e}")

        _mark_promote_run()

    return {
        "ingested": ingest_stats["sessions"],
        "summarized": summarized,
        "promoted": promoted,
        "promoted_confirmed": promoted_confirmed,
        "promoted_new": promoted_new,
        "promoted_projects": promoted_projects,
    }


# --- Background processing for long-running servers (MCP) ---

_bg_lock = threading.Lock()
_bg_summarize_running = False
_bg_promote_running = False
_bg_daily_running = False


def summarize_new_sessions_background(session_ids: list[str], model=None) -> None:
    """Kick off summarization for new sessions in a background thread.

    Runs immediately — no cooldown. Skips if a summarize is already in progress.
    """
    global _bg_summarize_running

    if not session_ids:
        return

    with _bg_lock:
        if _bg_summarize_running:
            logger.info("Summarization already running in background, skipping")
            return
        _bg_summarize_running = True

    def _run():
        global _bg_summarize_running
        try:
            db = _get_db()
            summarize_new_sessions(db, session_ids=session_ids, model=model)
        except Exception as e:
            logger.error(f"Background summarization failed: {e}")
        finally:
            with _bg_lock:
                _bg_summarize_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    logger.info(f"Started background summarization for {len(session_ids)} sessions")


def promote_background(model=None) -> None:
    """Kick off promote in a background thread with cooldown."""
    global _bg_promote_running

    if not _should_promote():
        return

    with _bg_lock:
        if _bg_promote_running:
            return
        _bg_promote_running = True

    def _run():
        global _bg_promote_running
        try:
            from src.promote import promote_project_knowledge

            db = _get_db()
            thirty_days_ago = int(time.time()) - 30 * 86400
            rows = db.conn.execute(
                "SELECT DISTINCT project_path, project_name FROM sessions "
                "WHERE project_path IS NOT NULL AND last_message_at >= ?",
                (thirty_days_ago,),
            ).fetchall()
            for project_path, _project_name in rows:
                try:
                    promote_project_knowledge(db, project_path, model=model)
                except Exception as e:
                    logger.error(f"Failed to promote {project_path}: {e}")
            _mark_promote_run()
        except Exception as e:
            logger.error(f"Background promote failed: {e}")
        finally:
            with _bg_lock:
                _bg_promote_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


def daily_auto_process_background() -> None:
    """Run daily auto process in a daemon background thread.

    Uses _bg_daily_running lock to prevent re-entry.
    """
    global _bg_daily_running

    with _bg_lock:
        if _bg_daily_running:
            _notify("[daily-auto] Already running, skipping")
            return
        _bg_daily_running = True

    def _run():
        global _bg_daily_running
        try:
            daily_auto_process()
        except Exception as e:
            _notify(f"[daily-auto] Background daily process failed: {e}")
            logger.error(f"Background daily process failed: {e}", exc_info=True)
        finally:
            with _bg_lock:
                _bg_daily_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    _notify("[daily-auto] Started background daily auto process")


# Backward compat
def auto_process_background(model=None) -> None:
    """Legacy entry point — now split into summarize + promote."""
    # Summarize all unsummarized sessions
    try:
        db = _get_db()
        unsummarized = db.get_unsummarized_sessions(min_user_messages=3)
        if unsummarized:
            sids = [s["id"] for s in unsummarized]
            summarize_new_sessions_background(sids, model=model)
    except Exception as e:
        logger.error(f"auto_process_background failed to check unsummarized sessions: {e}")

    promote_background(model=model)
