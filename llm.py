"""OpenAI-compatible chat client: transport, retries and response parsing.

Everything here speaks the `chat/completions` wire format. The agent loop only
sees two functions: `chat()` returns a response object, `parse_assistant()`
normalises it into messages the loop can append.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from config import Config
from errors import AgentError

MAX_API_ATTEMPTS = 3
RETRYABLE_HTTP = {408, 409, 429, 500, 502, 503, 504, 529}
USER_AGENT = "miniAgent/0.1"


def _api_error_detail(code: int, body: str) -> str:
    detail = body.strip()
    try:
        parsed = json.loads(body)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or detail)
        elif isinstance(error, str):
            detail = error
        elif isinstance(parsed, dict):
            detail = str(parsed.get("message") or detail)
    except json.JSONDecodeError:
        pass
    return f"HTTP {code}: {' '.join(detail.split())[:1_000] or '(empty body)'}"


def chat(config: Config, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": config.model, "messages": messages, "tools": tools}
    if config.temperature is not None:
        payload["temperature"] = config.temperature
    if config.max_tokens is not None:
        payload["max_tokens"] = config.max_tokens

    endpoint = config.base_url
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    for attempt in range(MAX_API_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=config.request_timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                raise AgentError(f"API returned invalid JSON: {exc}") from None
            if not isinstance(data, dict):
                raise AgentError("API returned a non-object JSON response.")
            return data
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            detail = _api_error_detail(exc.code, body)
            if exc.code not in RETRYABLE_HTTP or attempt == MAX_API_ATTEMPTS - 1:
                raise AgentError(detail) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            detail = f"Network error: {getattr(exc, 'reason', exc)}"
            if attempt == MAX_API_ATTEMPTS - 1:
                raise AgentError(detail) from None
        delay = 2**attempt
        print(f"[api] {detail}; retrying in {delay}s...", file=sys.stderr, flush=True)

    raise AgentError("API request failed.")


def content_text(content: Any) -> str:
    """Flatten string or content-part-list message content into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return str(content)


def parse_assistant(data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    """Normalise one API response into an assistant message, tool calls and finish reason."""
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        raise AgentError(f"API response has no choices[0].message: {str(data)[:500]}") from None
    if not isinstance(message, dict):
        raise AgentError("API response message is not an object.")

    calls: list[dict[str, Any]] = []
    for raw_call in message.get("tool_calls") or []:
        if not isinstance(raw_call, dict) or not isinstance(raw_call.get("function"), dict):
            continue
        function = raw_call["function"]
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments", "{}")
        if arguments is None:
            arguments = "{}"
        elif not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        calls.append(
            {
                "id": str(raw_call.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )

    assistant: dict[str, Any] = {"role": "assistant", "content": content_text(message.get("content"))}
    if calls:
        assistant["tool_calls"] = calls
    finish_reason = choice.get("finish_reason")
    return assistant, calls, str(finish_reason) if finish_reason is not None else None
