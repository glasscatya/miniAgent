"""Event-log tests: recording, replay without side effects, resume, divergence."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import events
from durable import Turn
from errors import AgentError
from events import EventContext, EventLog
from loop import run_task
from tests.helpers import make_temp_dir, remove_temp_dir

MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "do it"},
]
TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "fake", "description": "", "parameters": {}}}
]
CALL = {"id": "call_1", "type": "function", "function": {"name": "fake", "arguments": "{}"}}


def tool_turn() -> Turn:
    assistant: dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": [CALL]}
    return Turn(assistant=assistant, calls=[CALL], finish_reason="tool_calls")


def answer_turn(text: str = "done") -> Turn:
    return Turn(
        assistant={"role": "assistant", "content": text}, calls=[], finish_reason="stop"
    )


class ScriptedContext:
    """Feeds queued turns and counts how often the tool really ran."""

    def __init__(self, turns: list[Turn] | None = None, tool_result: str = "ok") -> None:
        self.turns = list(turns or [])
        self.tool_result = tool_result
        self.chat_calls = 0
        self.tool_calls = 0

    def chat(self, messages, tools) -> Turn:
        self.chat_calls += 1
        if not self.turns:
            raise AssertionError("backend ran out of scripted turns")
        return self.turns.pop(0)

    def call_tool(self, name: str, arguments: str) -> str:
        self.tool_calls += 1
        return self.tool_result


class EventLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir()
        self.log = EventLog(self.dir / "events.jsonl")

    def tearDown(self) -> None:
        remove_temp_dir(self.dir)

    def test_append_then_read_roundtrip(self) -> None:
        self.log.append({"type": "a", "value": 1})
        self.log.append({"type": "b", "value": "中文"})
        self.assertEqual(
            self.log.read(), [{"type": "a", "value": 1}, {"type": "b", "value": "中文"}]
        )

    def test_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(self.log.read(), [])

    def test_torn_tail_is_dropped(self) -> None:
        Path(self.log.path).write_text('{"type":"a"}\n{"type":"b"', encoding="utf-8")
        self.assertEqual(self.log.read(), [{"type": "a"}])

    def test_corrupt_middle_line_raises(self) -> None:
        Path(self.log.path).write_text('{"type":"a"}\nbroken\n{"type":"c"}\n', encoding="utf-8")
        with self.assertRaises(AgentError):
            self.log.read()


class EventContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir()
        self.log = EventLog(self.dir / "events.jsonl")

    def tearDown(self) -> None:
        remove_temp_dir(self.dir)

    def test_records_chat_and_tool_in_order(self) -> None:
        backend = ScriptedContext([tool_turn(), answer_turn()])
        ctx = EventContext(backend, self.log)
        answer = run_task(ctx, list(MESSAGES), TOOLS, max_turns=3)
        self.assertEqual(answer, "done")
        self.assertEqual(
            [event["type"] for event in self.log.read()],
            ["chat_completed", "tool_started", "tool_completed", "chat_completed"],
        )

    def test_replay_answers_from_log_without_calling_backend(self) -> None:
        run_task(EventContext(ScriptedContext([tool_turn(), answer_turn()]), self.log),
                 list(MESSAGES), TOOLS, max_turns=3)

        replay = ScriptedContext()
        answer = run_task(
            EventContext(replay, self.log, self.log.read()), list(MESSAGES), TOOLS, max_turns=3
        )
        self.assertEqual(answer, "done")
        self.assertEqual((replay.chat_calls, replay.tool_calls), (0, 0))

    def test_divergent_request_is_rejected(self) -> None:
        run_task(EventContext(ScriptedContext([tool_turn(), answer_turn()]), self.log),
                 list(MESSAGES), TOOLS, max_turns=3)
        with self.assertRaises(AgentError) as caught:
            run_task(
                EventContext(ScriptedContext(), self.log, self.log.read()),
                [{"role": "user", "content": "something else"}],
                TOOLS,
                max_turns=3,
            )
        self.assertIn("diverged", str(caught.exception))

    def test_crash_after_chat_resumes_cleanly(self) -> None:
        backend = ScriptedContext([tool_turn(), answer_turn()])
        with mock.patch.object(events.os, "_exit", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                run_task(
                    EventContext(backend, self.log, crash_at="after-chat"),
                    list(MESSAGES),
                    TOOLS,
                    max_turns=3,
                )
        self.assertEqual(backend.tool_calls, 0)

        resumed = ScriptedContext([answer_turn()], tool_result="ok")
        answer = run_task(
            EventContext(resumed, self.log, self.log.read(), policy="retry"),
            list(MESSAGES),
            TOOLS,
            max_turns=3,
        )
        self.assertEqual(answer, "done")
        self.assertEqual(resumed.tool_calls, 1)

    def test_crash_before_tool_aborts_by_default(self) -> None:
        backend = ScriptedContext([tool_turn(), answer_turn()])
        with mock.patch.object(events.os, "_exit", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                run_task(
                    EventContext(backend, self.log, crash_at="after-tool-start"),
                    list(MESSAGES),
                    TOOLS,
                    max_turns=3,
                )
        self.assertEqual(backend.tool_calls, 0)
        with self.assertRaises(AgentError) as caught:
            EventContext(ScriptedContext(), self.log, self.log.read())
        self.assertIn("never finished", str(caught.exception))

    def test_crash_after_tool_execution_duplicates_effect_on_retry(self) -> None:
        backend = ScriptedContext([tool_turn(), answer_turn()])
        with mock.patch.object(events.os, "_exit", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                run_task(
                    EventContext(backend, self.log, crash_at="after-tool-exec"),
                    list(MESSAGES),
                    TOOLS,
                    max_turns=3,
                )
        self.assertEqual(backend.tool_calls, 1)

        resumed = ScriptedContext([answer_turn()])
        answer = run_task(
            EventContext(resumed, self.log, self.log.read(), policy="retry"),
            list(MESSAGES),
            TOOLS,
            max_turns=3,
        )
        self.assertEqual(answer, "done")
        self.assertEqual(backend.tool_calls + resumed.tool_calls, 2)


if __name__ == "__main__":
    unittest.main()
