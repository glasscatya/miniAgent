#!/usr/bin/env python3
"""miniAgent: an OpenAI-compatible agent loop with one PowerShell tool.

This module is the composition root: it parses arguments, loads config, resolves
the shell, builds the tool registry and runs either one task or the REPL.

Usage:
    python agent.py                  # interactive mode
    python agent.py "task"           # run one task
    python agent.py --help

Exit codes: 0 success, 1 runtime failure (API/loop), 2 configuration error,
130 interrupted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from config import Runtime, load_config, resolve_shell
from errors import AgentError, ConfigError
from loop import run_agent
from prompt import system_message
from tools import build_default_registry

try:  # 装饰层是可选的：缺失或损坏时静默降级为无横幅
    from banner import show_banner
except Exception:  # noqa: BLE001

    def show_banner(**_kwargs: Any) -> bool:
        return False


CONFIG_PATH = Path(__file__).resolve().with_name("config.toml")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(
        description="Minimal OpenAI-compatible agent with a PowerShell tool."
    )
    parser.add_argument("prompt", nargs="*", help="Task to run once; omit for interactive mode.")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to config.toml.")
    parser.add_argument(
        "--cwd", help="Working directory for PowerShell (default: current directory)."
    )
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

    runtime = Runtime(config=config, shell=shell, cwd=cwd)
    registry = build_default_registry(config)
    system = system_message(config.system_prompt, cwd, runtime)
    prompt = " ".join(args.prompt).strip()

    if prompt:
        messages = [system, {"role": "user", "content": prompt}]
        try:
            answer = run_agent(runtime, messages, registry)
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
    print(f"miniAgent | model={config.model} | cwd={cwd}")
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
            answer = run_agent(runtime, messages, registry)
        except AgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            continue
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            continue
        print(f"\n{answer}" if answer.strip() else "\n[warn] model returned no text.")


if __name__ == "__main__":
    raise SystemExit(main())
