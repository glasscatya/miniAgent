"""Tool registry and the built-in `pwsh` tool.

A tool is metadata (name, description, JSON-schema parameters) plus a handler
``(arguments, runtime) -> str``. Handlers return text for the model and do not
raise on bad input: a tool error is data the model can read and fix.

Registration happens in `build_default_registry()`, which the composition root
(`agent.py`) calls once after the config is loaded. It cannot happen at import
time because the `pwsh` schema embeds the configured default timeout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from config import MAX_TOOL_TIMEOUT, Config, Runtime

PS_PREAMBLE = (
    "$ProgressPreference = 'SilentlyContinue'; "
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [Console]::OutputEncoding; "
)


@dataclass(frozen=True)
class Tool:
    """One callable tool: its API-facing spec plus the handler behind it."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], Runtime], str]

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Name -> Tool dispatch. One instance per run."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool `{tool.name}` is already registered")
        self._tools[tool.name] = tool

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    def execute(self, name: str, arguments_json: str, runtime: Runtime) -> str:
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(f"`{item}`" for item in self._tools) or "(none)"
            return f"error: unknown tool `{name}`; available tools: {available}."
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError as exc:
            return f"error: invalid tool arguments: {exc}"
        if not isinstance(arguments, dict):
            return "error: tool arguments must be a JSON object."
        return tool.handler(arguments, runtime)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n\n... [truncated {len(text) - limit} characters] ...\n\n"
    room = limit - len(marker)
    if room <= 0:
        return text[:limit]
    head = (room * 3) // 4
    return text[:head] + marker + text[-(room - head) :]


def kill_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except OSError:
        pass


def run_pwsh(command: str, timeout: float, cwd: Path, shell: str, max_output: int) -> str:
    script = f"{PS_PREAMBLE}\n{command}\nif ($null -ne $LASTEXITCODE) {{ exit $LASTEXITCODE }}\n"
    env = os.environ.copy()
    env.update(
        {"NO_COLOR": "1", "TERM": "dumb", "GIT_TERMINAL_PROMPT": "0", "PYTHONIOENCODING": "utf-8"}
    )
    try:
        process = subprocess.Popen(
            [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except OSError as exc:
        return f"error: could not start PowerShell: {exc}"

    started = time.monotonic()
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
    except KeyboardInterrupt:
        kill_tree(process)
        raise

    header = (
        f"timed_out: true (limit {timeout:g}s; process tree terminated)"
        if timed_out
        else f"exit_code: {process.returncode}"
    )
    result = "\n".join(
        [
            header,
            f"duration_ms: {round((time.monotonic() - started) * 1000)}",
            "stdout:",
            stdout.rstrip("\r\n") if stdout.strip() else "(empty)",
            "stderr:",
            stderr.rstrip("\r\n") if stderr.strip() else "(empty)",
        ]
    )
    return clip(result, max_output)


def _pwsh_handler(arguments: dict[str, Any], runtime: Runtime) -> str:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return "error: `command` must be a non-empty string."
    try:
        timeout = float(arguments.get("timeout_seconds", runtime.config.shell_timeout))
    except (TypeError, ValueError):
        return "error: `timeout_seconds` must be a number."
    if not 1 <= timeout <= MAX_TOOL_TIMEOUT:
        return f"error: `timeout_seconds` must be between 1 and {MAX_TOOL_TIMEOUT}."

    print(f"[tool] pwsh: {' '.join(command.split())[:240]}", file=sys.stderr, flush=True)
    result = run_pwsh(command, timeout, runtime.cwd, runtime.shell, runtime.config.max_output)
    status = (
        f"timed out after {timeout:g}s"
        if result.startswith("timed_out:")
        else result.splitlines()[0]
    )
    print(f"[tool] {status}", file=sys.stderr, flush=True)
    return result


def pwsh_tool(default_timeout: int) -> Tool:
    """Build the `pwsh` tool spec, embedding the configured default timeout."""
    return Tool(
        name="pwsh",
        description=(
            "Run a PowerShell command on Windows. Each call starts a fresh "
            "non-interactive process in the working directory; variables and "
            "location changes do not persist. Returns stdout, stderr, exit code, "
            "and timeout status."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "PowerShell code to run."},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TOOL_TIMEOUT,
                    "description": f"Timeout in seconds; defaults to {default_timeout}.",
                },
            },
            "required": ["command"],
        },
        handler=_pwsh_handler,
    )


def build_default_registry(config: Config) -> ToolRegistry:
    """Create the registry holding every built-in tool."""
    registry = ToolRegistry()
    registry.register(pwsh_tool(config.shell_timeout))
    return registry
