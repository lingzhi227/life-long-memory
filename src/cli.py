"""CLI interface for life-long memory."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.config import default_config
from src.db import MemoryDB
from src.entities import extract_entities_for_session
from src.parsers.codex import CodexParser
from src.parsers.claude_code import ClaudeCodeParser
from src.search import hybrid_search, timeline_search


def get_db() -> MemoryDB:
    config = default_config()
    db = MemoryDB(config.db_path)
    db.initialize()
    return db


def cmd_ingest(args: argparse.Namespace) -> None:
    """Ingest sessions from configured sources."""
    config = default_config()
    db = get_db()

    sources = []
    if args.source in (None, "codex") and config.codex_enabled:
        sources.append(("codex", CodexParser(), config.codex_paths))
    if args.source in (None, "claude_code") and config.claude_code_enabled:
        sources.append(("claude_code", ClaudeCodeParser(), config.claude_code_paths))

    total_sessions = 0
    total_messages = 0
    total_entities = 0
    skipped = 0

    for source_name, parser, paths in sources:
        files = parser.discover_files(paths)
        print(f"\n[{source_name}] Found {len(files)} session files")

        for i, fpath in enumerate(files, 1):
            try:
                parsed = parser.parse(fpath)
            except Exception as e:
                print(f"  Error parsing {fpath.name}: {e}")
                continue

            if not parsed:
                continue

            if db.session_exists(parsed.id) and not args.force:
                skipped += 1
                continue

            db.upsert_session(parsed.to_session_dict())
            msg_dicts = [m.to_dict(parsed.id) for m in parsed.messages]
            db.insert_messages(msg_dicts)
            entity_count = extract_entities_for_session(db, parsed.id)
            total_entities += entity_count

            total_sessions += 1
            total_messages += len(parsed.messages)

            if i % 10 == 0 or i == len(files):
                print(
                    f"  [{source_name}] {i}/{len(files)} files processed "
                    f"({total_sessions} sessions, {total_messages} messages)"
                )

    print(f"\nIngest complete:")
    print(f"  Sessions ingested: {total_sessions}")
    print(f"  Sessions skipped (already exists): {skipped}")
    print(f"  Messages stored: {total_messages}")
    print(f"  Entities extracted: {total_entities}")


def cmd_search(args: argparse.Namespace) -> None:
    """Search across sessions."""
    db = get_db()
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
    db = get_db()

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
    db = get_db()
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="life-long-memory",
        description="Lifelong context memory for CLI agents",
    )
    sub = parser.add_subparsers(dest="command")

    # ingest
    p_ingest = sub.add_parser("ingest", help="Ingest sessions from CLI tools")
    p_ingest.add_argument("--source", choices=["codex", "claude_code"], help="Only ingest from this source")
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
    p_summarize.add_argument("--model", default="haiku", help="Model for summarization (default: haiku)")

    # promote
    p_promote = sub.add_parser("promote", help="Promote L2 summaries to L1 knowledge")
    p_promote.add_argument("--project", help="Only promote for this project path")
    p_promote.add_argument("--model", default="haiku", help="Model for promotion (default: haiku)")

    # serve
    sub.add_parser("serve", help="Start MCP server")

    # recall
    p_recall = sub.add_parser("recall", help="Recall a specific session")
    p_recall.add_argument("session_id", help="Session UUID")
    p_recall.add_argument("--messages", action="store_true", help="Show messages")

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
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
