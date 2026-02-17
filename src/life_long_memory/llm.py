"""LLM invocation via locally installed CLI tools (no API keys needed).

Uses the Claude Code CLI's built-in OAuth authentication.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


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
