"""Entry-point wiring tests: argument handling and early config failures."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import agent
from tests.helpers import make_temp_dir, remove_temp_dir

VALID = """\
[auth]
api_key = "sk-test"

[base]
url = "https://example.test/v1"

[model]
name = "test-model"
"""


class EntryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir()
        self.addCleanup(remove_temp_dir, self.dir)
        self.config_path = self.dir / "config.toml"
        self.config_path.write_text(VALID, encoding="utf-8")

    def test_default_config_path_sits_next_to_entry(self) -> None:
        self.assertEqual(
            agent.CONFIG_PATH, Path(agent.__file__).resolve().with_name("config.toml")
        )

    def test_missing_config_returns_2(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = agent.main(["--config", str(self.dir / "missing.toml"), "task"])
        self.assertEqual(code, 2)
        self.assertIn("error:", stderr.getvalue())

    def test_missing_cwd_returns_2(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = agent.main(
                [
                    "--config",
                    str(self.config_path),
                    "--cwd",
                    str(self.dir / "missing-dir"),
                    "task",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("Working directory does not exist", stderr.getvalue())

    def test_help_exits_zero(self) -> None:
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            agent.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
