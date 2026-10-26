"""Event-sourced backend: record every effect, rebuild by replaying control flow.

Three ideas, deliberately kept separate:

- The log is the only durable artifact. Nothing is written to disk except this
  append-only JSONL file, one event per line, fsynced before the next effect.
- Replay does not read state back. It re-runs `loop.run_task()` and answers each
  effect from the log, so the control flow is verified instead of assumed. A
  recorded request that no longer matches means the code changed, and that is an
  error, not a silent divergence.
- A tool call is two events, not one. `tool_started` is written *before* the
  process runs, so a crash leaves a record of an effect whose outcome is
  unknown. That window is the part event sourcing cannot close.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from durable import AgentContext, Turn
from errors import AgentError

CRASH_POINTS = ("after-chat", "after-tool-start", "after-tool-exec")


def digest(messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]) -> str:
    """Stable fingerprint of everything sent to the model in one turn."""
    payload = json.dumps(
        {"messages": messages, "tools": tools},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EventLog:
    """Append-only JSONL file. One JSON object per line, flushed and fsynced."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> list[dict[str, Any]]:
        """Parse the log, tolerating a torn last line and nothing else.

        A crash during `write` can leave half a line. Skipping a broken line in
        the middle would hide real corruption, so only the tail is forgiven.
        """
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        events: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    break
                raise AgentError(
                    f"Corrupt event log {self.path} at line {index + 1}."
                ) from None
            if isinstance(event, dict):
                events.append(event)
        return events


class EventContext:
    """Wraps another context and turns every effect into a recorded event."""

    def __init__(
        self,
        backend: AgentContext,
        log: EventLog,
        recorded: Iterable[dict[str, Any]] = (),
        *,
        policy: str = "abort",
        crash_at: str | None = None,
    ) -> None:
        events = list(recorded)
        self._backend = backend
        self._log = log
        self._policy = policy
        self._crash_at = crash_at
        self._chats = sorted(
            (event for event in events if event.get("type") == "chat_completed"),
            key=lambda event: event.get("step", 0),
        )
        self._tools = sorted(
            (event for event in events if event.get("type") == "tool_completed"),
            key=lambda event: event.get("step", 0),
        )
        self._chat_cursor = 0
        self._tool_cursor = 0

        started = {event.get("step") for event in events if event.get("type") == "tool_started"}
        finished = {event.get("step") for event in self._tools}
        self._pending = sorted(int(step) for step in started - finished if step is not None)
        if self._pending:
            self._handle_pending()

    def _handle_pending(self) -> None:
        steps = ", ".join(str(step) for step in self._pending)
        detail = (
            f"tool step(s) {steps} started but never finished: the process died while "
            "the command was running, so its effect on the machine is unknown"
        )
        if self._policy == "retry":
            print(f"[resume] {detail}; running them again (at-least-once).", file=sys.stderr, flush=True)
            return
        raise AgentError(
            f"{detail}. Resuming with `--resume-policy retry` will run them again, "
            "which repeats any non-idempotent effect."
        )

    def _crash(self, point: str) -> None:
        if self._crash_at != point:
            return
        print(f"[crash] simulating power loss at {point}", file=sys.stderr, flush=True)
        os._exit(137)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        step = self._chat_cursor
        request = digest(messages, tools)
        if step < len(self._chats):
            event = self._chats[step]
            if event.get("digest") != request:
                raise AgentError(
                    f"replay diverged at model turn {step}: the recorded request does not "
                    "match this run, so the control flow changed since the log was written."
                )
            self._chat_cursor += 1
            turn = event["turn"]
            return Turn(
                assistant=turn["assistant"],
                calls=turn["calls"],
                finish_reason=turn.get("finish_reason"),
            )

        turn = self._backend.chat(messages, tools)
        self._log.append(
            {
                "type": "chat_completed",
                "step": step,
                "digest": request,
                "turn": {
                    "assistant": turn.assistant,
                    "calls": turn.calls,
                    "finish_reason": turn.finish_reason,
                },
            }
        )
        self._crash("after-chat")
        self._chat_cursor += 1
        return turn

    def call_tool(self, name: str, arguments: str) -> str:
        step = self._tool_cursor
        if step < len(self._tools):
            event = self._tools[step]
            if event.get("name") != name or event.get("arguments") != arguments:
                raise AgentError(
                    f"replay diverged at tool step {step}: recorded `{event.get('name')}` "
                    f"does not match this run's `{name}`."
                )
            self._tool_cursor += 1
            return str(event.get("result", ""))

        # Intent first: after this line the log admits the effect may have
        # happened, even though the result is not recorded yet.
        self._log.append(
            {"type": "tool_started", "step": step, "name": name, "arguments": arguments}
        )
        self._crash("after-tool-start")
        result = self._backend.call_tool(name, arguments)
        self._crash("after-tool-exec")
        self._log.append(
            {
                "type": "tool_completed",
                "step": step,
                "name": name,
                "arguments": arguments,
                "result": result,
            }
        )
        self._tool_cursor += 1
        return result


class ScriptedBackend(Protocol):
    """Minimal backend shape used by the demo and the tests."""

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn: ...
    def call_tool(self, name: str, arguments: str) -> str: ...
