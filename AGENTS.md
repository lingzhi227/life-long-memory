# AGENTS.md

Instructions for AI agents working with the Life-Long Memory codebase.

## What This Project Does

Life-Long Memory is a **persistent memory layer** for CLI coding agents ([Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), [Gemini CLI](https://github.com/google-gemini/gemini-cli)). It solves the "every session starts from scratch" problem by:

1. **Ingesting** raw session transcripts from all three CLI tools into a local SQLite database
2. **Summarizing** each session into structured knowledge (key decisions, files touched, commands run) using the same CLI that produced it
3. **Consolidating** cross-session patterns into stable per-project knowledge with confidence scores
4. **Exposing** everything via 4 [MCP](https://modelcontextprotocol.io/) tools that agents can query in real-time

No API keys needed — all LLM calls go through locally installed CLI subprocesses.

## Architecture

```
src/
  cli.py            # CLI entry point — all user-facing commands
  db.py             # SQLite layer — schema, CRUD, FTS5 search
  config.py         # Configuration dataclass with defaults
  search.py         # Hybrid search: BM25 (0.5) + recency (0.25) + importance (0.25)
  summarize.py      # L3->L2: per-session LLM summarization
  promote.py        # L2->L1: cross-session knowledge consolidation (fuzzy merge)
  llm.py            # LLM dispatch: claude --print, codex exec, gemini (auto-fallback)
  auto.py           # Auto-processing pipeline with 1-hour cooldown
  entities.py       # Regex entity extraction (files, functions, errors)
  mcp_server.py     # MCP server with 4 tools, auto-ingests on startup
  parsers/
    base.py          # Abstract BaseParser + ParsedSession/ParsedMessage
    claude_code.py   # Parser for ~/.claude/projects/ JSONL files
    codex.py         # Parser for ~/.codex/sessions/ JSONL files
    gemini.py        # Parser for ~/.gemini/tmp/ JSON files
```

### Data Flow

```
Session files ─(ingest)─> L3 Raw ─(summarize)─> L2 Summaries ─(promote)─> L1 Knowledge
                            │                        │                         │
                       FTS5 indexed            LLM-generated           Fuzzy-merged with
                       in messages          per-session via CLI      confidence + evidence
```

## Key Design Decisions

1. **Zero external dependencies** for core functionality. Optional: `mcp[cli]` for MCP server, `anthropic`/`openai` for direct API access.

2. **LLM calls use locally installed CLI subprocesses** (`claude --print`, `codex exec`, `gemini`) instead of API SDKs. Source-aware routing sends each session to the CLI that produced it. Auto-fallback on failure. The `CLAUDECODE` env var must be unset for nested Claude invocation (handled in `llm.py`).

3. **Three-tier memory model** — L3 (raw, FTS5-indexed) → L2 (LLM-summarized) → L1 (cross-session knowledge with confidence scores).

4. **SQLite-only storage** in a single file (`~/.tactical/memory.sqlite`). No external databases.

5. **Fuzzy merge for L1 knowledge** — `promote.py` uses word-level Jaccard similarity (threshold >= 0.7) to match existing entries instead of clear-and-replace. Matching entries get `evidence_count` bumped.

6. **MCP configs use absolute binary paths** — `setup` resolves the binary via `shutil.which` or `sys.executable` fallback and writes absolute paths to prevent PATH issues on remote/HPC machines.

7. **FTS5 queries are escaped** — each search token is wrapped in double-quotes to prevent operator interpretation of hyphens, colons, etc. (e.g., `"2025-12"`, `"o3-mini"`).

## Common Tasks

### Adding a new CLI source parser

1. Create `parsers/new_source.py` implementing `BaseParser`
2. Implement `discover_files(paths)` and `parse(filepath)` returning `ParsedSession`
3. Register in `config.py` with enable flag and default paths
4. Add to `cli.py:_run_ingest()` sources list and `auto.py:auto_ingest()`
5. Add MCP configurator in `cli.py:_MCP_CONFIGURATORS` if the CLI supports MCP
6. Add to `CLI_TOOLS` list with binary name, session dir, and config format

### Adding an MCP tool

1. Add the function in `mcp_server.py` with `@mcp.tool()` decorator
2. Include clear docstring with Args descriptions (these become the tool schema)
3. Call `_auto_refresh()` at the start for auto-ingestion

### Modifying the database schema

1. Update `SCHEMA_SQL` in `db.py` with new `CREATE TABLE IF NOT EXISTS`
2. Add corresponding CRUD methods to `MemoryDB`
3. Add tests in `tests/test_core.py`

## Testing

```bash
python -m pytest tests/ -v    # 35 tests, all in-memory SQLite, no external services
```

## Environment Notes

- **Database**: `~/.tactical/memory.sqlite` (default, configurable)
- **MCP configs**: Claude Code at `~/.claude/.mcp.json`, Codex at `~/.codex/config.toml`, Gemini at `~/.gemini/settings.json`
- **Auto-process cooldown**: 1 hour, stored at `~/.tactical/.last_auto_run`
- **Stale project threshold**: 30 days of inactivity — `doctor` flags these, `auto` skips them during promote
