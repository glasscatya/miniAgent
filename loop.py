"""The agent loop: ask the model, execute requested tools, repeat.

Deliberately thin: it depends on `llm.chat` / `llm.parse_assistant` and on a
`ToolRegistry`, so tests can drive it with scripted responses and fake tools
without touching the network or starting a PowerShell process.
"""

from __future__ import annotations

import sys
from typing import Any

from config import Runtime
from errors import AgentError
from llm import chat, parse_assistant
from tools import ToolRegistry


def run_agent(
    runtime: Runtime,
    messages: list[dict[str, Any]],
    registry: ToolRegistry,
) -> str:
    """Run one task until the model answers without requesting a tool."""
    config = runtime.config
    tools = registry.specs()
    for _ in range(config.max_turns):
        assistant, calls, finish_reason = parse_assistant(chat(config, messages, tools))
        messages.append(assistant)
        if not calls:
            if finish_reason == "length":
                print("[warn] model hit max_tokens.", file=sys.stderr)
            return assistant["content"]
        for call in calls:
            function = call["function"]
            result = registry.execute(function["name"], function["arguments"], runtime)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    raise AgentError(
        f"Stopped after {config.max_turns} turns without an answer. "
        "Increase `agent.max_turns` to continue."
    )
