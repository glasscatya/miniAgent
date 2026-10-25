#!/usr/bin/env python3
"""miniAgent: an OpenAI-compatible agent loop with one PowerShell tool."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # 装饰层是可选的：缺失或损坏时静默降级为无横幅
    from banner import show_banner
except Exception:  # noqa: BLE001

    def show_banner(**_kwargs: Any) -> bool:
        return False


APP_NAME = "miniAgent"
CONFIG_PATH = Path(__file__).resolve().with_name("config.toml")
MAX_TOOL_TIMEOUT = 600
MAX_API_ATTEMPTS = 3
RETRYABLE_HTTP = {408, 409, 429, 500, 502, 503, 504, 529}

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

PS_PREAMBLE = (
    "$ProgressPreference = 'SilentlyContinue'; "
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [Console]::OutputEncoding; "
)


class ConfigError(Exception):
    pass


class AgentError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    model: str
    temperature: float | None
    max_tokens: int | None
    max_turns: int
    request_timeout: float
    system_prompt: str
    shell: str
    shell_timeout: int
    max_output: int


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from None

    auth = raw.get("auth") or {}
    base = raw.get("base") or {}
    model = raw.get("model") or {}
    agent = raw.get("agent") or {}
    shell = raw.get("shell") or {}
    if not all(isinstance(item, dict) for item in (auth, base, model, agent, shell)):
        raise ConfigError("Config sections auth/base/model/agent/shell must be tables.")

    api_key = os.environ.get("MINIAGENT_API_KEY", "").strip()
    if not api_key:
        api_key = str(auth.get("api_key") or "").strip()
    base_url = str(base.get("url") or "").strip().rstrip("/")
    model_name = str(model.get("name") or "").strip()

    if not api_key:
        raise ConfigError("API key is empty. Set `auth.api_key` or MINIAGENT_API_KEY.")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError("`base.url` must be an http(s) URL.")
    if not model_name:
        raise ConfigError("`model.name` is required.")

    try:
        raw_temperature = model.get("temperature")
        temperature = None if raw_temperature is None else float(raw_temperature)
        raw_max_tokens = model.get("max_tokens")
        max_tokens = None if raw_max_tokens is None else int(raw_max_tokens)
        max_turns = int(agent.get("max_turns", 30))
        request_timeout = float(agent.get("request_timeout", 120))
        shell_timeout = int(shell.get("timeout_seconds", 120))
        max_output = int(shell.get("max_output_chars", 30_000))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid numeric setting: {exc}") from None

    if max_tokens is not None and max_tokens <= 0:
        raise ConfigError("`model.max_tokens` must be positive.")
    if max_turns <= 0 or request_timeout <= 0:
        raise ConfigError("`agent.max_turns` and `agent.request_timeout` must be positive.")
    if not 1 <= shell_timeout <= MAX_TOOL_TIMEOUT:
        raise ConfigError(f"`shell.timeout_seconds` must be between 1 and {MAX_TOOL_TIMEOUT}.")
    if max_output < 500:
        raise ConfigError("`shell.max_output_chars` must be at least 500.")

    prompt = agent.get("system_prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise ConfigError("`agent.system_prompt` must be a string.")

    return Config(
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        max_turns=max_turns,
        request_timeout=request_timeout,
        system_prompt=(prompt or DEFAULT_SYSTEM_PROMPT).strip(),
        shell=str(shell.get("executable") or "pwsh").strip(),
        shell_timeout=shell_timeout,
        max_output=max_output,
    )


def resolve_shell(preferred: str) -> str:
    found = shutil.which(preferred)
    if found:
        return found
    candidate = Path(preferred).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    if candidate.stem.lower() == "pwsh":
        found = shutil.which("powershell")
        if found:
            print("[shell] pwsh not found; using Windows PowerShell.", file=sys.stderr)
            return found
    raise ConfigError(
        f"PowerShell not found: {preferred}. Install PowerShell 7 or set `shell.executable`."
    )


def build_tools(default_timeout: int) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "pwsh",
                "description": (
                    "Run a PowerShell command on Windows. Each call starts a fresh "
                    "non-interactive process in the working directory; variables and "
                    "location changes do not persist. Returns stdout, stderr, exit code, "
                    "and timeout status."
                ),
                "parameters": {
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
            },
        }
    ]


def system_message(config: Config, cwd: Path) -> dict[str, str]:
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return {
        "role": "system",
        "content": (
            f"{config.system_prompt}\n\nRuntime:\n"
            f"- OS: {platform.platform()}\n"
            f"- Working directory: {cwd}\n"
            f"- Current time: {now}"
        ),
    }


def _api_error_detail(code: int, body: str) -> str:
    detail = body.strip()
    try:
        parsed = json.loads(body)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or detail)
        elif isinstance(error, str):
            detail = error
        elif isinstance(parsed, dict):
            detail = str(parsed.get("message") or detail)
    except json.JSONDecodeError:
        pass
    return f"HTTP {code}: {' '.join(detail.split())[:1_000] or '(empty body)'}"


def chat(config: Config, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": config.model, "messages": messages, "tools": tools}
    if config.temperature is not None:
        payload["temperature"] = config.temperature
    if config.max_tokens is not None:
        payload["max_tokens"] = config.max_tokens

    endpoint = config.base_url
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{APP_NAME}/0.1",
        },
        method="POST",
    )

    for attempt in range(MAX_API_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=config.request_timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                raise AgentError(f"API returned invalid JSON: {exc}") from None
            if not isinstance(data, dict):
                raise AgentError("API returned a non-object JSON response.")
            return data
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            detail = _api_error_detail(exc.code, body)
            if exc.code not in RETRYABLE_HTTP or attempt == MAX_API_ATTEMPTS - 1:
                raise AgentError(detail) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            detail = f"Network error: {getattr(exc, 'reason', exc)}"
            if attempt == MAX_API_ATTEMPTS - 1:
                raise AgentError(detail) from None
        delay = 2 ** attempt
        print(f"[api] {detail}; retrying in {delay}s...", file=sys.stderr, flush=True)
        time.sleep(delay)

    raise AgentError("API request failed.")


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return str(content)


def parse_assistant(data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        raise AgentError(f"API response has no choices[0].message: {str(data)[:500]}") from None
    if not isinstance(message, dict):
        raise AgentError("API response message is not an object.")

    calls: list[dict[str, Any]] = []
    for raw_call in message.get("tool_calls") or []:
        if not isinstance(raw_call, dict) or not isinstance(raw_call.get("function"), dict):
            continue
        function = raw_call["function"]
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments", "{}")
        if arguments is None:
            arguments = "{}"
        elif not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        calls.append(
            {
                "id": str(raw_call.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )

    assistant: dict[str, Any] = {"role": "assistant", "content": content_text(message.get("content"))}
    if calls:
        assistant["tool_calls"] = calls
    finish_reason = choice.get("finish_reason")
    return assistant, calls, str(finish_reason) if finish_reason is not None else None


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n\n... [truncated {len(text) - limit} characters] ...\n\n"
    room = limit - len(marker)
    if room <= 0:
        return text[:limit]
    head = (room * 3) // 4
    return text[:head] + marker + text[-(room - head):]


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


def execute_tool(name: str, arguments_json: str, *, config: Config, shell: str, cwd: Path) -> str:
    if name != "pwsh":
        return f"error: unknown tool `{name}`; only `pwsh` is available."
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as exc:
        return f"error: invalid tool arguments: {exc}"
    if not isinstance(arguments, dict):
        return "error: tool arguments must be a JSON object."

    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return "error: `command` must be a non-empty string."
    try:
        timeout = float(arguments.get("timeout_seconds", config.shell_timeout))
    except (TypeError, ValueError):
        return "error: `timeout_seconds` must be a number."
    if not 1 <= timeout <= MAX_TOOL_TIMEOUT:
        return f"error: `timeout_seconds` must be between 1 and {MAX_TOOL_TIMEOUT}."

    print(f"[tool] pwsh: {' '.join(command.split())[:240]}", file=sys.stderr, flush=True)
    result = run_pwsh(command, timeout, cwd, shell, config.max_output)
    status = f"timed out after {timeout:g}s" if result.startswith("timed_out:") else result.splitlines()[0]
    print(f"[tool] {status}", file=sys.stderr, flush=True)
    return result


def run_agent(
    config: Config,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    shell: str,
    cwd: Path,
) -> str:
    for _ in range(config.max_turns):
        assistant, calls, finish_reason = parse_assistant(chat(config, messages, tools))
        messages.append(assistant)
        if not calls:
            if finish_reason == "length":
                print("[warn] model hit max_tokens.", file=sys.stderr)
            return content_text(assistant["content"])
        for call in calls:
            function = call["function"]
            result = execute_tool(
                function["name"], function["arguments"], config=config, shell=shell, cwd=cwd
            )
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    raise AgentError(
        f"Stopped after {config.max_turns} turns without an answer. "
        "Increase `agent.max_turns` to continue."
    )


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(description="Minimal OpenAI-compatible agent with a PowerShell tool.")
    parser.add_argument("prompt", nargs="*", help="Task to run once; omit for interactive mode.")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to config.toml.")
    parser.add_argument("--cwd", help="Working directory for PowerShell (default: current directory).")
    parser.add_argument("--no-banner", action="store_true", help="Hide the ASCII banner.")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd().resolve()
    try:
        config = load_config(Path(args.config).expanduser().resolve())
        if not cwd.is_dir():
            raise ConfigError(f"Working directory does not exist: {cwd}")
        shell = resolve_shell(config.shell)
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    tools = build_tools(config.shell_timeout)
    system = system_message(config, cwd)
    prompt = " ".join(args.prompt).strip()

    if prompt:
        messages = [system, {"role": "user", "content": prompt}]
        try:
            answer = run_agent(config, messages, tools, shell, cwd)
        except AgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
        if answer.strip():
            print(answer)
        else:
            print("[warn] model returned no text.", file=sys.stderr)
        return 0

    messages: list[dict[str, Any]] = [system]
    show_banner(enabled=not args.no_banner)
    print(f"{APP_NAME} | model={config.model} | cwd={cwd}")
    print(f"shell: {shell}")
    print("Type a task. Commands: /clear, exit.")
    while True:
        try:
            task = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not task:
            continue
        if task.lower() in {"exit", "quit", "/exit", "/quit"}:
            return 0
        if task.lower() == "/clear":
            messages = [system]
            print("Conversation cleared.")
            continue

        messages.append({"role": "user", "content": task})
        try:
            answer = run_agent(config, messages, tools, shell, cwd)
        except AgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            continue
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            continue
        print(f"\n{answer}" if answer.strip() else "\n[warn] model returned no text.")


if __name__ == "__main__":
    raise SystemExit(main())
