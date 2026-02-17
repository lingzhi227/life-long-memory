# AGENTS.md

Instructions for AI agents working with the Life-Long Memory codebase.

## Overview

Life-Long Memory is a lifelong context memory system for CLI coding agents (Claude Code, Codex CLI). It ingests session transcripts, summarizes them, consolidates cross-session knowledge, and exposes everything via MCP tools.

## Architecture

```
src/
  cli.py            # CLI entry point - all user-facing commands
  db.py             # SQLite layer - schema, CRUD, FTS5 search
  config.py         # Configuration dataclass with defaults
  search.py         # Hybrid search combining BM25 + recency + importance
  summarize.py      # L3->L2: per-session LLM summarization
  promote.py        # L2->L1: cross-session knowledge consolidation
  llm.py            # LLM calls via `claude --print` subprocess (no API keys)
  entities.py       # Regex entity extraction (files, functions, errors)
  background.py     # Job queue (unused in current CLI flow)
  mcp_server.py     # MCP server with 4 tools for agent integration
  parsers/
    base.py          # Abstract BaseParser interface + ParsedSession/ParsedMessage
    claude_code.py   # Parser for ~/.claude/projects/ JSONL files
    codex.py         # Parser for ~/.codex/sessions/ JSONL files
```

## Key Design Decisions

1. **Zero external dependencies** for core functionality. Optional deps for MCP server (`mcp[cli]`) and direct API access (`anthropic`, `openai`).

2. **LLM calls use `claude --print` subprocess** instead of API SDKs. This leverages the locally installed Claude Code CLI's OAuth authentication, so no `ANTHROPIC_API_KEY` is needed. The `CLAUDECODE` env var must be unset to allow nested invocation (see `llm.py`).

3. **Three-tier memory model**:
   - **L3** (raw): Full session transcripts, FTS5-indexed
   - **L2** (summarized): Structured summaries per session
   - **L1** (consolidated): Cross-session patterns per project, with confidence scores

4. **SQLite-only storage** in a single file (`~/.tactical/memory.sqlite`). No external databases.

5. **Hybrid search** combines FTS5 BM25 (0.5 weight), recency decay (0.25), and session importance (0.25).

## Common Tasks

### Adding a new CLI source parser

1. Create `parsers/new_source.py` implementing `BaseParser`
2. Implement `discover_files(paths)` to find session files
3. Implement `parse(filepath)` returning `ParsedSession`
4. Register in `cli.py:cmd_ingest()` sources list
5. Add to `config.py` with enable flag and default paths

### Adding an MCP tool

1. Add the core logic function `_do_xxx()` in `mcp_server.py`
2. Register it with `@mcp.tool()` decorator in `run_server()`
3. Include clear docstring with Args descriptions (these become the tool schema)

### Modifying the database schema

1. Update `db.py:MemoryDB.initialize()` with new `CREATE TABLE IF NOT EXISTS`
2. Add corresponding CRUD methods to `MemoryDB`
3. Add tests in `tests/test_core.py`

## Testing

```bash
python -m pytest tests/ -v
```

All tests use temporary in-memory SQLite databases for isolation. No external services needed.

## Environment Notes

- **Nested Claude invocation**: `llm.py` clears the `CLAUDECODE` env var before spawning `claude --print` subprocesses. This is required because Claude Code blocks nested sessions by default.
- **Database path**: `~/.tactical/memory.sqlite` (hardcoded default, configurable via `MemoryConfig.db_path`)
- **MCP config**: User-level at `~/.claude/.mcp.json`, project-level at `.mcp.json`
