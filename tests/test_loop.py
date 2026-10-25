"""Agent-loop tests: scripted model responses, fake and real registry dispatch."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from typing import Any
from unittest import mock

import loop
from errors import AgentError
from tests.helpers import make_config, make_runtime
from tools import Tool, ToolRegistry


def response(
    content: str | None = None,
    calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    message: dict[str, Any] = {"content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


def tool_call(
    name: str = "fake", arguments: str = "{}", call_id: str = "call_1"
) -> dict[str, Any]:
    return {"id": call_id, "function": {"name": name, "arguments": arguments}}


class RunAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = make_runtime(config=make_config(max_turns=3))
        self.registry = ToolRegistry()
        self.registry.register(
            Tool(
                name="fake",
                description="fake tool",
                parameters={"type": "object", "properties": {}},
                handler=lambda arguments, runtime: f"out:{arguments.get('n', 0)}",
            )
        )
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]

    def test_returns_text_without_tool_calls(self) -> None:
        with mock.patch.object(loop, "chat", return_value=response("hello")) as chat:
            answer = loop.run_agent(self.runtime, self.messages, self.registry)
        self.assertEqual(answer, "hello")
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(chat.call_args.args[2], self.registry.specs())
        self.assertEqual(len(self.messages), 3)

    def test_executes_tool_call_and_continues(self) -> None:
        responses = [
            response(None, [tool_call(arguments='{"n": 1}')], "tool_calls"),
            response("done"),
        ]
        with mock.patch.object(loop, "chat", side_effect=responses) as chat:
            answer = loop.run_agent(self.runtime, self.messages, self.registry)
        self.assertEqual(answer, "done")
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(self.messages[2]["role"], "assistant")
        self.assertEqual(self.messages[2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(
            self.messages[3],
            {"role": "tool", "tool_call_id": "call_1", "content": "out:1"},
        )
        self.assertEqual(self.messages[-1], {"role": "assistant", "content": "done"})

    def test_multiple_tool_calls_in_one_turn(self) -> None:
        responses = [
            response(
                None,
                [
                    tool_call(arguments='{"n": 1}', call_id="a"),
                    tool_call(arguments='{"n": 2}', call_id="b"),
                ],
                "tool_calls",
            ),
            response("done"),
        ]
        with mock.patch.object(loop, "chat", side_effect=responses):
            loop.run_agent(self.runtime, self.messages, self.registry)
        tool_messages = [message for message in self.messages if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["a", "b"])
        self.assertEqual([message["content"] for message in tool_messages], ["out:1", "out:2"])

    def test_unknown_tool_is_reported_to_the_model(self) -> None:
        responses = [
            response(None, [tool_call(name="ghost")], "tool_calls"),
            response("done"),
        ]
        with mock.patch.object(loop, "chat", side_effect=responses):
            answer = loop.run_agent(self.runtime, self.messages, self.registry)
        self.assertEqual(answer, "done")
        self.assertIn("unknown tool `ghost`", self.messages[3]["content"])

    def test_turn_limit_raises_agent_error(self) -> None:
        self.runtime = make_runtime(config=make_config(max_turns=2))
        responses = [response(None, [tool_call()], "tool_calls")] * 2
        with mock.patch.object(loop, "chat", side_effect=responses) as chat:
            with self.assertRaises(AgentError) as ctx:
                loop.run_agent(self.runtime, self.messages, self.registry)
        self.assertIn("Stopped after 2 turns", str(ctx.exception))
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(len(self.messages), 6)

    def test_length_finish_reason_warns(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            loop, "chat", return_value=response("partial", finish_reason="length")
        ), redirect_stderr(stderr):
            answer = loop.run_agent(self.runtime, self.messages, self.registry)
        self.assertEqual(answer, "partial")
        self.assertIn("model hit max_tokens", stderr.getvalue())

    def test_empty_answer_is_returned_as_empty_string(self) -> None:
        with mock.patch.object(loop, "chat", return_value=response(None)):
            answer = loop.run_agent(self.runtime, self.messages, self.registry)
        self.assertEqual(answer, "")

    def test_api_errors_propagate(self) -> None:
        with mock.patch.object(loop, "chat", side_effect=AgentError("boom")):
            with self.assertRaises(AgentError):
                loop.run_agent(self.runtime, self.messages, self.registry)


if __name__ == "__main__":
    unittest.main()
