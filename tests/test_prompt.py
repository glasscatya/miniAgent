"""System prompt assembly tests."""

from __future__ import annotations

import unittest
from pathlib import Path

import prompt
from tests.helpers import make_runtime


class SystemMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cwd = Path.cwd()

    def test_empty_prompt_uses_builtin_default(self) -> None:
        message = prompt.system_message("", self.cwd)
        self.assertEqual(message["role"], "system")
        self.assertIn(prompt.DEFAULT_SYSTEM_PROMPT, message["content"])

    def test_custom_prompt_replaces_the_default(self) -> None:
        message = prompt.system_message("  custom instruction  ", self.cwd)
        self.assertTrue(message["content"].startswith("custom instruction\n\nRuntime:"))
        self.assertNotIn("You are miniAgent", message["content"])

    def test_runtime_context_is_appended(self) -> None:
        content = prompt.system_message(None, self.cwd)["content"]
        self.assertIn(f"- Working directory: {self.cwd}", content)
        self.assertIn("- OS:", content)
        self.assertIn("- Current time:", content)

    def test_runtime_limits_are_added_when_runtime_is_given(self) -> None:
        runtime = make_runtime(cwd=self.cwd)
        content = prompt.system_message(None, self.cwd, runtime)["content"]
        self.assertIn(f"- Shell: {runtime.shell}", content)
        self.assertIn(
            f"- Command timeout: {runtime.config.shell_timeout}s default", content
        )
        self.assertIn(f"- Output limit: {runtime.config.max_output} characters", content)
        self.assertIn(f"- Model turns per task: {runtime.config.max_turns}", content)


if __name__ == "__main__":
    unittest.main()
