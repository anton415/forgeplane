# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Minimal LLM client used by the spec reviewer.

The module defines a :class:`Protocol` so any object exposing a
``complete_json`` method can be substituted in tests, and a concrete HTTP
implementation that talks to OpenAI-compatible Chat Completions endpoints
through :mod:`httpx`.

Retry and richer error handling will land in a follow-up task (the
``tenacity retry & error handling`` checklist item in the parent issue);
this baseline only wires up the request/response path and surfaces a single
:class:`LLMError` exception so callers can catch a stable type.
"""

import json
from typing import Final, Literal, Protocol, TypedDict, runtime_checkable

import httpx

from forgeplane.core.config import get_logger

# Module-level logger so request lifecycle events surface under --verbose.
_logger = get_logger(__name__)

# Default OpenAI Chat Completions endpoint. Compatible providers (Azure
# OpenAI, OpenRouter, local proxies) can override this via the ``base_url``
# argument on :class:`OpenAIChatClient`.
DEFAULT_BASE_URL: Final[str] = "https://api.openai.com/v1"

# Default model used when the CLI does not pass an explicit ``--model``. The
# ``gpt-4o-mini`` family balances cost and JSON-mode reliability, which is
# what the reviewer needs.
DEFAULT_MODEL: Final[str] = "gpt-4o-mini"

# HTTP request timeout in seconds. Long enough to accommodate a full review
# response, short enough to surface dead endpoints quickly.
DEFAULT_TIMEOUT: Final[float] = 30.0


class ChatMessage(TypedDict):
    """OpenAI Chat Completions message payload."""

    # Restricted to the roles the reviewer actually emits; ``assistant`` is
    # accepted for future multi-turn flows.
    role: Literal["system", "user", "assistant"]
    content: str


class LLMError(Exception):
    """Raised when an LLM call fails or returns a malformed response.

    Carries the wrapped low-level exception (HTTP, JSON, schema) on
    ``__cause__`` so callers can inspect the root cause if needed without the
    reviewer having to expose a bespoke type per failure mode.
    """


@runtime_checkable
class LLMClient(Protocol):
    """Anything that can turn a list of messages into a JSON response string.

    The Protocol decouples the reviewer from a specific HTTP library or
    provider so tests can inject a deterministic fake client without spinning
    up an httpx mock server.
    """

    def complete_json(self, messages: list[ChatMessage]) -> str:
        """Send ``messages`` and return the assistant's raw JSON content."""
        # Protocol bodies are never executed at runtime; concrete clients
        # provide the implementation. Exempt from coverage to keep the
        # reported percentage honest.
        ...  # pragma: no cover


class OpenAIChatClient:
    """HTTP client for OpenAI-compatible Chat Completions endpoints.

    The client requests ``response_format={"type": "json_object"}`` so the
    model is forced to return a JSON document. Schema validation happens in
    the reviewer module via Pydantic; this layer only guarantees that the
    payload is syntactically a JSON object.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        # Empty keys are rejected early so a misconfigured ``.env`` fails at
        # construction time rather than on the first network call.
        if not api_key:
            raise ValueError("OpenAIChatClient requires a non-empty api_key.")
        self._api_key = api_key
        self._model = model
        # Strip trailing slashes so the joined URL never contains a double
        # slash regardless of how the user configured ``base_url``.
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # ``client`` is exposed so tests can inject an ``httpx.Client`` wired
        # to ``httpx.MockTransport``; production callers leave it ``None`` and
        # the client is constructed on demand inside ``complete_json``.
        self._client = client

    def complete_json(self, messages: list[ChatMessage]) -> str:
        """Send ``messages`` to the Chat Completions endpoint and return JSON."""
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": messages,
            # Forces the model into JSON mode; the server rejects responses
            # that do not parse as JSON, which keeps the contract with the
            # reviewer module tight.
            "response_format": {"type": "json_object"},
            # Deterministic-ish output makes review results easier to compare
            # across reruns when prompt engineering changes.
            "temperature": 0.0,
        }
        _logger.debug("Calling LLM %s at %s", self._model, url)
        try:
            client = self._client or httpx.Client(timeout=self._timeout)
            try:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                # ``httpx.Response.json`` delegates to the stdlib ``json``
                # module and surfaces decode failures as ``json.JSONDecodeError``
                # (a ``ValueError`` subclass), not as an ``httpx.HTTPError``.
                # The dedicated except clause below converts that into the
                # single stable ``LLMError`` the rest of Forgeplane catches.
                data = response.json()
            finally:
                # Only close clients that we created; injected clients are
                # owned by the caller (typically a test fixture).
                if self._client is None:
                    client.close()
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM HTTP call failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM response was not valid JSON: {exc}") from exc

        try:
            # OpenAI's Chat Completions returns the model output under
            # ``choices[0].message.content``; we surface the raw string so the
            # reviewer can run JSON parsing and Pydantic validation in one
            # place.
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"LLM response missing choices[0].message.content: {data!r}"
            ) from exc
