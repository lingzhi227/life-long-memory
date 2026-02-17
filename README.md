# Life-Long Memory

A lifelong context memory system for CLI coding agents. Automatically ingests, summarizes, and consolidates knowledge from your [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), and [Gemini CLI](https://github.com/google-gemini/gemini-cli) sessions, then exposes it via [MCP](https://modelcontextprotocol.io/) tools so your AI agent remembers what you've worked on across sessions.

<p align="center">
  <img src="assets/architecture.svg" alt="Life-Long Memory Architecture" width="100%">
</p>

## Why

CLI agents like Claude Code, Codex, and Gemini are powerful, but every session starts from scratch. Life-Long Memory solves this by:

- **Ingesting** past session transcripts from multiple CLI tools
- **Summarizing** them into structured knowledge using LLM
- **Consolidating** cross-session patterns into stable project knowledge
- **Exposing** everything via MCP tools your agent can query in real-time

## Three-Tier Memory Model

| Tier | Name | Scope | What it stores |
|------|------|-------|----------------|
| **L3** | Raw | All messages | Full conversation transcripts, FTS5 full-text indexed |
| **L2** | Summaries | Per-session | Key decisions, files touched, commands run, outcome |
| **L1** | Knowledge | Per-project | Stable patterns & architecture decisions with confidence scores |

```
L3 Raw Sessions ──(summarize)──▶ L2 Session Summaries ──(promote)──▶ L1 Project Knowledge
```

## Quick Start

```bash
pip install "life-long-memory[mcp] @ git+https://github.com/lingzhi227/life-long-memory.git" && life-long-memory setup
```

That's it. This single command installs the package, detects your CLI tools, configures MCP, and ingests all sessions. Restart your CLI tool to activate.

Or install from source:

```bash
git clone https://github.com/lingzhi227/life-long-memory.git
cd life-long-memory
pip install -e ".[mcp]" && life-long-memory setup
```

Use `--no-mcp` if you only want CLI access without MCP server configuration.

### MCP Tools

Once setup is complete and your CLI tool is restarted, four MCP tools become available:

| Tool | Description |
|------|-------------|
| `memory_search` | Hybrid search (keyword + recency + importance) across all sessions |
| `memory_timeline` | Chronological view of sessions, filterable by project and date |
| `memory_project_context` | L1 knowledge + recent summaries for a project |
| `memory_recall_session` | Full details of a specific session |

## Advanced Usage

### Manual Ingest

```bash
# Ingest from all configured sources (Claude Code + Codex + Gemini)
life-long-memory ingest

# Only ingest from one source
life-long-memory ingest --source claude_code
life-long-memory ingest --source codex
life-long-memory ingest --source gemini
```

### Generate Summaries (L3 -> L2)

```bash
# Summarize all unsummarized sessions
life-long-memory summarize

# Limit to N sessions
life-long-memory summarize --limit 20

# Use a specific model
life-long-memory summarize --model sonnet
```

### Promote Knowledge (L2 -> L1)

```bash
# Promote all projects
life-long-memory promote

# Only for a specific project
life-long-memory promote --project /path/to/project
```

### Full Pipeline

Run ingest, summarize, and promote in one step:

```bash
life-long-memory auto
```

### Search & Explore

```bash
# Search across all sessions
life-long-memory search "bioinformatics tool deployment"

# Filter by project
life-long-memory search "docker" --project /Users/me/Code/myproject

# View timeline
life-long-memory timeline --after 2025-11-01 --before 2025-12-01

# Recall a specific session
life-long-memory recall <session-uuid> --messages

# View stats
life-long-memory stats
```

### Manual MCP Configuration

If you prefer to configure MCP manually instead of using `life-long-memory setup`:

**Claude Code** — add to `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "life-long-memory": {
      "command": "life-long-memory",
      "args": ["serve"]
    }
  }
}
```

**Codex CLI** — add to `~/.codex/config.toml`:

```toml
[mcp_servers.life-long-memory]
command = "life-long-memory"
args = ["serve"]
```

**Gemini CLI** — add to `~/.gemini/settings.json` with `"trust": true` to bypass per-tool authorization prompts:

```json
{
  "mcpServers": {
    "life-long-memory": {
      "command": "life-long-memory",
      "args": ["serve"],
      "trust": true
    }
  }
}
```

## LLM Backend

Life-Long Memory uses locally installed CLI tools for all LLM calls (summarize, promote). No API keys are needed — each CLI handles its own authentication.

**Source-aware routing**: When summarizing a session, the system uses the same CLI tool that produced it. A Codex session gets summarized via `codex exec`, a Gemini session via `gemini`, and a Claude Code session via `claude --print`. If the source's CLI isn't installed, it falls back to any available CLI.

| Backend | CLI Command | Default Model |
|---------|-------------|---------------|
| Claude Code | `claude --print --model {m}` | `haiku` |
| Codex CLI | `codex exec -m {m}` | `o3` |
| Gemini CLI | `gemini --model {m}` | `gemini-2.5-flash` |

For knowledge promotion (which consolidates sessions from mixed sources), the dominant source's CLI is used.

The `--model` flag on `summarize` and `promote` commands overrides the backend default. If not specified, each backend picks its own fast/cheap model.

The `CLAUDECODE` environment variable is automatically cleared for Claude CLI subprocess invocations to allow nested usage.

## Database

All data is stored in a single SQLite file at `~/.tactical/memory.sqlite`.

**Tables:**
- `sessions` - Unified session metadata from all CLI tools
- `messages` - Normalized messages with FTS5 full-text search index
- `session_summaries` - L2 tier structured summaries
- `entities` - Extracted knowledge artifacts (file paths, functions, errors, packages)
- `project_knowledge` - L1 consolidated knowledge with confidence scores

## Hybrid Search

Search combines three signals:

```
score = fts_bm25 * 0.5 + recency * 0.25 + importance * 0.25
```

- **FTS BM25**: Full-text keyword relevance via SQLite FTS5
- **Recency**: Exponential decay with 30-day half-life
- **Importance**: Weighted by message count, user messages, tokens, compactions

## Supported Sources

| Source | Session Location | Parser |
|--------|-----------------|--------|
| Claude Code | `~/.claude/projects/{slug}/{uuid}.jsonl` | `parsers/claude_code.py` |
| Codex CLI | `~/.codex/sessions/{year}/{month}/{date}/rollout-*.jsonl` | `parsers/codex.py` |
| Gemini CLI | `~/.gemini/tmp/{projectHash}/chats/session-*.json` | `parsers/gemini.py` |

Adding a new source requires implementing the `SessionParser` interface (`parsers/base.py`).

## Project Structure

```
life-long-memory/
  src/                      # Python package
    __init__.py
    cli.py                  # CLI entry point (ingest, search, summarize, promote, ...)
    config.py               # Configuration & defaults
    db.py                   # SQLite schema, queries, FTS5
    search.py               # Hybrid search (BM25 + recency + importance)
    summarize.py            # L3->L2 session summarization via LLM
    promote.py              # L2->L1 cross-session knowledge consolidation
    llm.py                  # LLM invocation via CLI subprocesses (claude, codex, gemini)
    entities.py             # Regex-based entity extraction
    background.py           # Job queue for async processing
    mcp_server.py           # MCP server exposing memory tools
    parsers/
      base.py               # Abstract parser interface
      claude_code.py        # Claude Code session parser
      codex.py              # Codex CLI session parser
      gemini.py             # Gemini CLI session parser
  tests/
    test_core.py            # Unit tests (DB, entities, parsers, search)
  pyproject.toml
  README.md
  AGENTS.md
```

## License

MIT
