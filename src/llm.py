"""LLM invocation via locally installed CLI tools (no API keys needed).

Supports multiple backends: Claude Code CLI, Codex CLI, and Gemini CLI.
Routes LLM calls to the appropriate backend based on session source.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

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


def _detect_available_backend() -> str | None:
    """Detect which CLI backends are available on PATH.

    Returns the first available backend name, or None.
    """
    checks = [
        ("claude", "claude"),
        ("codex", "codex"),
        ("gemini", "gemini"),
    ]
    for backend, cmd in checks:
        if shutil.which(cmd):
            return backend
    return None


def _resolve_backend(source: str | None) -> str:
    """Map a session source to a backend, falling back to any available CLI.

    Args:
        source: Session source name (e.g. "claude_code", "codex", "gemini").

    Returns:
        Backend name ("claude", "codex", or "gemini").

    Raises:
        RuntimeError: If no CLI backend is available.
    """
    # Try the source's native backend first
    if source:
        backend = SOURCE_TO_BACKEND.get(source)
        if backend and shutil.which(backend if backend != "claude" else "claude"):
            return backend

    # Fall back to any available backend
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
    """Dispatch an LLM call to the appropriate backend based on source.

    Args:
        prompt: The prompt text to send.
        source: Session source (e.g. "claude_code", "codex", "gemini").
            Used to pick the backend. Falls back to any available CLI.
        model: Model override. If None, uses the backend's default.
        backend: Force a specific backend ("claude", "codex", "gemini"),
            bypassing source-based routing.

    Returns:
        The text response from the LLM.

    If the primary backend fails and no explicit backend was forced,
    automatically falls back to any other available backend.
    """
    if backend:
        resolved = backend
    else:
        resolved = _resolve_backend(source)
    effective_model = model or DEFAULT_MODELS[resolved]

    dispatch = {
        "claude": call_claude,
        "codex": call_codex,
        "gemini": call_gemini,
    }

    try:
        return dispatch[resolved](prompt, model=effective_model)
    except Exception:
        if backend:
            raise  # explicit backend requested — don't fallback
        # Auto-fallback to any other available backend
        for fb_name in ["claude", "codex", "gemini"]:
            if fb_name == resolved:
                continue
            fb_cmd = fb_name if fb_name != "claude" else "claude"
            if not shutil.which(fb_cmd):
                continue
            try:
                fb_model = model or DEFAULT_MODELS[fb_name]
                return dispatch[fb_name](prompt, model=fb_model)
            except Exception:
                continue
        raise  # all backends failed — re-raise original error


def call_claude(
    prompt: str,
    *,
    model: str = "haiku",
    max_tokens: int = 4000,
) -> str:
    """Call Claude via the locally installed claude CLI.

    Returns the text response.
    """
    # Write long prompts to a temp file to avoid arg length limits
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    ) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        cmd = [
            "claude",
            "--print",
            "--model", model,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            f"Read the file {prompt_file} and follow the instructions in it exactly. Return ONLY the requested output format, nothing else.",
        ]

        # Clear CLAUDECODE env var to allow nested invocation
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

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
            return result_text
        if assistant_texts:
            return "\n".join(assistant_texts)

        stderr = proc.stderr.read()
        raise RuntimeError(
            f"claude CLI returned no output (exit={proc.returncode}): {stderr[:500]}"
        )
    finally:
        Path(prompt_file).unlink(missing_ok=True)


def call_codex(
    prompt: str,
    *,
    model: str = "o3",
) -> str:
    """Call an LLM via the locally installed Codex CLI.

    Uses `codex exec --json` and parses the JSON output for assistant text.
    Returns the text response.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    ) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        cmd = [
            "codex", "exec",
            "--skip-git-repo-check",
            "--json",
            "--full-auto",
            "-m", model,
            f"Read the file {prompt_file} and follow the instructions in it exactly. Return ONLY the requested output format, nothing else.",
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                f"codex CLI failed (exit={proc.returncode}): {proc.stderr[:500]}"
            )

        output = proc.stdout.strip()
        if not output:
            raise RuntimeError(
                f"codex CLI returned no output (exit={proc.returncode}): {proc.stderr[:500]}"
            )

        # Parse JSON output — extract assistant message text
        return _parse_codex_json(output)
    finally:
        Path(prompt_file).unlink(missing_ok=True)


def _parse_codex_json(output: str) -> str:
    """Extract assistant text from codex exec --json output.

    The output is newline-delimited JSON events. We look for message events
    with role=assistant and extract the text content.
    """
    assistant_texts = []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Handle various codex JSON output formats
        etype = event.get("type", "")

        # Direct message format
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

        # Output/result event
        elif etype in ("output", "result"):
            text = event.get("text") or event.get("result") or event.get("content", "")
            if text:
                assistant_texts.append(text)

    if assistant_texts:
        return "\n".join(assistant_texts)

    # Fallback: if no structured events found, return raw output
    # (handles plain text output from older codex versions)
    return output


def call_gemini(
    prompt: str,
    *,
    model: str = "gemini-2.5-flash",
) -> str:
    """Call an LLM via the locally installed Gemini CLI.

    Returns the text response.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    ) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        cmd = [
            "gemini",
            "--prompt", f"Read the file {prompt_file} and follow the instructions in it exactly. Return ONLY the requested output format, nothing else.",
            "--model", model,
            "--output-format", "text",
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                f"gemini CLI failed (exit={proc.returncode}): {proc.stderr[:500]}"
            )

        output = proc.stdout.strip()
        if not output:
            raise RuntimeError(
                f"gemini CLI returned no output (exit={proc.returncode}): {proc.stderr[:500]}"
            )
        return output
    finally:
        Path(prompt_file).unlink(missing_ok=True)
