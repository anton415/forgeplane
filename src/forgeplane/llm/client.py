# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Minimal LLM client used by the spec reviewer.

The module defines a :class:`Protocol` so any object exposing a
``complete_json`` method can be substituted in tests, and a concrete HTTP
implementation that talks to OpenAI-compatible Chat Completions endpoints
through :mod:`httpx`.

The HTTP client wraps the request in a :mod:`tenacity` retry policy:
transient HTTP failures (rate limits and 5xx server errors) are retried with
exponential backoff up to three attempts, while non-transient failures (4xx
client errors other than 429, malformed JSON, missing fields) raise the
single :class:`LLMError` exception immediately. Every attempt is logged
through the Forgeplane rich-handled logger so ``--verbose`` invocations show
the retry timeline.
"""

import json
from typing import Final, Literal, Protocol, TypedDict, runtime_checkable

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from forgeplane.core.config import get_logger
from forgeplane.llm.redaction import (
    redact_json_decode_error,
    summarize_payload_shape,
)

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

# Total number of HTTP attempts before the retry policy gives up. Matches the
# parent issue's "tenacity retry & error handling" checklist item.
MAX_ATTEMPTS: Final[int] = 3

# Exponential backoff bounds in seconds. Tenacity multiplies the previous
# wait by two on each failure, clamped into the ``[min, max]`` window.
RETRY_WAIT_MIN: Final[float] = 1.0
RETRY_WAIT_MAX: Final[float] = 10.0


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


class LLMRetryableError(LLMError):
    """A transient LLM failure that the retry policy should retry.

    Raised for HTTP 429 (rate limited), HTTP 5xx (server errors) and network
    transport failures (DNS, connection, timeout). It is still an
    :class:`LLMError`, so callers that catch the parent type continue to
    work after the retry budget is exhausted and tenacity re-raises the last
    transient exception.
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


def _is_retryable_status(status_code: int) -> bool:
    """Return ``True`` for HTTP statuses worth retrying.

    Per the parent issue's "tenacity retry & error handling" item: rate
    limits (429) and server-side failures (5xx) are treated as transient and
    eligible for a retry; everything else is a terminal error.
    """
    return status_code == 429 or 500 <= status_code < 600


def _log_attempt(retry_state: RetryCallState) -> None:
    """Log each attempt through the rich-handled Forgeplane logger.

    Called by tenacity before every attempt (including the first), so the
    rich console shows ``LLM request attempt 1/3``, then ``2/3`` and so on
    when transient failures occur.
    """
    _logger.info(
        "LLM request attempt %d/%d",
        retry_state.attempt_number,
        MAX_ATTEMPTS,
    )


def _log_retry(retry_state: RetryCallState) -> None:
    """Log the transient failure that triggered the upcoming backoff sleep.

    Called by tenacity between attempts, only when the previous attempt
    raised an exception that the retry policy classified as retryable. The
    next sleep duration is read directly from the retry state so the log
    line always matches what tenacity is about to do.
    """
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    sleep_seconds = retry_state.next_action.sleep if retry_state.next_action else 0.0
    _logger.warning(
        "LLM call attempt %d failed (%s); retrying in %.1fs",
        retry_state.attempt_number,
        exc,
        sleep_seconds,
    )


class OpenAIChatClient:
    """HTTP client for OpenAI-compatible Chat Completions endpoints.

    The client requests ``response_format={"type": "json_object"}`` so the
    model is forced to return a JSON document. Schema validation happens in
    the reviewer module via Pydantic; this layer only guarantees that the
    payload is syntactically a JSON object.

    By default the auto-created transport runs with ``trust_env=False`` so the
    LLM request path never implicitly trusts proxy or certificate variables
    that may have been planted in an untrusted project directory. Pass
    ``trust_env=True`` to opt back into ambient transport configuration.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        trust_env: bool = False,
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
        # ``trust_env`` controls whether the auto-created httpx client honours
        # ambient transport variables (HTTP(S)_PROXY, NO_PROXY, SSL_CERT_FILE,
        # SSLKEYLOGFILE, .netrc). It defaults to ``False`` so a proxy or
        # certificate variable that leaked from an untrusted project directory
        # cannot silently redirect or intercept outbound LLM requests carrying
        # the API key and reviewed spec contents. An operator who genuinely
        # runs Forgeplane behind a corporate proxy can opt back in explicitly.
        self._trust_env = trust_env
        # ``client`` is exposed so tests can inject an ``httpx.Client`` wired
        # to ``httpx.MockTransport``; production callers leave it ``None`` and
        # the client is constructed on demand inside ``complete_json``.
        self._client = client

    @retry(
        # ``reraise=True`` propagates the last underlying exception once the
        # retry budget is spent instead of wrapping it in tenacity's own
        # ``RetryError``, so callers still catch :class:`LLMError`.
        reraise=True,
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        # Only transient failures are retried; permanent ones (bad request,
        # malformed JSON, missing fields) raise the base ``LLMError`` and
        # short-circuit the retry loop.
        retry=retry_if_exception_type(LLMRetryableError),
        before=_log_attempt,
        before_sleep=_log_retry,
    )
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
            client = self._client or httpx.Client(
                timeout=self._timeout, trust_env=self._trust_env
            )
            try:
                response = client.post(url, headers=headers, json=payload)
                # Split 429/5xx from other HTTP errors so the retry policy
                # only fires on transient server-side problems; everything
                # else surfaces immediately as a terminal LLMError below.
                if _is_retryable_status(response.status_code):
                    raise LLMRetryableError(
                        f"LLM HTTP call failed with status {response.status_code}"
                    )
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
        except httpx.TransportError as exc:
            # Network-level failures (DNS, connection refused, timeout) are
            # transient and worth retrying alongside 429/5xx.
            raise LLMRetryableError(f"LLM HTTP call failed: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM HTTP call failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            # The raw response body is attacker- or provider-controlled, so only
            # the redacted reason/position is surfaced; ``__cause__`` keeps the
            # full ``json.JSONDecodeError`` available for internal debugging.
            raise LLMError(
                f"LLM response was not valid JSON: {redact_json_decode_error(exc)}"
            ) from exc

        try:
            # OpenAI's Chat Completions returns the model output under
            # ``choices[0].message.content``; we surface the raw string so the
            # reviewer can run JSON parsing and Pydantic validation in one
            # place.
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            # An unexpected response shape could carry provider- or prompt-
            # controlled text, so report only the missing field path and a
            # content-free shape summary instead of the decoded payload.
            raise LLMError(
                "LLM response missing choices[0].message.content; "
                f"received {summarize_payload_shape(data)}"
            ) from exc
