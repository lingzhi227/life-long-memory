"""CLI interface for life-long memory."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.config import default_config
from src.db import MemoryDB
from src.entities import extract_entities_for_session
from src.parsers.codex import CodexParser
from src.parsers.claude_code import ClaudeCodeParser
from src.parsers.gemini import GeminiParser
from src.search import hybrid_search, timeline_search


def get_db() -> MemoryDB:
    config = default_config()
    db = MemoryDB(config.db_path)
    db.initialize()
    return db


def _run_ingest(
    db: MemoryDB,
    source: str | None = None,
    force: bool = False,
    verbose: bool = True,
    on_progress: callable | None = None,
) -> dict:
    """Shared ingest logic.

    Returns {sessions, messages, entities, skipped,
             per_source: [{name, files, new, existing}]}.

    When verbose=True, prints progress in cmd_ingest style.
    When on_progress is set, calls:
        on_progress(source_name, current, total, source_new, source_existing)
    every 10 files and at the end of each source.
    """
    config = default_config()

    sources = []
    if source in (None, "codex") and config.codex_enabled:
        sources.append(("codex", CodexParser(), config.codex_paths))
    if source in (None, "claude_code") and config.claude_code_enabled:
        sources.append(("claude_code", ClaudeCodeParser(), config.claude_code_paths))
    if source in (None, "gemini") and config.gemini_enabled:
        sources.append(("gemini", GeminiParser(), config.gemini_paths))

    total_sessions = 0
    total_messages = 0
    total_entities = 0
    total_skipped = 0
    per_source = []

    for source_name, parser, paths in sources:
        files = parser.discover_files(paths)
        source_new = 0
        source_existing = 0

        if verbose:
            print(f"\n[{source_name}] Found {len(files)} session files")

        for i, fpath in enumerate(files, 1):
            # Parse
            try:
                parsed = parser.parse(fpath)
            except Exception as e:
                if verbose:
                    print(f"  Error parsing {fpath.name}: {e}")
                parsed = None

            # Process
            if parsed:
                if db.session_exists(parsed.id) and not force:
                    source_existing += 1
                    total_skipped += 1
                else:
                    db.upsert_session(parsed.to_session_dict())
                    msg_dicts = [m.to_dict(parsed.id) for m in parsed.messages]
                    db.insert_messages(msg_dicts)
                    entity_count = extract_entities_for_session(db, parsed.id)
                    total_entities += entity_count
                    source_new += 1
                    total_sessions += 1
                    total_messages += len(parsed.messages)

            # Progress (always fires, regardless of skip/error)
            if i % 10 == 0 or i == len(files):
                if on_progress:
                    on_progress(source_name, i, len(files), source_new, source_existing)
                elif verbose:
                    print(
                        f"  [{source_name}] {i}/{len(files)} files processed "
                        f"({total_sessions} sessions, {total_messages} messages)"
                    )

        per_source.append({
            "name": source_name,
            "files": len(files),
            "new": source_new,
            "existing": source_existing,
        })

    return {
        "sessions": total_sessions,
        "messages": total_messages,
        "entities": total_entities,
        "skipped": total_skipped,
        "per_source": per_source,
    }


def cmd_ingest(args: argparse.Namespace) -> None:
    """Ingest sessions from configured sources."""
    db = get_db()
    result = _run_ingest(db, source=args.source, force=args.force, verbose=True)

    print(f"\nIngest complete:")
    print(f"  Sessions ingested: {result['sessions']}")
    print(f"  Sessions skipped (already exists): {result['skipped']}")
    print(f"  Messages stored: {result['messages']}")
    print(f"  Entities extracted: {result['entities']}")


def cmd_search(args: argparse.Namespace) -> None:
    """Search across sessions."""
    from src.auto import auto_ingest

    db = get_db()
    auto_ingest(db)
    query = " ".join(args.query)

    after_epoch = None
    if args.after:
        try:
            dt = datetime.fromisoformat(args.after).replace(tzinfo=timezone.utc)
            after_epoch = int(dt.timestamp())
        except ValueError:
            print(f"Invalid date: {args.after}")
            return

    results = hybrid_search(
        db, query,
        limit=args.limit,
        project_path=args.project,
        after=after_epoch,
    )

    if not results:
        print("No matching sessions found.")
        return

    for r in results:
        ts = datetime.fromtimestamp(r.first_message_at, tz=timezone.utc)
        print(f"\n{'='*60}")
        print(f"  {r.title or 'Untitled'}  (score: {r.score:.3f})")
        print(f"  Session: {r.session_id}")
        print(f"  Source: {r.source} | Project: {r.project_name or 'N/A'}")
        print(f"  Date: {ts.strftime('%Y-%m-%d %H:%M')}")
        if r.summary:
            print(f"  Summary: {r.summary[:200]}...")
        if r.matching_snippets:
            print(f"  Match: {r.matching_snippets[0][:150]}")


def cmd_timeline(args: argparse.Namespace) -> None:
    """Show session timeline."""
    from src.auto import auto_ingest

    db = get_db()
    auto_ingest(db)

    after_epoch = None
    before_epoch = None
    if args.after:
        try:
            dt = datetime.fromisoformat(args.after).replace(tzinfo=timezone.utc)
            after_epoch = int(dt.timestamp())
        except ValueError:
            pass
    if args.before:
        try:
            dt = datetime.fromisoformat(args.before).replace(tzinfo=timezone.utc)
            before_epoch = int(dt.timestamp())
        except ValueError:
            pass

    results = timeline_search(
        db,
        project_path=args.project,
        after=after_epoch,
        before=before_epoch,
        limit=args.limit,
    )

    if not results:
        print("No sessions found.")
        return

    for r in results:
        ts = datetime.fromtimestamp(r["first_message_at"], tz=timezone.utc)
        print(f"\n[{ts.strftime('%Y-%m-%d %H:%M')}] {r['title'] or 'Untitled'}")
        print(f"  {r['source']} | {r['project_name'] or 'N/A'} | "
              f"{r['user_message_count']} user msgs | tier: {r['tier']}")
        if r.get("summary"):
            print(f"  {r['summary'][:150]}...")


def cmd_stats(args: argparse.Namespace) -> None:
    """Show database statistics."""
    db = get_db()
    stats = db.stats()

    print(f"\nLife-Long Memory Statistics")
    print(f"{'='*40}")
    print(f"  Total sessions:   {stats['total_sessions']}")
    print(f"  Total messages:   {stats['total_messages']}")
    print(f"  Total entities:   {stats['total_entities']}")
    print(f"  Total summaries:  {stats['total_summaries']}")
    print(f"  Knowledge entries: {stats['total_knowledge_entries']}")
    print(f"\n  Sessions by source:")
    for source, count in stats.get("sessions_by_source", {}).items():
        print(f"    {source}: {count}")
    print(f"\n  Sessions by tier:")
    for tier, count in stats.get("sessions_by_tier", {}).items():
        print(f"    {tier}: {count}")
    if stats.get("jobs_by_status"):
        print(f"\n  Jobs by status:")
        for status, count in stats["jobs_by_status"].items():
            print(f"    {status}: {count}")

    print(f"\n  Database: {db.db_path}")
    if db.db_path.exists():
        size_mb = db.db_path.stat().st_size / (1024 * 1024)
        print(f"  Size: {size_mb:.1f} MB")


def cmd_summarize(args: argparse.Namespace) -> None:
    """Generate summaries for unsummarized sessions."""
    from src.summarize import summarize_session

    db = get_db()
    sessions = db.get_unsummarized_sessions(min_user_messages=3)

    if not sessions:
        print("No sessions need summarization.")
        return

    limit = args.limit or len(sessions)
    print(f"Found {len(sessions)} unsummarized sessions, processing {min(limit, len(sessions))}")

    count = 0
    for i, session in enumerate(sessions[:limit], 1):
        label = session.get('title', session['id'][:20])
        try:
            result = summarize_session(db, session["id"], model=args.model)
            if result:
                print(f"  [{i}/{min(limit, len(sessions))}] Summarized: {label}")
                count += 1
            else:
                print(f"  [{i}/{min(limit, len(sessions))}] Skipped (too short): {label}")
        except Exception as e:
            print(f"  [{i}/{min(limit, len(sessions))}] Error: {label}: {e}")
    print(f"\nSummarized {count} sessions.")


def cmd_promote(args: argparse.Namespace) -> None:
    """Promote L2 summaries to L1 project knowledge."""
    from src.promote import promote_project_knowledge

    db = get_db()

    if args.project:
        projects = [(args.project, None)]
    else:
        rows = db.conn.execute(
            "SELECT DISTINCT project_path, project_name FROM sessions WHERE project_path IS NOT NULL"
        ).fetchall()
        projects = [(r[0], r[1]) for r in rows]

    if not projects:
        print("No projects found.")
        return

    print(f"Promoting knowledge for {len(projects)} projects...")
    total = 0
    for project_path, project_name in projects:
        label = project_name or project_path
        try:
            entries = promote_project_knowledge(
                db, project_path, model=args.model
            )
            if entries:
                print(f"  [{label}] Promoted {len(entries)} knowledge entries")
                total += len(entries)
            else:
                print(f"  [{label}] No stable patterns found (need >= 2 summarized sessions)")
        except Exception as e:
            print(f"  [{label}] Error: {e}")
    print(f"\nPromote complete: {total} knowledge entries created.")


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the MCP server."""
    from src.mcp_server import run_server
    print("Starting life-long-memory MCP server...")
    run_server()


def cmd_recall(args: argparse.Namespace) -> None:
    """Recall a specific session."""
    from src.auto import auto_ingest

    db = get_db()
    auto_ingest(db)
    session = db.get_session(args.session_id)
    if not session:
        print(f"Session not found: {args.session_id}")
        return

    ts = datetime.fromtimestamp(session["first_message_at"], tz=timezone.utc)
    print(f"\n{'='*60}")
    print(f"  {session.get('title', 'Untitled')}")
    print(f"  Session: {session['id']}")
    print(f"  Date: {ts.strftime('%Y-%m-%d %H:%M')}")
    print(f"  Source: {session['source']} | Model: {session.get('model', 'N/A')}")
    print(f"  Project: {session.get('project_name', 'N/A')} ({session.get('cwd', 'N/A')})")
    print(f"  Messages: {session['message_count']} ({session['user_message_count']} user)")

    summary = db.get_summary(args.session_id)
    if summary:
        print(f"\n  Summary:\n  {summary['summary_text']}")
        decisions = json.loads(summary.get("key_decisions", "[]"))
        if decisions:
            print(f"\n  Key Decisions:")
            for d in decisions:
                print(f"    - {d}")

    if args.messages:
        messages = db.get_session_messages(args.session_id)
        print(f"\n{'─'*60}")
        for msg in messages[:50]:
            role = msg["role"]
            ctype = msg.get("content_type", "text")
            text = msg.get("content_text", "")
            if not text or ctype == "thinking":
                continue
            if ctype == "tool_call":
                tool = msg.get("tool_name", "?")
                print(f"  [{role} -> {tool}]: {text[:200]}")
            elif ctype == "tool_result":
                print(f"  [tool result]: {text[:100]}")
            else:
                print(f"  [{role}]: {text[:300]}")


def cmd_auto(args: argparse.Namespace) -> None:
    """Run full pipeline: ingest → summarize → promote."""
    from src.auto import auto_process

    start = time.time()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] Running auto pipeline: ingest → summarize → promote")

    result = auto_process(model=args.model, force=True)

    elapsed = time.time() - start
    ts_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts_end}] Auto pipeline complete in {elapsed:.1f}s")
    print(f"  Ingested: {result['ingested']} sessions")
    print(f"  Summarized: {result['summarized']} sessions")
    print(f"  Promoted: {result['promoted']} knowledge entries")


# CLI tool definitions: (binary name, display name, session dirs, MCP config details)
CLI_TOOLS = [
    {
        "binary": "claude",
        "name": "Claude Code",
        "session_dir": Path.home() / ".claude" / "projects",
        "mcp_path": Path.home() / ".claude" / ".mcp.json",
        "mcp_key": "mcpServers",
    },
    {
        "binary": "codex",
        "name": "Codex CLI",
        "session_dir": Path.home() / ".codex" / "sessions",
        "mcp_path": None,  # Codex doesn't support MCP config files
        "mcp_key": None,
    },
    {
        "binary": "gemini",
        "name": "Gemini CLI",
        "session_dir": Path.home() / ".gemini" / "tmp",
        "mcp_path": Path.home() / ".gemini" / "settings.json",
        "mcp_key": "mcpServers",
    },
]


def _count_files(directory: Path) -> int:
    """Count files recursively in a directory."""
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.rglob("*") if _.is_file())


def _configure_mcp_claude(mcp_path: Path) -> str:
    """Configure MCP for Claude Code. Returns status message."""
    config = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
    servers = config.setdefault("mcpServers", {})
    if "life-long-memory" in servers:
        return "already configured"
    servers["life-long-memory"] = {
        "command": "life-long-memory",
        "args": ["serve"],
    }
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(config, indent=2) + "\n")
    return "added"


def _configure_mcp_gemini(mcp_path: Path) -> str:
    """Configure MCP for Gemini CLI. Returns status message."""
    config = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
    servers = config.setdefault("mcpServers", {})
    if "life-long-memory" in servers:
        return "already configured"
    servers["life-long-memory"] = {
        "command": "life-long-memory",
        "args": ["serve"],
        "trust": True,
    }
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(config, indent=2) + "\n")
    return "added"


def cmd_setup(args: argparse.Namespace) -> None:
    """Auto-configure life-long-memory: detect CLIs, init DB, configure MCP, ingest."""
    start = time.time()

    print("\n  Life-Long Memory")
    print("  =================\n")

    # Step 1/5: Detect CLI tools
    print("  [1/5] Detecting CLI tools...")
    detected = {}
    for tool in CLI_TOOLS:
        found = shutil.which(tool["binary"]) is not None
        detected[tool["binary"]] = found
        if found:
            print(f"        \u2713 {tool['name']}")
        else:
            print(f"        \u2717 {tool['name']} (not installed)")

    # Step 2/5: Scan session directories
    print("\n  [2/5] Scanning session directories...")
    for tool in CLI_TOOLS:
        d = tool["session_dir"]
        if d.exists():
            count = _count_files(d)
            print(f"        \u2713 {d.name}/ ({count} files)")
        else:
            print(f"        \u2717 {d.name}/ (not found)")

    # Step 3/5: Initialize database
    config = default_config()
    db_existed = config.db_path.exists()
    db = get_db()
    db_status = "exists" if db_existed else "created"
    print(f"\n  [3/5] Database: {config.db_path} ({db_status})")

    # Step 4/5: Configure MCP
    print("\n  [4/5] Configuring MCP servers...")
    if not args.no_mcp:
        # Claude Code
        claude_tool = CLI_TOOLS[0]
        if detected["claude"]:
            status = _configure_mcp_claude(claude_tool["mcp_path"])
            print(f"        \u2713 Claude Code: {status}")
        else:
            print(f"        \u2014 Claude Code: skipped (not installed)")

        # Gemini CLI
        gemini_tool = CLI_TOOLS[2]
        if detected["gemini"]:
            status = _configure_mcp_gemini(gemini_tool["mcp_path"])
            print(f"        \u2713 Gemini CLI: {status}")
        else:
            print(f"        \u2014 Gemini CLI: skipped (not installed)")
    else:
        print("        skipped (--no-mcp)")

    # Step 5/5: Ingest sessions
    print("\n  [5/5] Ingesting sessions...")

    # Track which source we're currently printing progress for
    _current_source = [None]

    def _setup_progress(source_name, current, total, new, existing):
        if source_name != _current_source[0]:
            # New source — print header
            if _current_source[0] is not None:
                print()  # newline after previous source's final line
            print(f"        {source_name}: {total} files found")
            _current_source[0] = source_name
        # Overwrite progress line; show new/existing on final line
        if current == total:
            print(f"\r          {current}/{total} processed ({new} new, {existing} existing)", flush=True)
        else:
            print(f"\r          {current}/{total} processed", end="", flush=True)

    result = _run_ingest(db, source=None, force=False, verbose=False, on_progress=_setup_progress)

    if not any(s["files"] > 0 for s in result["per_source"]):
        print("        No session files found")

    # Done
    elapsed = time.time() - start
    print(f"\n  \u2713 Done! ({elapsed:.1f}s)")
    print(f"    {result['sessions']} sessions ingested, {result['messages']} messages indexed")
    print(f"    Restart your CLI tool to activate MCP memory tools.")
    print(f"    Try: life-long-memory search \"your query here\"")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="life-long-memory",
        description="Lifelong context memory for CLI agents",
    )
    sub = parser.add_subparsers(dest="command")

    # ingest
    p_ingest = sub.add_parser("ingest", help="Ingest sessions from CLI tools")
    p_ingest.add_argument("--source", choices=["codex", "claude_code", "gemini"], help="Only ingest from this source")
    p_ingest.add_argument("--force", action="store_true", help="Re-ingest already processed sessions")

    # search
    p_search = sub.add_parser("search", help="Search across sessions")
    p_search.add_argument("query", nargs="+", help="Search query")
    p_search.add_argument("--project", help="Filter by project path")
    p_search.add_argument("--after", help="Filter by date (ISO format)")
    p_search.add_argument("--limit", type=int, default=10)

    # timeline
    p_timeline = sub.add_parser("timeline", help="Show session timeline")
    p_timeline.add_argument("--project", help="Filter by project path")
    p_timeline.add_argument("--after", help="Start date (ISO)")
    p_timeline.add_argument("--before", help="End date (ISO)")
    p_timeline.add_argument("--limit", type=int, default=20)

    # stats
    sub.add_parser("stats", help="Show database statistics")

    # summarize
    p_summarize = sub.add_parser("summarize", help="Generate session summaries")
    p_summarize.add_argument("--limit", type=int, help="Max sessions to summarize")
    p_summarize.add_argument("--model", default=None, help="Model override (default: auto per backend)")

    # promote
    p_promote = sub.add_parser("promote", help="Promote L2 summaries to L1 knowledge")
    p_promote.add_argument("--project", help="Only promote for this project path")
    p_promote.add_argument("--model", default=None, help="Model override (default: auto per backend)")

    # serve
    sub.add_parser("serve", help="Start MCP server")

    # recall
    p_recall = sub.add_parser("recall", help="Recall a specific session")
    p_recall.add_argument("session_id", help="Session UUID")
    p_recall.add_argument("--messages", action="store_true", help="Show messages")

    # auto
    p_auto = sub.add_parser("auto", help="Run full pipeline: ingest → summarize → promote")
    p_auto.add_argument("--model", default=None, help="Model override for summarize & promote")

    # setup
    p_setup = sub.add_parser("setup", help="Auto-configure: detect CLIs, init DB, configure MCP")
    p_setup.add_argument("--no-mcp", action="store_true", help="Skip MCP configuration")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "ingest": cmd_ingest,
        "search": cmd_search,
        "timeline": cmd_timeline,
        "stats": cmd_stats,
        "summarize": cmd_summarize,
        "promote": cmd_promote,
        "serve": cmd_serve,
        "recall": cmd_recall,
        "auto": cmd_auto,
        "setup": cmd_setup,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
