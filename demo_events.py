#!/usr/bin/env python3
"""Walk through what an event log can and cannot recover.

Runs three scenarios against the same agent, each in its own directory, with a
scripted model and a deliberately non-idempotent tool (appending a charge to a
ledger file). The crash is a real `os._exit`, so each scenario runs in a child
process — no network, no API key, no real PowerShell.

    python demo_events.py

Scenario 1  crash after the model turn is recorded  -> resume, no duplicate
Scenario 2  crash after the intent but before the run -> resume refuses to guess
Scenario 3  crash after the command ran, before the result was recorded
            -> resume with `--resume-policy retry` charges twice
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from durable import Turn
from errors import AgentError
from events import EventContext, EventLog
from loop import run_task

MAX_TURNS = 4
TASK = "charge the customer 5.00"
CHARGE = "charged 5.00"

SYSTEM: dict[str, Any] = {"role": "system", "content": "demo agent"}
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {"name": "ledger", "description": "append a charge", "parameters": {}},
    }
]
CALL: dict[str, Any] = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "ledger", "arguments": "{}"},
}


def tool_turn() -> Turn:
    assistant: dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": [CALL]}
    return Turn(assistant=assistant, calls=[CALL], finish_reason="tool_calls")


def answer_turn() -> Turn:
    return Turn(assistant={"role": "assistant", "content": "charged"}, calls=[], finish_reason="stop")


class DemoBackend:
    """Scripted model plus a tool that writes one line per charge."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        # Decided from the transcript, not from an internal counter, so the same
        # backend behaves identically whether it runs live or after a replay.
        seen_result = any(message.get("role") == "tool" for message in messages)
        return answer_turn() if seen_result else tool_turn()

    def call_tool(self, name: str, arguments: str) -> str:
        with (self.root / "ledger.txt").open("a", encoding="utf-8") as handle:
            handle.write(f"{CHARGE}\n")
            handle.flush()
            os.fsync(handle.fileno())
        return CHARGE


def child(phase: str, root: Path, policy: str, crash_at: str | None) -> int:
    """One process: either a fresh run that may die, or a resume."""
    log = EventLog(root / "events.jsonl")
    events = log.read() if phase == "resume" else []

    if phase == "resume":
        started = next((event for event in events if event.get("type") == "task_started"), None)
        if started is None:
            print("no task recorded", file=sys.stderr)
            return 2
        messages = list(started["messages"])
    else:
        messages = [SYSTEM, {"role": "user", "content": TASK}]
        log.append(
            {"type": "task_started", "task": TASK, "messages": messages, "max_turns": MAX_TURNS}
        )

    try:
        ctx = EventContext(DemoBackend(root), log, events, policy=policy, crash_at=crash_at)
        began = time.perf_counter()
        answer = run_task(ctx, messages, TOOLS, MAX_TURNS)
        elapsed = (time.perf_counter() - began) * 1000
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    log.append({"type": "task_finished", "answer": answer})
    print(f"answer={answer!r} replay_wall_ms={elapsed:.1f} events={len(log.read())}")
    return 0


def ledger(root: Path) -> int:
    path = root / "ledger.txt"
    if not path.exists():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


def spawn(script: Path, root: Path, phase: str, policy: str, crash_at: str | None) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(script), "--child", "--phase", phase, "--dir", str(root),
            "--policy", policy]
    if crash_at:
        args += ["--crash-at", crash_at]
    return subprocess.run(args, capture_output=True, text=True)


def events_size(root: Path) -> str:
    path = root / "events.jsonl"
    return f"{path.stat().st_size} B" if path.exists() else "0 B"


def show(label: str, result: subprocess.CompletedProcess[str]) -> None:
    text = (result.stdout + result.stderr).strip().replace("\n", "\n    ")
    print(f"  {label}: exit={result.returncode}")
    if text:
        print(f"    {text}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Event-sourcing walkthrough for miniAgent.")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--phase", choices=("run", "resume"), default="run")
    parser.add_argument("--policy", choices=("abort", "retry"), default="abort")
    parser.add_argument("--crash-at", default=None)
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent / ".demo"))
    args = parser.parse_args(argv)

    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.child:
        return child(args.phase, root, args.policy, args.crash_at)

    script = Path(__file__).resolve()
    base = root
    for index in range(1, 4):
        shutil.rmtree(base / f"scenario-{index}", ignore_errors=True)
    scenarios = [
        ("1. 崩溃在模型响应落盘之后", "after-chat", "resume-clean"),
        ("2. 崩溃在命令启动之前", "after-tool-start", "resume-abort"),
        ("3. 崩溃在命令执行之后、结果落盘之前", "after-tool-exec", "resume-retry"),
    ]

    print("事件溯源能恢复什么、不能恢复什么\n")
    summary: list[tuple[str, int, str]] = []

    for index, (title, crash_at, mode) in enumerate(scenarios, start=1):
        workdir = base / f"scenario-{index}"
        workdir.mkdir(parents=True, exist_ok=True)
        print(title)
        show("首次运行", spawn(script, workdir, "run", "abort", crash_at))

        if mode == "resume-abort":
            show("恢复（默认 abort）", spawn(script, workdir, "resume", "abort", None))
            outcome = "拒绝继续，拒绝猜测"
            summary.append((title, ledger(workdir), outcome))
            print(f"  ledger 行数: {ledger(workdir)}   事件体积: {events_size(workdir)}\n")
            continue

        if mode == "resume-retry":
            show("恢复（默认 abort）", spawn(script, workdir, "resume", "abort", None))
            show("恢复（--resume-policy retry）", spawn(script, workdir, "resume", "retry", None))
            outcome = "续跑成功，但副作用执行了两次"
            summary.append((title, ledger(workdir), outcome))
            print(f"  ledger 行数: {ledger(workdir)}   事件体积: {events_size(workdir)}\n")
            continue

        show("恢复", spawn(script, workdir, "resume", "abort", None))
        outcome = "续跑成功，副作用只发生一次"
        summary.append((title, ledger(workdir), outcome))
        print(f"  ledger 行数: {ledger(workdir)}   事件体积: {events_size(workdir)}\n")

    print("小结")
    for title, charges, outcome in summary:
        print(f"  {title}")
        print(f"    ledger 行数 = {charges}（应为 1）  {outcome}")
    print("\n结论：事件日志恢复的是上下文，不是现实；")
    print("      intent 事件让不确定窗口可见，但不能把它消掉。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
