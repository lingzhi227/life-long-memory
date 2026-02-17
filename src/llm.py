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
) -> str:
    """Dispatch an LLM call to the appropriate backend based on source.

    Args:
        prompt: The prompt text to send.
        source: Session source (e.g. "claude_code", "codex", "gemini").
            Used to pick the backend. Falls back to any available CLI.
        model: Model override. If None, uses the backend's default.

    Returns:
        The text response from the LLM.
    """
    backend = _resolve_backend(source)
    effective_model = model or DEFAULT_MODELS[backend]

    dispatch = {
        "claude": call_claude,
        "codex": call_codex,
        "gemini": call_gemini,
    }
    return dispatch[backend](prompt, model=effective_model)


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
            "--ephemeral",
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
        return output
    finally:
        Path(prompt_file).unlink(missing_ok=True)


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
