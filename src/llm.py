"""LLM invocation via locally installed CLI tools (no API keys needed).

Supports multiple backends: Claude Code CLI, Codex CLI, and Gemini CLI.
Routes LLM calls to the appropriate backend based on session source.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time as _time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Default models per backend (fast & cheap)
DEFAULT_MODELS = {
    "claude": "haiku",
    "codex": "o3",
    "gemini": "gemini-2.5-flash",
}

# Map source names to backend names
SOURCE_TO_BACKEND = {
    "claude_code": "claude",
    "codex": "codex",
    "gemini": "gemini",
}


@dataclass
class LLMResponse:
    """Structured response from any LLM CLI with full content blocks."""

    text: str = ""                                     # final text output
    thinking: list[str] = field(default_factory=list)  # thinking / reasoning blocks
    tool_calls: list[dict] = field(default_factory=list)  # tool_use / function_call blocks
    tool_results: list[dict] = field(default_factory=list)  # tool_result / function_call_output
    usage: dict = field(default_factory=dict)           # token usage
    session_id: str = ""                                # traceable session ID
    backend: str = ""                                   # "claude" | "codex" | "gemini"
    raw_messages: list[dict] = field(default_factory=list)  # full raw messages
    jsonl_path: str | None = None                       # path to clean trace file


# Backward compat alias
ClaudeResponse = LLMResponse


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_traces_dir() -> Path:
    """Return the default traces directory, creating it if needed.

    Uses {cwd}/tests/traces/ so traces live alongside the project code.
    """
    traces_dir = Path.cwd() / "tests" / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    return traces_dir


def _save_trace(trace: dict, session_id: str, traces_dir: Path | None = None) -> str | None:
    """Write a clean trace dict to JSON and return the path."""
    try:
        dest_dir = traces_dir or _default_traces_dir()
        dest = dest_dir / f"{session_id}.json"
        with open(dest, "w") as f:
            json.dump(trace, f, indent=2, ensure_ascii=False)
        return str(dest)
    except Exception as e:
        logger.warning(f"Failed to save trace: {e}")
        return None


# ---------------------------------------------------------------------------
# Backend detection & dispatch
# ---------------------------------------------------------------------------

def _detect_available_backend() -> str | None:
    """Detect which CLI backends are available on PATH."""
    for backend, cmd in [("claude", "claude"), ("codex", "codex"), ("gemini", "gemini")]:
        if shutil.which(cmd):
            return backend
    return None


def _resolve_backend(source: str | None) -> str:
    """Map a session source to a backend, falling back to any available CLI."""
    if source:
        backend = SOURCE_TO_BACKEND.get(source)
        if backend and shutil.which(backend if backend != "claude" else "claude"):
            return backend
    fallback = _detect_available_backend()
    if fallback:
        return fallback
    raise RuntimeError(
        "No LLM CLI backend found. Install one of: claude (Claude Code), codex (Codex CLI), gemini (Gemini CLI)"
    )


def call_llm(
    prompt: str,
    *,
    source: str | None = None,
    model: str | None = None,
    backend: str | None = None,
) -> str:
    """Dispatch an LLM call to the appropriate backend. Returns text."""
    if backend:
        resolved = backend
    else:
        resolved = _resolve_backend(source)
    effective_model = model or DEFAULT_MODELS[resolved]

    dispatch = {"claude": call_claude, "codex": call_codex, "gemini": call_gemini}
    try:
        return dispatch[resolved](prompt, model=effective_model)
    except Exception:
        if backend:
            raise
        for fb_name in ["claude", "codex", "gemini"]:
            if fb_name == resolved:
                continue
            if not shutil.which(fb_name):
                continue
            try:
                return dispatch[fb_name](prompt, model=model or DEFAULT_MODELS[fb_name])
            except Exception:
                continue
        raise


def call_llm_full(
    prompt: str,
    *,
    source: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    traces_dir: Path | None = None,
) -> LLMResponse:
    """Dispatch a full LLM call with structured response + trace.

    Like call_llm() but returns an LLMResponse with thinking, tool calls,
    usage, and saves a clean trace file.
    """
    if backend:
        resolved = backend
    else:
        resolved = _resolve_backend(source)
    effective_model = model or DEFAULT_MODELS[resolved]

    dispatch = {
        "claude": call_claude_full,
        "codex": call_codex_full,
        "gemini": call_gemini_full,
    }
    return dispatch[resolved](prompt, model=effective_model, traces_dir=traces_dir)


# ===========================================================================
# Claude backend
# ===========================================================================

def _cwd_to_slug(cwd: str) -> str:
    """Convert a CWD path to Claude Code's project slug."""
    return cwd.replace("/", "-").replace("\\", "-")


def _find_session_jsonl(session_id: str, projects_base: Path | None = None) -> Path | None:
    """Locate the JSONL session file for a given Claude session ID."""
    if projects_base is None:
        projects_base = Path.home() / ".claude" / "projects"
    projects_base = projects_base.expanduser()
    if not projects_base.exists():
        return None
    target = f"{session_id}.jsonl"
    for project_dir in projects_base.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / target
        if candidate.exists():
            return candidate
    return None


def _parse_session_jsonl(jsonl_path: Path, session_id: str) -> LLMResponse:
    """Parse a Claude Code JSONL session file into an LLMResponse."""
    response = LLMResponse(session_id=session_id, backend="claude")
    text_parts: list[str] = []

    with open(jsonl_path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            message = rec.get("message", {})
            if not message:
                continue
            response.raw_messages.append(rec)

            usage = message.get("usage", {})
            if usage:
                response.usage = usage

            role = message.get("role", "")
            content = message.get("content", "")

            if role == "assistant" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "thinking":
                        t = block.get("thinking", "")
                        if t.strip():
                            response.thinking.append(t)
                    elif btype == "text":
                        t = block.get("text", "")
                        if t.strip():
                            text_parts.append(t)
                    elif btype == "tool_use":
                        response.tool_calls.append({
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        })

            elif role == "user" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rc = "\n".join(
                                rb.get("text", "") for rb in rc
                                if isinstance(rb, dict) and rb.get("type") == "text"
                            )
                        response.tool_results.append({
                            "tool_use_id": block.get("tool_use_id", ""),
                            "content": rc,
                            "is_error": block.get("is_error", False),
                        })

    response.text = "\n".join(text_parts)
    return response


def _build_claude_trace(jsonl_path: Path, session_id: str) -> dict:
    """Build a clean trace from a raw Claude JSONL session file."""
    meta: dict = {}
    turns: list[dict] = []
    final_usage: dict = {}

    with open(jsonl_path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("type") in ("queue-operation", "progress", "file-history-snapshot"):
                continue

            if not meta:
                meta = {k: rec[k] for k in ("sessionId", "cwd", "gitBranch", "version") if k in rec}
                if "model" in rec.get("message", {}):
                    meta["model"] = rec["message"]["model"]

            message = rec.get("message", {})
            if not message:
                continue

            role = message.get("role", "")
            content = message.get("content", "")
            ts = rec.get("timestamp", "")

            if message.get("usage"):
                final_usage = message["usage"]

            if role == "assistant" and isinstance(content, list):
                if "model" in message and message["model"]:
                    meta["model"] = message["model"]
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type", "")
                    if bt == "thinking" and block.get("thinking", "").strip():
                        turns.append({"role": "assistant", "type": "thinking", "text": block["thinking"], "ts": ts})
                    elif bt == "text" and block.get("text", "").strip():
                        turns.append({"role": "assistant", "type": "text", "text": block["text"], "ts": ts})
                    elif bt == "tool_use":
                        turns.append({"role": "assistant", "type": "tool_use", "tool": block.get("name", ""),
                                      "input": block.get("input", {}), "id": block.get("id", ""), "ts": ts})

            elif role == "user":
                if isinstance(content, str) and content.strip():
                    turns.append({"role": "user", "type": "text", "text": content, "ts": ts})
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type", "")
                        if bt == "text" and block.get("text", "").strip():
                            turns.append({"role": "user", "type": "text", "text": block["text"], "ts": ts})
                        elif bt == "tool_result":
                            rc = block.get("content", "")
                            if isinstance(rc, list):
                                rc = "\n".join(rb.get("text", "") for rb in rc if isinstance(rb, dict) and rb.get("type") == "text")
                            turns.append({"role": "tool", "type": "tool_result", "tool_use_id": block.get("tool_use_id", ""),
                                          "content": str(rc), "is_error": block.get("is_error", False), "ts": ts})

    return {"session_id": session_id, "backend": "claude", **meta, "usage": final_usage, "turns": turns}


def _run_claude_cli(prompt: str, *, model: str = "haiku", session_id: str | None = None) -> tuple[str, str]:
    """Run the claude CLI and return (text_result, session_id)."""
    if session_id is None:
        session_id = str(uuid.uuid4())

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(prompt)
        prompt_file = f.name
    try:
        cmd = [
            "claude", "--print", "--model", model, "--session-id", session_id,
            "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
            f"Read the file {prompt_file} and follow the instructions in it exactly. Return ONLY the requested output format, nothing else.",
        ]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

        result_text = None
        assistant_texts = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "result":
                result_text = event.get("result", "")
            elif etype == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        assistant_texts.append(block["text"])
        proc.wait()

        if result_text:
            return result_text, session_id
        if assistant_texts:
            return "\n".join(assistant_texts), session_id
        stderr = proc.stderr.read()
        raise RuntimeError(f"claude CLI returned no output (exit={proc.returncode}): {stderr[:500]}")
    finally:
        Path(prompt_file).unlink(missing_ok=True)


def call_claude(prompt: str, *, model: str = "haiku", max_tokens: int = 4000) -> str:
    """Call Claude via CLI. Returns text."""
    text, _ = _run_claude_cli(prompt, model=model)
    return text


def call_claude_full(
    prompt: str, *, model: str = "haiku", session_id: str | None = None,
    projects_base: Path | None = None, traces_dir: Path | None = None,
) -> LLMResponse:
    """Call Claude via CLI and return structured response with thinking/tool_use + trace."""
    sid = session_id or str(uuid.uuid4())
    text, sid = _run_claude_cli(prompt, model=model, session_id=sid)

    jsonl_path = _find_session_jsonl(sid, projects_base)
    if jsonl_path:
        response = _parse_session_jsonl(jsonl_path, sid)
        if not response.text:
            response.text = text
        trace = _build_claude_trace(jsonl_path, sid)
        response.jsonl_path = _save_trace(trace, sid, traces_dir)
        return response

    return LLMResponse(text=text, session_id=sid, backend="claude")


# ===========================================================================
# Codex backend
# ===========================================================================

def _find_latest_codex_session(after_ts: float) -> Path | None:
    """Find the most recently created Codex session JSONL created after after_ts."""
    base = Path.home() / ".codex" / "sessions"
    if not base.exists():
        return None
    candidates = sorted(base.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for c in candidates:
        if c.stat().st_mtime >= after_ts:
            return c
    return None


def _parse_codex_session(jsonl_path: Path) -> LLMResponse:
    """Parse a Codex session JSONL into an LLMResponse."""
    response = LLMResponse(backend="codex")
    text_parts: list[str] = []

    with open(jsonl_path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            response.raw_messages.append(rec)
            rec_type = rec.get("type", "")
            payload = rec.get("payload", {})

            if rec_type == "session_meta":
                response.session_id = payload.get("id", "")

            elif rec_type == "event_msg":
                pt = payload.get("type", "")
                if pt == "token_count":
                    info = payload.get("info")
                    if info and isinstance(info, dict):
                        response.usage = info.get("total_token_usage", {})

            elif rec_type == "response_item":
                pt = payload.get("type", "")

                if pt == "reasoning":
                    parts = []
                    for s in payload.get("summary", []):
                        if isinstance(s, dict):
                            parts.append(s.get("text", ""))
                    text = "\n".join(parts)
                    if text.strip():
                        response.thinking.append(text)

                elif pt in ("function_call", "custom_tool_call"):
                    name = payload.get("name", "")
                    args = payload.get("arguments", payload.get("input", ""))
                    response.tool_calls.append({
                        "name": name, "arguments": args,
                        "call_id": payload.get("call_id", ""),
                    })

                elif pt in ("function_call_output", "custom_tool_call_output"):
                    response.tool_results.append({
                        "call_id": payload.get("call_id", ""),
                        "content": payload.get("output", ""),
                    })

                elif pt == "message":
                    role = payload.get("role", "")
                    if role == "assistant":
                        for part in payload.get("content", []):
                            if isinstance(part, dict):
                                t = part.get("text", "")
                                if t:
                                    text_parts.append(t)
                            elif isinstance(part, str):
                                text_parts.append(part)

    response.text = "\n".join(text_parts)
    return response


def _build_codex_trace(jsonl_path: Path) -> dict:
    """Build a clean trace from a Codex session JSONL."""
    meta: dict = {}
    turns: list[dict] = []
    usage: dict = {}

    with open(jsonl_path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            rec_type = rec.get("type", "")
            payload = rec.get("payload", {})
            ts = rec.get("timestamp", "")

            if rec_type == "session_meta":
                meta = {
                    "session_id": payload.get("id", ""),
                    "cwd": payload.get("cwd", ""),
                    "version": payload.get("cli_version", ""),
                }

            elif rec_type == "turn_context":
                if not meta.get("model"):
                    meta["model"] = payload.get("model", "")

            elif rec_type == "event_msg":
                pt = payload.get("type", "")
                if pt == "token_count":
                    info = payload.get("info")
                    if info and isinstance(info, dict):
                        usage = info.get("total_token_usage", {})
                elif pt == "user_message":
                    text = payload.get("message", "")
                    if text.strip():
                        turns.append({"role": "user", "type": "text", "text": text, "ts": ts})

            elif rec_type == "response_item":
                pt = payload.get("type", "")

                if pt == "reasoning":
                    parts = []
                    for s in payload.get("summary", []):
                        if isinstance(s, dict):
                            parts.append(s.get("text", ""))
                    text = "\n".join(parts)
                    if text.strip():
                        turns.append({"role": "assistant", "type": "thinking", "text": text, "ts": ts})

                elif pt in ("function_call", "custom_tool_call"):
                    name = payload.get("name", "")
                    args = payload.get("arguments", payload.get("input", ""))
                    turns.append({
                        "role": "assistant", "type": "tool_use", "tool": name,
                        "input": args, "id": payload.get("call_id", ""), "ts": ts,
                    })

                elif pt in ("function_call_output", "custom_tool_call_output"):
                    turns.append({
                        "role": "tool", "type": "tool_result",
                        "call_id": payload.get("call_id", ""),
                        "content": payload.get("output", ""), "ts": ts,
                    })

                elif pt == "message":
                    role = payload.get("role", "")
                    # Skip system/developer/context messages
                    if role in ("developer", "system"):
                        continue
                    content_parts = payload.get("content", [])
                    for part in content_parts:
                        text = ""
                        if isinstance(part, dict):
                            text = part.get("text", "")
                        elif isinstance(part, str):
                            text = part
                        if text.strip():
                            # Skip large instruction/context blocks
                            if text.startswith("<environment_context>") or text.startswith("<permissions"):
                                continue
                            if text.startswith("# AGENTS.md") or text.startswith("<INSTRUCTIONS>"):
                                continue
                            turns.append({"role": role, "type": "text", "text": text, "ts": ts})

    return {"backend": "codex", **meta, "usage": usage, "turns": turns}


def call_codex(prompt: str, *, model: str = "o3") -> str:
    """Call Codex CLI. Returns text."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(prompt)
        prompt_file = f.name
    try:
        cmd = [
            "codex", "exec", "--skip-git-repo-check", "--json", "--full-auto", "-m", model,
            f"Read the file {prompt_file} and follow the instructions in it exactly. Return ONLY the requested output format, nothing else.",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"codex CLI failed (exit={proc.returncode}): {proc.stderr[:500]}")
        output = proc.stdout.strip()
        if not output:
            raise RuntimeError(f"codex CLI returned no output (exit={proc.returncode}): {proc.stderr[:500]}")
        return _parse_codex_json(output)
    finally:
        Path(prompt_file).unlink(missing_ok=True)


def _parse_codex_json(output: str) -> str:
    """Extract assistant text from codex exec --json stdout."""
    assistant_texts = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type", "")
        if etype == "message" and event.get("role") == "assistant":
            content = event.get("content", "")
            if isinstance(content, str) and content:
                assistant_texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        assistant_texts.append(block["text"])
                    elif isinstance(block, str):
                        assistant_texts.append(block)
        elif etype in ("output", "result"):
            text = event.get("text") or event.get("result") or event.get("content", "")
            if text:
                assistant_texts.append(text)
    if assistant_texts:
        return "\n".join(assistant_texts)
    return output


def call_codex_full(
    prompt: str, *, model: str = "o3", traces_dir: Path | None = None,
) -> LLMResponse:
    """Call Codex CLI and return structured response with reasoning/tool calls + trace."""
    before = _time.time()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(prompt)
        prompt_file = f.name
    try:
        cmd = [
            "codex", "exec", "--skip-git-repo-check", "--json", "--full-auto", "-m", model,
            f"Read the file {prompt_file} and follow the instructions in it exactly. Return ONLY the requested output format, nothing else.",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"codex CLI failed (exit={proc.returncode}): {proc.stderr[:500]}")
        output = proc.stdout.strip()
        if not output:
            raise RuntimeError(f"codex CLI returned no output (exit={proc.returncode}): {proc.stderr[:500]}")
    finally:
        Path(prompt_file).unlink(missing_ok=True)

    # Parse text from stdout
    text = _parse_codex_json(output)

    # Find the session JSONL for full structured content
    session_path = _find_latest_codex_session(before)
    if session_path:
        response = _parse_codex_session(session_path)
        if not response.text:
            response.text = text
        trace = _build_codex_trace(session_path)
        response.jsonl_path = _save_trace(trace, response.session_id or str(uuid.uuid4()), traces_dir)
        return response

    return LLMResponse(text=text, backend="codex")


# ===========================================================================
# Gemini backend
# ===========================================================================

def _find_latest_gemini_session(after_ts: float) -> Path | None:
    """Find the most recently created Gemini session JSON created after after_ts."""
    base = Path.home() / ".gemini" / "tmp"
    if not base.exists():
        return None
    candidates = sorted(base.rglob("session-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for c in candidates:
        if c.stat().st_mtime >= after_ts:
            return c
    return None


def _parse_gemini_session(json_path: Path) -> LLMResponse:
    """Parse a Gemini session JSON into an LLMResponse."""
    try:
        data = json.loads(json_path.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return LLMResponse(backend="gemini")

    response = LLMResponse(
        session_id=data.get("sessionId", json_path.stem),
        backend="gemini",
    )
    text_parts: list[str] = []
    total_usage: dict = {}

    for msg in data.get("messages", []):
        if not isinstance(msg, dict):
            continue
        response.raw_messages.append(msg)
        msg_type = msg.get("type", "")

        if msg_type == "gemini":
            # Thinking / thoughts
            for thought in msg.get("thoughts", []):
                desc = thought.get("description", "")
                subject = thought.get("subject", "")
                t = f"{subject}: {desc}" if subject else desc
                if t.strip():
                    response.thinking.append(t)

            # Tool calls + results
            for tc in msg.get("toolCalls", []):
                name = tc.get("name", "")
                if name:
                    response.tool_calls.append({
                        "name": name, "args": tc.get("args", {}),
                        "status": tc.get("status", ""),
                    })
                    result = tc.get("result", "")
                    result_text = json.dumps(result) if not isinstance(result, str) else result
                    response.tool_results.append({"content": result_text})

            # Token usage (accumulate)
            tokens = msg.get("tokens", {})
            if tokens:
                for k, v in tokens.items():
                    total_usage[k] = total_usage.get(k, 0) + (v if isinstance(v, int) else 0)

            # Text content
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                text_parts.append(content)

    response.usage = total_usage
    response.text = "\n".join(text_parts)
    return response


def _build_gemini_trace(json_path: Path) -> dict:
    """Build a clean trace from a Gemini session JSON."""
    try:
        data = json.loads(json_path.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}

    meta = {
        "session_id": data.get("sessionId", ""),
        "backend": "gemini",
        "startTime": data.get("startTime", ""),
        "lastUpdated": data.get("lastUpdated", ""),
    }
    turns: list[dict] = []
    total_usage: dict = {}
    model = None

    for msg in data.get("messages", []):
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type", "")
        ts = msg.get("timestamp", "")

        if msg_type == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                turns.append({"role": "user", "type": "text", "text": content, "ts": ts})
            elif isinstance(content, list):
                for item in content:
                    t = item.get("text", "") if isinstance(item, dict) else str(item)
                    if t.strip():
                        turns.append({"role": "user", "type": "text", "text": t, "ts": ts})

        elif msg_type == "gemini":
            if not model:
                model = msg.get("model")

            for thought in msg.get("thoughts", []):
                desc = thought.get("description", "")
                subject = thought.get("subject", "")
                t = f"{subject}: {desc}" if subject else desc
                if t.strip():
                    turns.append({"role": "assistant", "type": "thinking", "text": t, "ts": ts})

            for tc in msg.get("toolCalls", []):
                name = tc.get("name", "")
                if name:
                    turns.append({
                        "role": "assistant", "type": "tool_use", "tool": name,
                        "input": tc.get("args", {}), "ts": ts,
                    })
                    result = tc.get("result", "")
                    result_text = json.dumps(result) if not isinstance(result, str) else result
                    turns.append({
                        "role": "tool", "type": "tool_result",
                        "content": result_text, "ts": ts,
                    })

            tokens = msg.get("tokens", {})
            if tokens:
                for k, v in tokens.items():
                    total_usage[k] = total_usage.get(k, 0) + (v if isinstance(v, int) else 0)

            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                turns.append({"role": "assistant", "type": "text", "text": content, "ts": ts})

    if model:
        meta["model"] = model
    return {**meta, "usage": total_usage, "turns": turns}


def call_gemini(prompt: str, *, model: str = "gemini-2.5-flash") -> str:
    """Call Gemini CLI. Returns text."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(prompt)
        prompt_file = f.name
    try:
        cmd = [
            "gemini",
            "--prompt", f"Read the file {prompt_file} and follow the instructions in it exactly. Return ONLY the requested output format, nothing else.",
            "--model", model, "--output-format", "text",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"gemini CLI failed (exit={proc.returncode}): {proc.stderr[:500]}")
        output = proc.stdout.strip()
        if not output:
            raise RuntimeError(f"gemini CLI returned no output (exit={proc.returncode}): {proc.stderr[:500]}")
        return output
    finally:
        Path(prompt_file).unlink(missing_ok=True)


def call_gemini_full(
    prompt: str, *, model: str = "gemini-2.5-flash", traces_dir: Path | None = None,
) -> LLMResponse:
    """Call Gemini CLI and return structured response with thoughts/tool calls + trace."""
    before = _time.time()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(prompt)
        prompt_file = f.name
    try:
        cmd = [
            "gemini",
            "--prompt", f"Read the file {prompt_file} and follow the instructions in it exactly. Return ONLY the requested output format, nothing else.",
            "--model", model, "--output-format", "text",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"gemini CLI failed (exit={proc.returncode}): {proc.stderr[:500]}")
        output = proc.stdout.strip()
        if not output:
            raise RuntimeError(f"gemini CLI returned no output (exit={proc.returncode}): {proc.stderr[:500]}")
    finally:
        Path(prompt_file).unlink(missing_ok=True)

    text = output

    # Find the session JSON for full structured content
    session_path = _find_latest_gemini_session(before)
    if session_path:
        response = _parse_gemini_session(session_path)
        if not response.text:
            response.text = text
        trace = _build_gemini_trace(session_path)
        response.jsonl_path = _save_trace(trace, response.session_id or str(uuid.uuid4()), traces_dir)
        return response

    return LLMResponse(text=text, backend="gemini")
