"""System prompt defaults and the runtime context appended to them.

Prompts change for reasons that have nothing to do with config validation or
HTTP, so they live here instead of inside `config.py`.
"""

from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path

DEFAULT_SYSTEM_PROMPT = """You are miniAgent, a pragmatic agent running on Windows.
You have one tool, `pwsh`, which runs PowerShell commands in a fresh process. Each call starts in the configured working directory; variables and location changes do not persist. Chain related statements with `;` or newlines when state matters.

Working style:
- Inspect relevant files before changing them.
- Prefer non-interactive commands.
- Do not start servers or long-running processes unless the task requires it.
- Make focused changes and avoid unrelated refactors.
- Verify your work when possible.
- Be cautious with destructive or sensitive actions.
- When finished, reply with a concise summary of changes, verification, and any remaining issues. Reply in the user's language."""


def system_message(prompt: str | None, cwd: Path) -> dict[str, str]:
    """Build the system message from the configured prompt plus runtime facts."""
    instruction = (prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return {
        "role": "system",
        "content": (
            f"{instruction}\n\nRuntime:\n"
            f"- OS: {platform.platform()}\n"
            f"- Working directory: {cwd}\n"
            f"- Current time: {now}"
        ),
    }
