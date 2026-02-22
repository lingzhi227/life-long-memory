"""MCP server exposing memory query tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.db import MemoryDB
from src.search import hybrid_search, timeline_search
from src.promote import select_l1_context

# Global DB instance, initialized on startup
_db: MemoryDB | None = None


def get_db() -> MemoryDB:
    global _db
    if _db is None:
        _db = MemoryDB()
        _db.initialize()
    return _db


def _auto_refresh() -> None:
    """Auto-ingest new sessions; trigger daily process on first use of the day."""
    from src.auto import (
        _should_run_daily,
        auto_ingest,
        daily_auto_process_background,
        summarize_new_sessions_background,
        promote_background,
    )

    # Daily process: first use today → full pipeline in background
    if _should_run_daily():
        daily_auto_process_background()
        return  # daily process handles ingest/summarize/promote itself

    # Lightweight path: just ingest + summarize new sessions
    db = get_db()
    result = auto_ingest(db)

    if result["new_session_ids"]:
        summarize_new_sessions_background(result["new_session_ids"])

    promote_background()


def _do_search(
    query: str,
    limit: int = 10,
    project: str | None = None,
    after: str | None = None,
) -> str:
    _auto_refresh()
    db = get_db()
    after_epoch = None
    if after:
        try:
            dt = datetime.fromisoformat(after).replace(tzinfo=timezone.utc)
            after_epoch = int(dt.timestamp())
        except ValueError:
            pass

    results = hybrid_search(db, query, limit=limit, project_path=project, after=after_epoch)
    if not results:
        return "No matching sessions found."

    output = []
    for r in results:
        ts = datetime.fromtimestamp(r.first_message_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        output.append(
            f"**{r.title or 'Untitled'}** (score: {r.score:.2f})\n"
            f"  Session: {r.session_id} | Source: {r.source} | Project: {r.project_name or 'N/A'}\n"
            f"  Date: {ts}\n"
            f"  {'Summary: ' + r.summary[:200] + '...' if r.summary else ''}\n"
            f"  Match: {r.matching_snippets[0][:150] if r.matching_snippets else ''}"
        )
    return "\n\n".join(output)


def _do_timeline(
    project: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> str:
    _auto_refresh()
    db = get_db()
    after_epoch = None
    before_epoch = None
    if after:
        try:
            dt = datetime.fromisoformat(after).replace(tzinfo=timezone.utc)
            after_epoch = int(dt.timestamp())
        except ValueError:
            pass
    if before:
        try:
            dt = datetime.fromisoformat(before).replace(tzinfo=timezone.utc)
            before_epoch = int(dt.timestamp())
        except ValueError:
            pass

    results = timeline_search(db, project_path=project, after=after_epoch, before=before_epoch, limit=limit)
    if not results:
        return "No sessions found for the given criteria."

    output = []
    for r in results:
        ts = datetime.fromtimestamp(r["first_message_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        line = (
            f"[{ts}] **{r['title'] or 'Untitled'}**\n"
            f"  {r['source']} | {r['project_name'] or 'N/A'} | "
            f"{r['user_message_count']} user msgs | tier: {r['tier']}"
        )
        if r.get("summary"):
            line += f"\n  {r['summary'][:150]}..."
        output.append(line)
    return "\n\n".join(output)


def _do_project_context(project_path: str) -> str:
    _auto_refresh()
    db = get_db()
    l1_text = select_l1_context(db, project_path, budget_tokens=2000)

    sessions = db.list_sessions(project_path=project_path, limit=5)
    summary_lines = []
    for s in sessions:
        summary = db.get_summary(s["id"])
        if summary:
            ts = datetime.fromtimestamp(s["first_message_at"], tz=timezone.utc).strftime("%Y-%m-%d")
            summary_lines.append(
                f"### {s.get('title', 'Untitled')} ({ts})\n{summary['summary_text'][:300]}"
            )

    output_parts = []
    if l1_text:
        output_parts.append(l1_text)
    if summary_lines:
        output_parts.append("## Recent Sessions\n\n" + "\n\n".join(summary_lines))

    if not output_parts:
        return f"No accumulated knowledge for project: {project_path}"
    return "\n\n".join(output_parts)


def _do_recall_session(session_id: str) -> str:
    _auto_refresh()
    db = get_db()
    session = db.get_session(session_id)
    if not session:
        return f"Session not found: {session_id}"

    messages = db.get_session_messages(session_id)
    summary = db.get_summary(session_id)

    ts = datetime.fromtimestamp(session["first_message_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    output = [
        f"# Session: {session.get('title', 'Untitled')}",
        f"**Date**: {ts}",
        f"**Source**: {session['source']} | **Model**: {session.get('model', 'N/A')}",
        f"**Project**: {session.get('project_name', 'N/A')} ({session.get('cwd', 'N/A')})",
        f"**Messages**: {session['message_count']} ({session['user_message_count']} user)",
        f"**Tier**: {session['tier']}",
    ]

    if summary:
        output.append(f"\n## Summary\n{summary['summary_text']}")
        decisions = json.loads(summary.get("key_decisions", "[]"))
        if decisions:
            output.append("\n**Key Decisions**:")
            for d in decisions:
                output.append(f"- {d}")

    output.append("\n## Messages\n")
    for msg in messages[:100]:
        role = msg["role"]
        ctype = msg.get("content_type", "text")
        text = msg.get("content_text", "")
        if not text:
            continue
        if ctype == "thinking":
            continue
        elif ctype == "tool_call":
            tool = msg.get("tool_name", "?")
            output.append(f"**[{role} -> {tool}]**: {text[:300]}")
        elif ctype == "tool_result":
            output.append(f"**[tool result]**: {text[:200]}")
        else:
            output.append(f"**[{role}]**: {text[:500]}")

    if len(messages) > 100:
        output.append(f"\n... and {len(messages) - 100} more messages")

    return "\n\n".join(output)


def run_server():
    """Run the MCP server with all memory tools."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("Error: mcp package required. Install with: pip install 'life-long-memory[mcp]'")
        raise SystemExit(1)

    mcp = FastMCP("life-long-memory")

    # On startup: daily process if first use today, otherwise just ingest
    try:
        from src.auto import _should_run_daily, daily_auto_process_background, auto_ingest
        if _should_run_daily():
            daily_auto_process_background()
        else:
            auto_ingest(get_db())
    except Exception:
        pass  # best-effort; don't block server startup

    @mcp.tool()
    def memory_search(
        query: str,
        limit: int = 10,
        project: str | None = None,
        after: str | None = None,
    ) -> str:
        """Search across all coding sessions using hybrid search (keyword + recency + importance).

        Use this to find past sessions where you solved a specific problem, used a certain
        technique, or worked on a particular topic.

        Args:
            query: Natural language search query (e.g., "fix netplan permissions", "SQLite FTS5")
            limit: Maximum number of results to return
            project: Optional project path to filter by (e.g., "/Users/lingzhi/Code/apas")
            after: Optional ISO date to search after (e.g., "2026-02-01")
        """
        return _do_search(query, limit, project, after)

    @mcp.tool()
    def memory_timeline(
        project: str | None = None,
        after: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> str:
        """View a chronological timeline of coding sessions.

        Use this to understand the history of work on a project or across all projects.

        Args:
            project: Optional project path to filter by
            after: Optional ISO date for start of range (e.g., "2026-02-01")
            before: Optional ISO date for end of range
            limit: Maximum number of sessions to return
        """
        return _do_timeline(project, after, before, limit)

    @mcp.tool()
    def memory_project_context(project_path: str) -> str:
        """Get all accumulated knowledge for a project (L1 entries + recent summaries).

        Use this at the start of a session to understand what's been done before
        in this project and what patterns/conventions to follow.

        Args:
            project_path: The project directory path (e.g., "/Users/lingzhi/Code/apas")
        """
        return _do_project_context(project_path)

    @mcp.tool()
    def memory_recall_session(session_id: str) -> str:
        """Recall the full details of a specific session, including all messages.

        Use this to dig into the details of a past session identified via search.

        Args:
            session_id: The session UUID to recall
        """
        return _do_recall_session(session_id)

    mcp.run()
