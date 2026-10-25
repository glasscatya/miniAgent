"""Tests for the OpenAI-compatible client's pure parsing helpers."""

from __future__ import annotations

import json
import unittest

import llm
from errors import AgentError


class ContentTextTests(unittest.TestCase):
    def test_none_is_empty(self) -> None:
        self.assertEqual(llm.content_text(None), "")

    def test_string_passthrough(self) -> None:
        self.assertEqual(llm.content_text("hello"), "hello")

    def test_content_parts_are_joined(self) -> None:
        parts = [{"type": "text", "text": "a"}, {"text": "b"}, {"type": "image"}]
        self.assertEqual(llm.content_text(parts), "a\nb")

    def test_other_values_are_stringified(self) -> None:
        self.assertEqual(llm.content_text(5), "5")


class ApiErrorDetailTests(unittest.TestCase):
    def test_openai_error_object(self) -> None:
        body = json.dumps({"error": {"message": "bad key"}})
        self.assertEqual(llm._api_error_detail(401, body), "HTTP 401: bad key")

    def test_error_string(self) -> None:
        body = json.dumps({"error": "nope"})
        self.assertEqual(llm._api_error_detail(500, body), "HTTP 500: nope")

    def test_top_level_message(self) -> None:
        body = json.dumps({"message": "oops"})
        self.assertEqual(llm._api_error_detail(400, body), "HTTP 400: oops")

    def test_plain_text_is_whitespace_collapsed(self) -> None:
        self.assertEqual(llm._api_error_detail(502, "  boom\n details "), "HTTP 502: boom details")

    def test_empty_body(self) -> None:
        self.assertEqual(llm._api_error_detail(503, ""), "HTTP 503: (empty body)")

    def test_detail_is_truncated(self) -> None:
        body = json.dumps({"error": {"message": "x" * 2_000}})
        detail = llm._api_error_detail(500, body)
        self.assertEqual(len(detail.split(": ", 1)[1]), 1_000)


class ParseAssistantTests(unittest.TestCase):
    def test_plain_answer(self) -> None:
        assistant, calls, finish = llm.parse_assistant(
            {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        )
        self.assertEqual(assistant, {"role": "assistant", "content": "hi"})
        self.assertEqual(calls, [])
        self.assertEqual(finish, "stop")

    def test_tool_call_is_normalised(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {"name": "pwsh", "arguments": '{"command": "ls"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        assistant, calls, finish = llm.parse_assistant(payload)
        self.assertEqual(assistant["content"], "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call_1")
        self.assertEqual(calls[0]["function"], {"name": "pwsh", "arguments": '{"command": "ls"}'})
        self.assertEqual(assistant["tool_calls"], calls)
        self.assertEqual(finish, "tool_calls")

    def test_missing_call_id_is_generated(self) -> None:
        payload = {
            "choices": [{"message": {"tool_calls": [{"function": {"name": "pwsh"}}]}}]
        }
        _, calls, _ = llm.parse_assistant(payload)
        self.assertTrue(calls[0]["id"].startswith("call_"))

    def test_arguments_are_defaulted_and_encoded(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "pwsh", "arguments": None}},
                            {"function": {"name": "pwsh", "arguments": {"command": "ls"}}},
                        ]
                    }
                }
            ]
        }
        _, calls, _ = llm.parse_assistant(payload)
        self.assertEqual(calls[0]["function"]["arguments"], "{}")
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"command": "ls"})

    def test_malformed_tool_calls_are_skipped(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            None,
                            {"id": "x"},
                            {"function": {}},
                            {"function": {"name": "pwsh"}},
                        ]
                    }
                }
            ]
        }
        _, calls, _ = llm.parse_assistant(payload)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "pwsh")

    def test_content_parts_are_flattened(self) -> None:
        payload = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
        assistant, _, _ = llm.parse_assistant(payload)
        self.assertEqual(assistant["content"], "a\nb")

    def test_missing_finish_reason(self) -> None:
        _, _, finish = llm.parse_assistant({"choices": [{"message": {"content": "hi"}}]})
        self.assertIsNone(finish)

    def test_missing_choices_is_an_agent_error(self) -> None:
        for payload in ({}, {"choices": []}, {"choices": [{}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(AgentError):
                    llm.parse_assistant(payload)

    def test_non_object_message_is_an_agent_error(self) -> None:
        with self.assertRaises(AgentError):
            llm.parse_assistant({"choices": [{"message": "hi"}]})


if __name__ == "__main__":
    unittest.main()
