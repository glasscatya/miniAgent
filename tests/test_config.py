"""Config loading and validation tests (no network, no child processes)."""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

import config
from errors import ConfigError
from tests.helpers import make_temp_dir, remove_temp_dir

BASE = """\
[auth]
api_key = "sk-file"

[base]
url = "https://example.test/v1"

[model]
name = "test-model"
"""


class LoadConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir()
        self.addCleanup(remove_temp_dir, self.dir)

    def write(self, text: str) -> Path:
        path = self.dir / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def load(self, text: str = BASE, env: dict[str, str] | None = None) -> config.Config:
        values = {"MINIAGENT_API_KEY": ""}
        if env:
            values.update(env)
        with mock.patch.dict(os.environ, values):
            return config.load_config(self.write(text))

    def assert_config_error(self, text: str, fragment: str = "") -> None:
        with self.assertRaises(ConfigError) as ctx:
            self.load(text)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_minimal_config_uses_defaults(self) -> None:
        cfg = self.load()
        self.assertEqual(cfg.api_key, "sk-file")
        self.assertEqual(cfg.base_url, "https://example.test/v1")
        self.assertEqual(cfg.model, "test-model")
        self.assertIsNone(cfg.temperature)
        self.assertIsNone(cfg.max_tokens)
        self.assertEqual(cfg.max_turns, 30)
        self.assertEqual(cfg.request_timeout, 120.0)
        self.assertEqual(cfg.system_prompt, "")
        self.assertEqual(cfg.shell, "pwsh")
        self.assertEqual(cfg.shell_timeout, 120)
        self.assertEqual(cfg.max_output, 30_000)

    def test_explicit_values_are_parsed(self) -> None:
        cfg = self.load(
            BASE
            + """
[agent]
max_turns = 7
request_timeout = 12.5
system_prompt = "  custom  "

[shell]
executable = "pwsh"
timeout_seconds = 45
max_output_chars = 900
"""
        )
        self.assertEqual(cfg.max_turns, 7)
        self.assertEqual(cfg.request_timeout, 12.5)
        self.assertEqual(cfg.system_prompt, "custom")
        self.assertEqual(cfg.shell_timeout, 45)
        self.assertEqual(cfg.max_output, 900)

    def test_temperature_and_max_tokens(self) -> None:
        cfg = self.load(
            BASE.replace(
                'name = "test-model"',
                'name = "test-model"\ntemperature = 0.2\nmax_tokens = 4096',
            )
        )
        self.assertAlmostEqual(cfg.temperature or 0.0, 0.2)
        self.assertEqual(cfg.max_tokens, 4096)

    def test_environment_key_wins(self) -> None:
        cfg = self.load(env={"MINIAGENT_API_KEY": "sk-env"})
        self.assertEqual(cfg.api_key, "sk-env")

    def test_missing_key_is_fatal(self) -> None:
        self.assert_config_error(
            """\
[base]
url = "https://example.test/v1"

[model]
name = "test-model"
""",
            "API key is empty",
        )

    def test_non_table_section_is_fatal(self) -> None:
        self.assert_config_error(
            """\
base = "not-a-table"

[auth]
api_key = "sk"
""",
            "must be tables",
        )

    def test_url_must_be_http(self) -> None:
        self.assert_config_error(
            BASE.replace("https://example.test/v1", "ftp://example.test"), "http(s)"
        )

    def test_model_name_required(self) -> None:
        self.assert_config_error(BASE.replace('name = "test-model"', ""), "model.name")

    def test_invalid_numeric_setting(self) -> None:
        self.assert_config_error(
            BASE + '\n[agent]\nmax_turns = "many"\n', "Invalid numeric setting"
        )

    def test_shell_timeout_bounds(self) -> None:
        self.assert_config_error(
            BASE + "\n[shell]\ntimeout_seconds = 0\n", "must be between 1 and 600"
        )
        self.assert_config_error(
            BASE + "\n[shell]\ntimeout_seconds = 601\n", "must be between 1 and 600"
        )

    def test_max_output_lower_bound(self) -> None:
        self.assert_config_error(BASE + "\n[shell]\nmax_output_chars = 499\n", "at least 500")

    def test_system_prompt_must_be_string(self) -> None:
        self.assert_config_error(BASE + "\n[agent]\nsystem_prompt = 42\n", "must be a string")

    def test_missing_config_file(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            config.load_config(self.dir / "nope.toml")
        self.assertIn("not found", str(ctx.exception))

    def test_malformed_toml(self) -> None:
        self.assert_config_error("= broken", "Cannot read config")


class ResolveShellTests(unittest.TestCase):
    def test_missing_shell_is_fatal(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            config.resolve_shell("miniagent-no-such-shell")
        self.assertIn("PowerShell not found", str(ctx.exception))

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is not installed")
    def test_finds_pwsh_on_path(self) -> None:
        self.assertTrue(Path(config.resolve_shell("pwsh")).is_file())


if __name__ == "__main__":
    unittest.main()
