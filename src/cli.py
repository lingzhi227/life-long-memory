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
                if parsed.user_message_count == 0:
                    pass  # skip trivially empty sessions
                elif db.session_exists(parsed.id) and not force:
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
    backend = getattr(args, "backend", None)
    n = min(limit, len(sessions))
    backend_info = f" (backend: {backend})" if backend else ""
    print(f"Found {len(sessions)} unsummarized sessions, processing {n}{backend_info}", flush=True)

    count = 0
    skipped = 0
    errors = 0
    for i, session in enumerate(sessions[:limit], 1):
        sid = session['id'][:12]
        source = session.get('source', '?')
        msgs = session.get('message_count', 0)
        try:
            result = summarize_session(db, session["id"], model=args.model, backend=backend)
            if result:
                words = len(result.get('summary_text', '').split())
                print(f"  [{i}/{n}] \u2713 {sid} ({source}, {msgs} msgs) \u2192 {words} word summary", flush=True)
                count += 1
            else:
                print(f"  [{i}/{n}] \u2014 {sid} ({source}, {msgs} msgs) skipped (too short)", flush=True)
                skipped += 1
        except Exception as e:
            print(f"  [{i}/{n}] \u2717 {sid} ({source}): {e}", flush=True)
            errors += 1
    parts = []
    if skipped:
        parts.append(f"{skipped} skipped")
    if errors:
        parts.append(f"{errors} errors")
    suffix = f" ({', '.join(parts)})" if parts else ""
    print(f"\nSummarized {count} sessions{suffix}.", flush=True)


def cmd_promote(args: argparse.Namespace) -> None:
    """Promote L2 summaries to L1 project knowledge."""
    from src.promote import promote_project_knowledge

    db = get_db()
    backend = getattr(args, "backend", None)

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

    backend_info = f" (backend: {backend})" if backend else ""
    print(f"Promoting knowledge for {len(projects)} project(s){backend_info}...", flush=True)
    total_confirmed = 0
    total_new = 0
    for project_path, project_name in projects:
        label = project_name or project_path
        # Count summaries available for this project
        summarized = db.conn.execute(
            "SELECT COUNT(*) FROM session_summaries s JOIN sessions ss ON s.session_id = ss.id "
            "WHERE ss.project_path = ?", (project_path,)
        ).fetchone()[0]
        try:
            result = promote_project_knowledge(
                db, project_path, model=args.model, backend=backend
            )
            entries = result["entries"]
            confirmed = result["confirmed"]
            new = result["new"]
            if entries:
                print(f"  \u2713 {label} ({summarized} summaries): confirmed {confirmed} existing, added {new} new", flush=True)
                total_confirmed += confirmed
                total_new += new
            else:
                print(f"  \u2014 {label} ({summarized} summaries): no stable patterns found", flush=True)
        except Exception as e:
            print(f"  \u2717 {label}: {e}", flush=True)

    total = total_confirmed + total_new
    all_entries = db.conn.execute(
        "SELECT COUNT(*) FROM project_knowledge"
    ).fetchone()[0]
    print(f"\nDone. Confirmed {total_confirmed}, added {total_new} entries. {all_entries} total L1 entries.", flush=True)


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
    from src.auto import auto_ingest
    from src.summarize import summarize_session
    from src.promote import promote_project_knowledge

    backend = getattr(args, "backend", None)
    start = time.time()
    ts = datetime.now().strftime("%Y-%m-%d")
    print(f"[{ts}] Running auto pipeline: ingest \u2192 summarize \u2192 promote", flush=True)

    db = get_db()

    # 1. Ingest
    ingest_stats = auto_ingest(db)
    print(f"  Ingested: {ingest_stats['sessions']} new sessions", flush=True)

    # 2. Summarize
    sessions = db.get_unsummarized_sessions(min_user_messages=3)
    limit = getattr(args, "limit", None)
    to_process = sessions[:limit] if limit else sessions
    summarized = 0
    sum_errors = 0
    for session in to_process:
        try:
            result = summarize_session(db, session["id"], model=args.model, backend=backend)
            if result:
                summarized += 1
        except Exception:
            sum_errors += 1

    backend_info = f" (via {backend} backend)" if backend else ""
    error_info = f", {sum_errors} errors" if sum_errors else ""
    limit_info = f" of {len(sessions)}" if limit and limit < len(sessions) else ""
    print(f"  Summarized: {summarized}{limit_info} sessions{backend_info}{error_info}", flush=True)

    # 3. Promote (skip projects with no sessions in last 30 days)
    thirty_days_ago = int(time.time()) - 30 * 86400
    rows = db.conn.execute(
        "SELECT DISTINCT project_path, project_name FROM sessions "
        "WHERE project_path IS NOT NULL AND last_message_at >= ?",
        (thirty_days_ago,),
    ).fetchall()
    total_confirmed = 0
    total_new = 0
    project_count = 0
    for project_path, _project_name in rows:
        try:
            result = promote_project_knowledge(db, project_path, model=args.model, backend=backend)
            if result["entries"]:
                total_confirmed += result["confirmed"]
                total_new += result["new"]
                project_count += 1
        except Exception:
            pass

    print(f"  Promoted: {project_count} projects (confirmed {total_confirmed}, added {total_new} entries)", flush=True)

    # Mark cooldown
    from src.auto import _mark_full_run
    _mark_full_run()

    elapsed = time.time() - start
    print(f"Done. ({elapsed:.1f}s)", flush=True)


# CLI tool definitions: (binary name, display name, session dirs, MCP config details)
CLI_TOOLS = [
    {
        "binary": "claude",
        "name": "Claude Code",
        "session_dir": Path.home() / ".claude" / "projects",
        "mcp_config": "json",
        "mcp_path": Path.home() / ".claude" / ".mcp.json",
    },
    {
        "binary": "codex",
        "name": "Codex CLI",
        "session_dir": Path.home() / ".codex" / "sessions",
        "mcp_config": "toml",
        "mcp_path": Path.home() / ".codex" / "config.toml",
    },
    {
        "binary": "gemini",
        "name": "Gemini CLI",
        "session_dir": Path.home() / ".gemini" / "tmp",
        "mcp_config": "json",
        "mcp_path": Path.home() / ".gemini" / "settings.json",
    },
]


def _find_binary() -> str:
    """Find absolute path to the life-long-memory binary.

    Tries shutil.which first, then falls back to the bin dir next to
    sys.executable (handles pip install --user where ~/.local/bin isn't
    in PATH).  Returns absolute path string, or raises RuntimeError.
    """
    name = "life-long-memory"
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())

    # Fallback: same directory as the running Python interpreter
    candidate = Path(sys.executable).resolve().parent / name
    if candidate.exists():
        return str(candidate)

    raise RuntimeError(
        f"Cannot find '{name}' binary. pip may have installed it to a "
        f"directory not in $PATH. Try:\n"
        f"  export PATH=\"{Path(sys.executable).parent}:$PATH\"\n"
        f"then re-run: life-long-memory setup"
    )


def _count_files(directory: Path) -> int:
    """Count files recursively in a directory."""
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.rglob("*") if _.is_file())


def _configure_mcp_claude(mcp_path: Path, binary: str) -> str:
    """Configure MCP for Claude Code. Returns status message."""
    config = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
    servers = config.setdefault("mcpServers", {})
    if "life-long-memory" in servers:
        # Update binary path if it changed (e.g. was bare name, now absolute)
        existing_cmd = servers["life-long-memory"].get("command", "")
        if existing_cmd != binary:
            servers["life-long-memory"]["command"] = binary
            mcp_path.write_text(json.dumps(config, indent=2) + "\n")
            return "updated (fixed path)"
        return "already configured"
    servers["life-long-memory"] = {
        "command": binary,
        "args": ["serve"],
    }
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(config, indent=2) + "\n")
    return "added"


def _configure_mcp_codex(mcp_path: Path, binary: str) -> str:
    """Configure MCP for Codex CLI (TOML config). Returns status message."""
    import tomllib

    if mcp_path.exists():
        with open(mcp_path, "rb") as f:
            config = tomllib.load(f)
        existing_server = config.get("mcp_servers", {}).get("life-long-memory")
        if existing_server:
            existing_cmd = existing_server.get("command", "")
            if existing_cmd != binary:
                # Re-read as text and replace the command line
                text = mcp_path.read_text()
                text = text.replace(
                    f'command = "{existing_cmd}"',
                    f'command = "{binary}"',
                )
                mcp_path.write_text(text)
                return "updated (fixed path)"
            return "already configured"
        # Append new section to preserve existing formatting/comments
        existing = mcp_path.read_text()
        if not existing.endswith("\n"):
            existing += "\n"
        existing += (
            f'\n[mcp_servers.life-long-memory]\n'
            f'command = "{binary}"\n'
            f'args = ["serve"]\n'
        )
        mcp_path.write_text(existing)
    else:
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(
            f'[mcp_servers.life-long-memory]\n'
            f'command = "{binary}"\n'
            f'args = ["serve"]\n'
        )
    return "added"


def _configure_mcp_gemini(mcp_path: Path, binary: str) -> str:
    """Configure MCP for Gemini CLI. Returns status message."""
    config = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
    servers = config.setdefault("mcpServers", {})
    if "life-long-memory" in servers:
        existing_cmd = servers["life-long-memory"].get("command", "")
        if existing_cmd != binary:
            servers["life-long-memory"]["command"] = binary
            mcp_path.write_text(json.dumps(config, indent=2) + "\n")
            return "updated (fixed path)"
        return "already configured"
    servers["life-long-memory"] = {
        "command": binary,
        "args": ["serve"],
        "trust": True,
    }
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(config, indent=2) + "\n")
    return "added"


# Dispatch table for MCP configuration
_MCP_CONFIGURATORS = {
    "claude": _configure_mcp_claude,
    "codex": _configure_mcp_codex,
    "gemini": _configure_mcp_gemini,
}


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
        try:
            binary_path = _find_binary()
        except RuntimeError as e:
            print(f"        \u2717 {e}")
            print("        MCP configuration skipped (binary not found)")
            binary_path = None

        if binary_path:
            print(f"        Binary: {binary_path}")
            for tool in CLI_TOOLS:
                cli_name = tool["binary"]
                if detected[cli_name]:
                    configurator = _MCP_CONFIGURATORS[cli_name]
                    status = configurator(tool["mcp_path"], binary_path)
                    print(f"        \u2713 {tool['name']}: {status}")
                else:
                    print(f"        \u2014 {tool['name']}: skipped (not installed)")
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
    print()
    print(f"  Next steps:")
    print(f"    1. life-long-memory doctor        # verify everything works")
    print(f"    2. life-long-memory auto           # generate summaries & knowledge (uses LLM)")
    print(f"    3. Restart your CLI tool            # activate MCP memory tools")


def cmd_doctor(args: argparse.Namespace) -> None:
    """Verify installation health: binary paths, MCP config, DB, pipeline status."""
    ok = True

    print("\n  Life-Long Memory Doctor")
    print("  ========================\n")

    # 1. Check binary
    print("  [Binary]")
    try:
        binary_path = _find_binary()
        print(f"    \u2713 {binary_path}")
    except RuntimeError:
        print(f"    \u2717 life-long-memory not found in PATH")
        print(f"      Python bin dir: {Path(sys.executable).parent}")
        ok = False
        binary_path = None

    # 2. Check MCP configs
    print("\n  [MCP Configs]")
    for tool in CLI_TOOLS:
        mcp_path = tool["mcp_path"]
        name = tool["name"]
        fmt = tool["mcp_config"]
        if not mcp_path.exists():
            if shutil.which(tool["binary"]):
                print(f"    \u2717 {name}: config missing ({mcp_path})")
                ok = False
            else:
                print(f"    \u2014 {name}: not installed")
            continue

        # Parse and check command path
        try:
            if fmt == "toml":
                import tomllib
                with open(mcp_path, "rb") as f:
                    cfg = tomllib.load(f)
                server = cfg.get("mcp_servers", {}).get("life-long-memory")
            else:
                cfg = json.loads(mcp_path.read_text())
                server = cfg.get("mcpServers", {}).get("life-long-memory")

            if not server:
                print(f"    \u2717 {name}: life-long-memory not in config ({mcp_path})")
                ok = False
                continue

            cmd = server.get("command", "")
            cmd_resolves = shutil.which(cmd) is not None or Path(cmd).exists()
            if cmd_resolves:
                print(f"    \u2713 {name}: {cmd}")
            else:
                print(f"    \u2717 {name}: command not found: {cmd}")
                if binary_path:
                    print(f"      Fix: run 'life-long-memory setup' to update path")
                ok = False
        except Exception as e:
            print(f"    \u2717 {name}: error reading config: {e}")
            ok = False

    # 3. Check MCP server can import
    print("\n  [MCP Server]")
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401
        print(f"    \u2713 mcp package installed")
    except ImportError:
        print(f"    \u2717 mcp package not installed")
        print(f"      Fix: pip install 'life-long-memory[mcp]'")
        ok = False

    # 4. Check database
    print("\n  [Database]")
    config = default_config()
    if config.db_path.exists():
        db = get_db()
        stats = db.stats()
        size_mb = config.db_path.stat().st_size / (1024 * 1024)
        print(f"    \u2713 {config.db_path} ({size_mb:.1f} MB)")
        print(f"    Sessions: {stats['total_sessions']}")
        by_tier = stats.get("sessions_by_tier", {})
        l3 = by_tier.get("L3", 0)
        l2 = by_tier.get("L2", 0)
        print(f"    Tiers: {l3} L3, {l2} L2")
        print(f"    Summaries: {stats['total_summaries']}")
        print(f"    Knowledge: {stats['total_knowledge_entries']} entries")
        if stats['total_sessions'] == 0:
            print(f"    \u26a0 No sessions — run 'life-long-memory setup' to ingest")
        elif stats['total_summaries'] == 0:
            print(f"    \u26a0 No summaries — run 'life-long-memory auto' to generate")
    else:
        print(f"    \u2717 {config.db_path} (not found)")
        print(f"      Fix: run 'life-long-memory setup'")
        ok = False

    # 5. Check for stale projects
    if config.db_path.exists():
        print("\n  [Stale Projects]")
        thirty_days_ago = int(time.time()) - 30 * 86400
        stale_rows = db.conn.execute(
            """SELECT pk.project_path, COUNT(*) as entry_count,
                      MAX(s.last_message_at) as last_active
            FROM project_knowledge pk
            LEFT JOIN sessions s ON s.project_path = pk.project_path
            WHERE pk.superseded_by IS NULL
            GROUP BY pk.project_path
            HAVING last_active IS NULL OR last_active < ?""",
            (thirty_days_ago,),
        ).fetchall()
        if stale_rows:
            for row in stale_rows:
                path = row[0]
                entries = row[1]
                last = row[2]
                if last:
                    last_dt = datetime.fromtimestamp(last, tz=timezone.utc).strftime("%Y-%m-%d")
                    print(f"    \u26a0 {path}: {entries} L1 entries, no sessions since {last_dt}")
                else:
                    print(f"    \u26a0 {path}: {entries} L1 entries, no sessions found")
                print(f"      Run: life-long-memory prune --project \"{path}\"")
        else:
            print("    \u2713 No stale projects")

    # Verdict
    print()
    if ok:
        print("  \u2713 All checks passed. MCP memory tools should work on next CLI restart.")
    else:
        print("  \u2717 Issues found. Fix the problems above, then run doctor again.")


def cmd_prune(args: argparse.Namespace) -> None:
    """Delete L1 knowledge (and optionally sessions) for a project path."""
    db = get_db()
    project_path = args.project

    # Show what will be deleted
    knowledge = db.get_project_knowledge(project_path)
    sessions = db.list_sessions(project_path=project_path, limit=1000)

    if not knowledge and not sessions:
        print(f"No data found for: {project_path}")
        return

    print(f"Project: {project_path}")
    print(f"  L1 knowledge entries: {len(knowledge)}")
    print(f"  Sessions: {len(sessions)}")

    if args.knowledge_only:
        deleted = db.clear_project_knowledge(project_path)
        print(f"\nDeleted {deleted} L1 knowledge entries.")
    else:
        result = db.delete_project_data(project_path)
        print(f"\nDeleted: {result['knowledge']} L1 entries, "
              f"{result['summaries']} summaries, "
              f"{result['sessions']} sessions, "
              f"{result['messages']} messages")


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
    p_summarize.add_argument("--backend", choices=["claude", "codex", "gemini"], help="Force a specific LLM backend")

    # promote
    p_promote = sub.add_parser("promote", help="Promote L2 summaries to L1 knowledge")
    p_promote.add_argument("--project", help="Only promote for this project path")
    p_promote.add_argument("--model", default=None, help="Model override (default: auto per backend)")
    p_promote.add_argument("--backend", choices=["claude", "codex", "gemini"], help="Force a specific LLM backend")

    # serve
    sub.add_parser("serve", help="Start MCP server")

    # recall
    p_recall = sub.add_parser("recall", help="Recall a specific session")
    p_recall.add_argument("session_id", help="Session UUID")
    p_recall.add_argument("--messages", action="store_true", help="Show messages")

    # auto
    p_auto = sub.add_parser("auto", help="Run full pipeline: ingest → summarize → promote")
    p_auto.add_argument("--limit", type=int, default=None, help="Max sessions to summarize per run")
    p_auto.add_argument("--model", default=None, help="Model override for summarize & promote")
    p_auto.add_argument("--backend", choices=["claude", "codex", "gemini"], help="Force a specific LLM backend")

    # setup
    p_setup = sub.add_parser("setup", help="Auto-configure: detect CLIs, init DB, configure MCP")
    p_setup.add_argument("--no-mcp", action="store_true", help="Skip MCP configuration")

    # doctor
    sub.add_parser("doctor", help="Verify installation: binary paths, MCP config, DB health")

    # prune
    p_prune = sub.add_parser("prune", help="Delete data for a stale project path")
    p_prune.add_argument("--project", required=True, help="Project path to prune")
    p_prune.add_argument("--knowledge-only", action="store_true", help="Only delete L1 knowledge entries, keep sessions")

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
        "doctor": cmd_doctor,
        "prune": cmd_prune,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
