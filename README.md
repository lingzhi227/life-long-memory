# Life-Long Memory

<p align="center">
  <img src="assets/architecture.svg" alt="Life-Long Memory Architecture" width="100%">
</p>

<p align="center">
  <strong>Give your AI coding agent a persistent memory across sessions.</strong>
</p>

<p align="center">
  <a href="https://github.com/lingzhi227/life-long-memory/releases"><img src="https://img.shields.io/github/v/release/lingzhi227/life-long-memory?include_prereleases&style=for-the-badge" alt="GitHub release"></a>
  <a href="https://github.com/lingzhi227/life-long-memory/releases"><img src="https://img.shields.io/github/release-date/lingzhi227/life-long-memory?style=for-the-badge" alt="Release date"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.11+-3776AB.svg?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"></a>
</p>

**Life-Long Memory** is a memory layer for CLI coding agents. It automatically ingests, summarizes, and consolidates knowledge from your [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), and [Gemini CLI](https://github.com/google-gemini/gemini-cli) sessions into a local SQLite database, then exposes it via [MCP](https://modelcontextprotocol.io/) tools so your AI agent remembers what you've worked on — across sessions, across projects, across tools.

No API keys needed. No cloud services. Everything runs locally.

[Quick Start](#quick-start) · [How It Works](#how-it-works) · [MCP Tools](#mcp-tools) · [CLI Commands](#cli-commands) · [LLM Backend](#llm-backend) · [Changelog](CHANGELOG.md) · [Contributing](#contributors)

---

## Why

CLI agents like Claude Code, Codex, and Gemini are powerful — but every session starts from scratch. Life-Long Memory fixes this:

- **Ingests** session transcripts from all three major CLI tools
- **Summarizes** each session into structured knowledge (decisions, files, commands, outcome)
- **Consolidates** cross-session patterns into stable per-project knowledge
- **Exposes** everything via MCP tools your agent can query in real-time

The result: your agent knows what you built last week, what architectural decisions were made, and what commands worked — without you re-explaining anything.

## How It Works

### Three-Tier Memory Model

| Tier | Name | What it stores | How it's built |
|------|------|----------------|----------------|
| **L3** | Raw | Full conversation transcripts, FTS5-indexed | `ingest` — parses session files |
| **L2** | Summaries | Key decisions, files touched, commands run, outcome | `summarize` — LLM-generated per session |
| **L1** | Knowledge | Stable patterns & architecture decisions with confidence scores | `promote` — cross-session consolidation |

```
L3 Raw Sessions ──(summarize)──> L2 Session Summaries ──(promote)──> L1 Project Knowledge
```

### Supported Sources

| Source | Session Location | Format |
|--------|-----------------|--------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `~/.claude/projects/{slug}/{uuid}.jsonl` | JSONL |
| [Codex CLI](https://github.com/openai/codex) | `~/.codex/sessions/{year}/{month}/{date}/rollout-*.jsonl` | JSONL |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `~/.gemini/tmp/{projectHash}/chats/session-*.json` | JSON |

Adding a new source requires implementing the `SessionParser` interface ([`parsers/base.py`](src/parsers/base.py)).

### Hybrid Search

Search combines three signals for relevance:

```
score = fts_bm25 * 0.5 + recency * 0.25 + importance * 0.25
```

- **FTS BM25** — full-text keyword relevance via SQLite FTS5
- **Recency** — exponential decay with 30-day half-life
- **Importance** — weighted by message count, user messages, tokens, compactions

### Database

All data lives in a single SQLite file at `~/.tactical/memory.sqlite`. No external databases or services.

**Tables:** `sessions`, `messages` (FTS5-indexed), `session_summaries`, `entities`, `project_knowledge`

---

## Quick Start

### Install

```bash
pip install --user "life-long-memory[mcp] @ git+https://github.com/lingzhi227/life-long-memory.git"
export PATH="$HOME/.local/bin:$PATH"  # if not found after install
```

### Setup

```bash
life-long-memory setup     # detect CLIs, configure MCP, ingest all sessions
life-long-memory doctor    # verify everything works
```

Setup detects your CLI tools (Claude Code, Codex, Gemini), writes MCP configs with **absolute binary paths** (avoids PATH issues), and ingests all sessions. Restart your CLI tool to activate MCP memory tools.

### Generate Knowledge

```bash
life-long-memory auto      # ingest + summarize + promote in one step
```

### Install from Source

```bash
git clone https://github.com/lingzhi227/life-long-memory.git
cd life-long-memory
pip install -e ".[mcp]"
life-long-memory setup
```

### Troubleshooting

If `life-long-memory` is not found after install:

```bash
python -m site --user-base                                    # usually ~/.local
ls "$(python -m site --user-base)/bin/life-long-memory"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc      # add to PATH permanently
```

Run `life-long-memory doctor` to diagnose MCP and binary path issues.

---

## MCP Tools

Once setup is complete and your CLI tool is restarted, four MCP tools become available:

| Tool | Description |
|------|-------------|
| `memory_search` | Hybrid search (keyword + recency + importance) across all sessions |
| `memory_timeline` | Chronological view of sessions, filterable by project and date |
| `memory_project_context` | L1 knowledge + recent summaries for a project |
| `memory_recall_session` | Full details of a specific session |

The MCP server auto-ingests new sessions on startup, so memory is always current.

---

## CLI Commands

### Core Pipeline

```bash
life-long-memory ingest                          # ingest sessions from all sources
life-long-memory ingest --source codex           # ingest from one source only
life-long-memory summarize                       # generate L2 summaries (LLM)
life-long-memory summarize --limit 20            # cap to 20 sessions
life-long-memory promote                         # consolidate L1 knowledge (LLM)
life-long-memory promote --project /path/to/proj # promote one project only
life-long-memory auto                            # run all three steps
life-long-memory auto --limit 10 --backend claude  # cap + force backend
```

### Search & Explore

```bash
life-long-memory search "bioinformatics deployment"
life-long-memory search "docker" --project /path/to/project
life-long-memory timeline --after 2025-11-01 --before 2025-12-01
life-long-memory recall <session-uuid> --messages
life-long-memory stats
```

### Maintenance

```bash
life-long-memory setup                           # one-command install
life-long-memory doctor                          # verify installation health
life-long-memory prune --project /old/path       # delete stale project data
life-long-memory prune --project /old/path --knowledge-only  # keep sessions
```

### Backend Override

The `--backend` flag on `summarize`, `promote`, and `auto` overrides source-aware routing:

```bash
life-long-memory summarize --backend claude      # summarize all sessions via Claude
life-long-memory auto --backend claude --limit 5 # incremental processing via Claude
```

---

## LLM Backend

Life-Long Memory uses locally installed CLI tools for all LLM calls. **No API keys needed** — each CLI handles its own authentication.

**Source-aware routing**: when summarizing a session, the system uses the same CLI that produced it. If that CLI isn't installed, it falls back to the next available one.

| Backend | CLI Command | Default Model |
|---------|-------------|---------------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `claude --print --model {m}` | `haiku` |
| [Codex CLI](https://github.com/openai/codex) | `codex exec -m {m}` | `o3` |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `gemini --model {m}` | `gemini-2.5-flash` |

The `--model` flag overrides the backend default. The `CLAUDECODE` environment variable is automatically cleared for nested Claude invocations.

---

## Manual MCP Configuration

If you prefer manual setup over `life-long-memory setup`, use the **absolute path** to the binary (`which life-long-memory`):

<details>
<summary><strong>Claude Code</strong> — <code>~/.claude/.mcp.json</code></summary>

```json
{
  "mcpServers": {
    "life-long-memory": {
      "command": "/absolute/path/to/life-long-memory",
      "args": ["serve"]
    }
  }
}
```
</details>

<details>
<summary><strong>Codex CLI</strong> — <code>~/.codex/config.toml</code></summary>

```toml
[mcp_servers.life-long-memory]
command = "/absolute/path/to/life-long-memory"
args = ["serve"]
```
</details>

<details>
<summary><strong>Gemini CLI</strong> — <code>~/.gemini/settings.json</code></summary>

```json
{
  "mcpServers": {
    "life-long-memory": {
      "command": "/absolute/path/to/life-long-memory",
      "args": ["serve"],
      "trust": true
    }
  }
}
```
</details>

---

## Project Structure

```
life-long-memory/
  src/                        # Python package
    cli.py                    # CLI entry point (all user-facing commands)
    db.py                     # SQLite schema, CRUD, FTS5 search
    config.py                 # Configuration & defaults
    search.py                 # Hybrid search (BM25 + recency + importance)
    summarize.py              # L3->L2 session summarization via LLM
    promote.py                # L2->L1 cross-session knowledge consolidation
    llm.py                    # LLM invocation via CLI subprocesses
    entities.py               # Regex-based entity extraction
    auto.py                   # Auto-processing pipeline with cooldown
    mcp_server.py             # MCP server exposing memory tools
    parsers/
      base.py                 # Abstract parser interface
      claude_code.py          # Claude Code session parser
      codex.py                # Codex CLI session parser
      gemini.py               # Gemini CLI session parser
  tests/
    test_core.py              # Unit tests (35 tests, all in-memory SQLite)
  pyproject.toml
  CHANGELOG.md
  AGENTS.md
  LICENSE
  README.md
```

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=lingzhi227/life-long-memory&type=Date)](https://star-history.com/#lingzhi227/life-long-memory&Date)

## Contributors

<a href="https://github.com/lingzhi227/life-long-memory/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=lingzhi227/life-long-memory" />
</a>

---

## License

[MIT](LICENSE) &copy; 2026 [lingzhi227](https://github.com/lingzhi227)
