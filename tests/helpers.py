"""Shared fixtures for the unit tests."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from config import Config, Runtime

# Tests keep their scratch files inside the repo: some sandboxes deny writes to
# the platform temp directory. The directory is git-ignored.
TESTS_TMP = Path(__file__).resolve().parent / ".tmp"


def make_temp_dir() -> Path:
    """Create a scratch directory with default permissions.

    `tempfile.mkdtemp` creates 0700 directories; some Windows sandboxes then
    deny writes inside them, so create the directory ourselves.
    """
    TESTS_TMP.mkdir(parents=True, exist_ok=True)
    path = TESTS_TMP / f"case-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    return path


def make_config(**overrides: Any) -> Config:
    values: dict[str, Any] = {
        "api_key": "sk-test",
        "base_url": "https://example.test/v1",
        "model": "test-model",
        "temperature": None,
        "max_tokens": None,
        "max_turns": 3,
        "request_timeout": 5.0,
        "system_prompt": "",
        "shell": "pwsh",
        "shell_timeout": 30,
        "max_output": 5_000,
    }
    values.update(overrides)
    return Config(**values)


def make_runtime(
    config: Config | None = None,
    shell: str = "pwsh",
    cwd: Path | None = None,
) -> Runtime:
    return Runtime(config=config or make_config(), shell=shell, cwd=cwd or Path.cwd())


def remove_temp_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
