"""The durable boundary between the agent's control flow and the outside world.

`loop.run_task()` is written against `AgentContext` and performs no I/O of its
own: every model call and every tool call goes through this interface. That is
the only seam that decides durability, and it matches the Workflow/Activity
split used by durable-execution engines.

Backends that fit behind it:

- `LiveContext` here calls the network and spawns PowerShell directly. Nothing
  is recorded, so a crash loses the task.
- An event-sourced context records each result and returns the recorded value
  when the same control flow runs again, which is what makes resumption
  possible.
- A Temporal backend would implement the same two methods as activities and
  register `run_task` as a workflow; `run_task` itself would not change.

The point of the split is that `run_task` must stay deterministic: no clock
reads, no random numbers, no direct I/O. Anything that can differ between two
runs of the same task belongs behind `AgentContext`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from config import Runtime
from llm import chat, parse_assistant
from tools import ToolRegistry


@dataclass(frozen=True)
class Turn:
    """One normalised model response: message, tool calls, finish reason."""

    assistant: dict[str, Any]
    calls: list[dict[str, Any]]
    finish_reason: str | None


def turn_from_response(data: dict[str, Any]) -> Turn:
    assistant, calls, finish_reason = parse_assistant(data)
    return Turn(assistant=assistant, calls=calls, finish_reason=finish_reason)


class AgentContext(Protocol):
    """The two effects a task can have.

    Both are recorded boundaries, not conveniences: a backend may answer from
    storage instead of calling out, and `run_task` cannot tell the difference.
    """

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        """Ask the model for the next turn."""
        ...

    def call_tool(self, name: str, arguments: str) -> str:
        """Run one tool and return the text handed back to the model."""
        ...


class LiveContext:
    """Unrecorded backend: every call hits the real world."""

    def __init__(self, runtime: Runtime, registry: ToolRegistry) -> None:
        self._runtime = runtime
        self._registry = registry

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        return turn_from_response(chat(self._runtime.config, messages, tools))

    def call_tool(self, name: str, arguments: str) -> str:
        return self._registry.execute(name, arguments, self._runtime)
