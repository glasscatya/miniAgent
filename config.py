"""Configuration loading and per-run settings for miniAgent.

This module owns the config schema, its validation, the shell lookup and the
upper bound for tool timeouts. It deliberately knows nothing about the network,
the agent loop or the command line.
"""

from __future__ import annotations

import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from errors import ConfigError

MAX_TOOL_TIMEOUT = 600


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    model: str
    temperature: float | None
    max_tokens: int | None
    max_turns: int
    request_timeout: float
    system_prompt: str  # empty means "use prompt.DEFAULT_SYSTEM_PROMPT"
    shell: str
    shell_timeout: int
    max_output: int


@dataclass(frozen=True)
class Runtime:
    """Resolved inputs for one run: settings, shell path, working directory."""

    config: Config
    shell: str
    cwd: Path


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
        system_prompt=(prompt or "").strip(),
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
