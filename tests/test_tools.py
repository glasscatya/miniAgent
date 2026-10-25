"""Tool registry, argument validation, and real pwsh execution tests."""

from __future__ import annotations

import json
import shutil
import unittest
from unittest import mock

import tools
from tests.helpers import make_config, make_runtime, make_temp_dir, remove_temp_dir


class ClipTests(unittest.TestCase):
    def test_short_text_is_untouched(self) -> None:
        self.assertEqual(tools.clip("abc", 10), "abc")

    def test_text_at_limit_is_untouched(self) -> None:
        self.assertEqual(tools.clip("abcde", 5), "abcde")

    def test_long_text_keeps_head_and_tail(self) -> None:
        text = "a" * 100 + "b" * 100
        result = tools.clip(text, 100)
        self.assertEqual(len(result), 100)
        self.assertIn("truncated 100 characters", result)
        self.assertTrue(result.startswith("a"))
        self.assertTrue(result.endswith("b"))

    def test_limit_smaller_than_marker(self) -> None:
        self.assertEqual(tools.clip("abcdefghij", 5), "abcde")


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = make_config()
        self.runtime = make_runtime(config=self.config)
        self.registry = tools.build_default_registry(self.config)

    def test_default_registry_contains_only_pwsh(self) -> None:
        self.assertEqual(self.registry.names(), ("pwsh",))
        specs = self.registry.specs()
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["type"], "function")
        function = specs[0]["function"]
        self.assertEqual(function["name"], "pwsh")
        self.assertIn("PowerShell", function["description"])
        self.assertEqual(function["parameters"]["required"], ["command"])
        timeout = function["parameters"]["properties"]["timeout_seconds"]
        self.assertEqual(timeout["maximum"], 600)
        self.assertIn("30", timeout["description"])

    def test_duplicate_registration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register(tools.pwsh_tool(30))

    def test_custom_tool_dispatch(self) -> None:
        self.registry.register(
            tools.Tool(
                name="echo",
                description="echo text",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                handler=lambda arguments, runtime: f"echo:{arguments['text']}",
            )
        )
        self.assertEqual(self.registry.names(), ("pwsh", "echo"))
        self.assertEqual(len(self.registry.specs()), 2)
        self.assertEqual(
            self.registry.execute("echo", '{"text": "hi"}', self.runtime), "echo:hi"
        )

    def test_unknown_tool_error_lists_registered_tools(self) -> None:
        result = self.registry.execute("nope", "{}", self.runtime)
        self.assertTrue(result.startswith("error: unknown tool `nope`"))
        self.assertIn("`pwsh`", result)

    def test_unknown_tool_on_empty_registry(self) -> None:
        result = tools.ToolRegistry().execute("pwsh", "{}", self.runtime)
        self.assertIn("(none)", result)

    def test_invalid_json_arguments(self) -> None:
        result = self.registry.execute("pwsh", "{", self.runtime)
        self.assertIn("invalid tool arguments", result)

    def test_arguments_must_be_an_object(self) -> None:
        result = self.registry.execute("pwsh", "[]", self.runtime)
        self.assertIn("JSON object", result)

    def test_empty_arguments_string_is_treated_as_empty_object(self) -> None:
        result = self.registry.execute("pwsh", "", self.runtime)
        self.assertTrue(result.startswith("error: `command`"))


class PwshValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = make_runtime(config=make_config(shell_timeout=30, max_output=5_000))
        self.registry = tools.build_default_registry(self.runtime.config)

    def execute(self, arguments: dict[str, object]) -> str:
        return self.registry.execute("pwsh", json.dumps(arguments), self.runtime)

    def test_invalid_arguments_never_spawn_a_process(self) -> None:
        cases: list[dict[str, object]] = [
            {},
            {"command": ""},
            {"command": "   "},
            {"command": "hostname", "timeout_seconds": "soon"},
            {"command": "hostname", "timeout_seconds": 0},
            {"command": "hostname", "timeout_seconds": 601},
        ]
        with mock.patch.object(tools, "run_pwsh") as run:
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    self.assertTrue(self.execute(arguments).startswith("error:"))
            run.assert_not_called()

    def test_valid_command_uses_configured_defaults(self) -> None:
        with mock.patch.object(tools, "run_pwsh", return_value="exit_code: 0") as run:
            self.assertEqual(self.execute({"command": "hostname"}), "exit_code: 0")
        run.assert_called_once_with(
            "hostname", 30.0, self.runtime.cwd, self.runtime.shell, 5_000
        )

    def test_explicit_timeout_is_passed_through(self) -> None:
        with mock.patch.object(tools, "run_pwsh", return_value="exit_code: 0") as run:
            self.execute({"command": "hostname", "timeout_seconds": 5})
        self.assertEqual(run.call_args.args[1], 5.0)


PWSH = shutil.which("pwsh")


@unittest.skipUnless(PWSH, "pwsh is not installed")
class RunPwshIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cwd = make_temp_dir()
        self.addCleanup(remove_temp_dir, self.cwd)

    def test_reports_stdout_and_exit_code(self) -> None:
        result = tools.run_pwsh("Write-Output hello-miniagent", 30, self.cwd, str(PWSH), 5_000)
        self.assertIn("exit_code: 0", result)
        self.assertIn("hello-miniagent", result)
        self.assertIn("stderr:", result)

    def test_reports_nonzero_exit_code(self) -> None:
        result = tools.run_pwsh("Write-Error 'boom'; exit 3", 30, self.cwd, str(PWSH), 5_000)
        self.assertIn("exit_code: 3", result)
        self.assertIn("boom", result)


if __name__ == "__main__":
    unittest.main()
