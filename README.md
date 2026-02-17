# Life-Long Memory

A lifelong context memory system for CLI coding agents. Automatically ingests, summarizes, and consolidates knowledge from your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [Codex CLI](https://github.com/openai/codex) sessions, then exposes it via [MCP](https://modelcontextprotocol.io/) tools so your AI agent remembers what you've worked on across sessions.

## Why

CLI agents like Claude Code and Codex are powerful, but every session starts from scratch. Life-Long Memory solves this by:

- **Ingesting** past session transcripts from multiple CLI tools
- **Summarizing** them into structured knowledge using LLM
- **Consolidating** cross-session patterns into stable project knowledge
- **Exposing** everything via MCP tools your agent can query in real-time

## Architecture

```
                         You + CLI Agent
                              |
                    +---------+---------+
                    |                   |
              Claude Code            Codex CLI
                    |                   |
              ~/.claude/            ~/.codex/
              projects/             sessions/
                    |                   |
                    +-------+-----------+
                            |
                      life-long-memory
                         ingest
                            |
                    +-------+-------+
                    |               |
                 Parsers         SQLite DB
              (normalize)    (~/.tactical/memory.sqlite)
                    |               |
                    +-------+-------+
                            |
               +------------+------------+
               |            |            |
             L3 Raw    L2 Summary   L1 Knowledge
           (messages)  (per-session)  (per-project)
               |            |            |
               +------------+------------+
                            |
                       MCP Server
                     (life-long-memory serve)
                            |
                  +---------+---------+
                  |         |         |
              memory_   memory_   memory_
              search   timeline  project_
                                 context
```

## Three-Tier Memory Model

```
  +-----------------------------------------------------------------+
  |                                                                 |
  |  L1  Consolidated Knowledge                   (per-project)     |
  |  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~         |
  |  Stable patterns, preferences, architecture decisions,          |
  |  gotchas, and workflows extracted from multiple sessions.       |
  |  Only entries with confidence >= 0.5 are kept.                  |
  |                                                                 |
  |  Example:                                                       |
  |    [architecture] (0.95) MAS project uses standardized          |
  |      .tool_installer directory with Docker/BioContainers...     |
  |    [preference]   (0.85) Responds in Chinese when user          |
  |      writes in Chinese                                          |
  |                                                                 |
  +-----------------------------------------------------------------+
        ^  promote (cross-session LLM analysis)
        |
  +-----------------------------------------------------------------+
  |                                                                 |
  |  L2  Session Summaries                        (per-session)     |
  |  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~         |
  |  Structured summary of each session:                            |
  |    - summary_text (200-500 words)                               |
  |    - key_decisions                                               |
  |    - files_touched                                               |
  |    - commands_run                                                |
  |    - outcome (completed / partial / error)                      |
  |                                                                 |
  +-----------------------------------------------------------------+
        ^  summarize (per-session LLM analysis)
        |
  +-----------------------------------------------------------------+
  |                                                                 |
  |  L3  Raw Sessions                             (all messages)    |
  |  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~         |
  |  Full conversation transcripts normalized from multiple         |
  |  CLI tools. Includes user messages, assistant responses,        |
  |  tool calls, tool results, and thinking blocks.                 |
  |  FTS5 full-text indexed for keyword search.                     |
  |                                                                 |
  +-----------------------------------------------------------------+
```

## Installation

```bash
# Clone
git clone https://github.com/lingzhi227/life-long-memory.git
cd life-long-memory

# Install (editable)
pip install -e .

# With MCP server support
pip install -e ".[mcp]"
```

## Quick Start

### 1. Ingest your sessions

```bash
# Ingest from all configured sources (Claude Code + Codex)
life-long-memory ingest

# Only ingest from one source
life-long-memory ingest --source claude_code
life-long-memory ingest --source codex
```

### 2. Generate summaries (L3 -> L2)

```bash
# Summarize all unsummarized sessions
life-long-memory summarize

# Limit to N sessions
life-long-memory summarize --limit 20

# Use a specific model
life-long-memory summarize --model sonnet
```

### 3. Promote knowledge (L2 -> L1)

```bash
# Promote all projects
life-long-memory promote

# Only for a specific project
life-long-memory promote --project /path/to/project
```

### 4. Search & explore

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

### 5. Connect to Claude Code via MCP

Add to `~/.claude/.mcp.json`:

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

Restart Claude Code. Four MCP tools become available:

| Tool | Description |
|------|-------------|
| `memory_search` | Hybrid search (keyword + recency + importance) across all sessions |
| `memory_timeline` | Chronological view of sessions, filterable by project and date |
| `memory_project_context` | L1 knowledge + recent summaries for a project |
| `memory_recall_session` | Full details of a specific session |

## LLM Backend

Life-Long Memory uses the locally installed **Claude Code CLI** (`claude --print`) for all LLM calls (summarize, promote). No API keys are needed - it piggybacks on Claude Code's built-in OAuth authentication.

The `CLAUDECODE` environment variable is automatically cleared for subprocess invocations to allow nested usage.

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

Adding a new source requires implementing the `BaseParser` interface (`parsers/base.py`).

## Project Structure

```
src/life_long_memory/
  __init__.py
  cli.py            # CLI entry point (ingest, search, summarize, promote, ...)
  config.py         # Configuration & defaults
  db.py             # SQLite schema, queries, FTS5
  search.py         # Hybrid search (BM25 + recency + importance)
  summarize.py      # L3->L2 session summarization via LLM
  promote.py        # L2->L1 cross-session knowledge consolidation
  llm.py            # LLM invocation via claude CLI subprocess
  entities.py       # Regex-based entity extraction
  background.py     # Job queue for async processing
  mcp_server.py     # MCP server exposing memory tools
  parsers/
    base.py          # Abstract parser interface
    claude_code.py   # Claude Code session parser
    codex.py         # Codex CLI session parser
tests/
  test_core.py       # Unit tests (DB, entities, parsers, search)
```

## License

MIT
