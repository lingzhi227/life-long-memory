# Changelog

## 0.1.2 (2026-02-18)

Major update focused on multi-CLI support, one-command setup, and production stability.

### New Features

- **Gemini CLI support**: Full parser for `~/.gemini/tmp/` sessions, including tool calls and token counting. Gemini joins Claude Code and Codex as a supported source. (`0080e7d`)
- **One-command install**: `pip install ... && life-long-memory setup` detects CLIs, configures MCP, initializes the database, and ingests all sessions in a single 5-step flow. (`8728722`)
- **Codex CLI MCP configuration**: `setup` now writes TOML MCP config for Codex CLI at `~/.codex/config.toml`, alongside JSON configs for Claude Code and Gemini. (`55f9667`)
- **Multi-backend LLM routing**: `call_llm()` dispatches to the CLI that produced the session (Claude → `claude --print`, Codex → `codex exec`, Gemini → `gemini`). No API keys needed. (`0080e7d`)
- **Auto-processing pipeline**: `life-long-memory auto` runs ingest → summarize → promote in one step. Background `auto_process` runs on a 1-hour cooldown in the MCP server. (`0080e7d`)
- **`doctor` command**: Verifies binary paths, MCP configs, mcp package, database health, and stale projects. (`f0e6314`)
- **`prune` command**: Deletes L1 knowledge and data for stale/migrated project paths. `--knowledge-only` preserves sessions. (`8917867`)
- **`--backend` flag**: Force a specific LLM backend on `summarize`, `promote`, and `auto` commands (e.g., `--backend claude` to summarize codex sessions via Claude). (`46cb09f`)
- **`--limit` flag on `auto`**: Cap the number of sessions summarized per run to control LLM costs (e.g., `auto --limit 10`). (`8917867`)
- **LLM auto-fallback**: If the primary backend fails, automatically tries the next available CLI. (`7641e81`)
- **Auto-ingest on MCP startup**: The MCP server ingests new sessions on startup and on every tool call, so memory is always up to date. (`f860c69`)

### Bug Fixes

- **FTS search crash on hyphens**: Queries like `2025-12`, `o3-mini`, `step-1` crashed with `OperationalError: no such column`. Fixed by quoting each FTS5 search token. (`8917867`)
- **MCP binary path resolution**: MCP configs now use absolute binary paths (resolved via `shutil.which` or `sys.executable` fallback), preventing silent failures when `~/.local/bin` isn't in PATH. Stale paths are auto-fixed on re-run. (`f0e6314`)
- **Codex CLI `--ephemeral` flag**: Removed non-existent flag; Codex backend now uses `--json --full-auto` with proper JSON output parsing. (`46cb09f`)
- **Duplicate L1 knowledge entries**: Replaced clear-and-replace with fuzzy merge strategy (word-level Jaccard similarity ≥ 0.7). Matching entries get their `evidence_count` bumped instead of being recreated. (`f860c69`)
- **Empty session inflation**: Sessions with 0 user messages are now skipped during ingest. (`f860c69`)
- **Silent `summarize`/`promote`/`auto` commands**: Added `flush=True` to all print statements (fixes stdout buffering on remote machines). Commands now show per-item progress. (`7b06e64`)
- **`cmd_promote` crash**: Fixed wrong table name `summaries` → `session_summaries`. (`1899efa`)

### Improvements

- **Stale project detection**: `doctor` flags projects with no sessions in 30 days and suggests `prune`. `auto` and `auto_process` skip stale projects during promote to avoid wasting LLM calls. (`8917867`)
- **Rich command output**: `summarize` shows per-session detail with backend info. `promote` shows confirmed/new entry breakdown per project. `auto` shows per-step detail. (`7b06e64`)
- **`promote` returns richer results**: `promote_project_knowledge()` now returns `{entries, confirmed, new}` dict instead of a flat list. (`7b06e64`)
- **Architecture diagram**: SVG diagram added to README. (`9200db4`)

### Internal

- Flattened project structure: package directory renamed to `src/`. (`acd8850`, `a2a766f`)
- All 35 tests pass on Python 3.11+.

## 0.1.0 (2026-02-15)

Initial release.

- Three-tier memory model (L3 raw → L2 summaries → L1 knowledge)
- Claude Code and Codex CLI session parsers
- SQLite database with FTS5 full-text search
- Hybrid search (BM25 + recency + importance scoring)
- LLM-based session summarization and knowledge promotion
- Entity extraction (file paths, functions, errors, packages)
- MCP server with 4 tools: `memory_search`, `memory_timeline`, `memory_project_context`, `memory_recall_session`
- CLI commands: `ingest`, `search`, `timeline`, `stats`, `summarize`, `promote`, `recall`, `serve`
