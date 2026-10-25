"""Exception types shared by every miniAgent module.

Two failure domains, deliberately kept apart:

- `ConfigError`: startup problems (bad config.toml, missing shell, missing cwd).
  The entry point reports it and exits with status 2.
- `AgentError`: runtime problems (API failure, malformed response, turn limit).
  The entry point reports it and exits with status 1.

Tool failures are NOT exceptions: a tool returns an ``error: ...`` string so the
model can read the problem and correct itself.
"""

from __future__ import annotations


class ConfigError(Exception):
    """Fatal startup problem; retrying will not help."""


class AgentError(Exception):
    """Runtime failure while talking to the model or driving the loop."""
