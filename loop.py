"""The agent loop: ask the model, execute requested tools, repeat.

Two layers, deliberately separated:

- `run_task()` is the control flow. It is deterministic: no network, no
  subprocess, no clock. Every effect goes through an `AgentContext`, so the same
  function can run live or be replayed against recorded results.
- `run_agent()` binds the live context. It keeps the original signature so the
  composition root and the tests did not have to move.
"""

from __future__ import annotations

import sys
from typing import Any

from config import Runtime
from durable import AgentContext, LiveContext
from errors import AgentError
from tools import ToolRegistry


def run_task(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_turns: int,
) -> str:
    """Run one task until the model answers without requesting a tool.

    `messages` is appended to in place and returned implicitly through `ctx`, so
    a caller that keeps the list (interactive mode) still sees the full history.

    Replaying this function re-prints the `[warn]` line; stderr output is not
    part of the recorded state and is the first visible cost of replay.
    """
    for _ in range(max_turns):
        turn = ctx.chat(messages, tools)
        messages.append(turn.assistant)
        if not turn.calls:
            if turn.finish_reason == "length":
                print("[warn] model hit max_tokens.", file=sys.stderr)
            return turn.assistant["content"]
        for call in turn.calls:
            function = call["function"]
            result = ctx.call_tool(function["name"], function["arguments"])
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    raise AgentError(
        f"Stopped after {max_turns} turns without an answer. "
        "Increase `agent.max_turns` to continue."
    )


def run_agent(
    runtime: Runtime,
    messages: list[dict[str, Any]],
    registry: ToolRegistry,
    ctx: AgentContext | None = None,
) -> str:
    """Run one task, defaulting to the live backend (nothing is recorded)."""
    context = ctx if ctx is not None else LiveContext(runtime, registry)
    return run_task(context, messages, registry.specs(), runtime.config.max_turns)
