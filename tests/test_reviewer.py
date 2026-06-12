# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the structured spec reviewer and SpecReview schema."""

import io
import json
import logging
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError
from rich.console import Console
from typer.testing import CliRunner

from forgeplane import cli
from forgeplane.cli import app, build_review_table, print_review
from forgeplane.llm.client import (
    DEFAULT_BASE_URL,
    MAX_ATTEMPTS,
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRetryableError,
    OpenAIChatClient,
)
from forgeplane.specs.reviewer import (
    REVIEW_SYSTEM_PROMPT,
    build_review_messages,
    parse_review_response,
    review_spec_file,
    review_spec_text,
)
from forgeplane.specs.schemas import SpecReview


@pytest.fixture(autouse=True)
def _disable_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # The OpenAI client retry policy waits between attempts via
    # ``tenacity.nap.time.sleep``; muting it keeps the test suite fast while
    # still exercising the full retry control flow (attempt counts, error
    # classification, before/before_sleep callbacks).
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _seconds: None)


def _render(table_or_text: object) -> str:
    # Render to a string buffer with ANSI styling disabled so assertions can
    # match content without escape-code noise.
    buffer = io.StringIO()
    console = Console(file=buffer, width=120, color_system=None)
    console.print(table_or_text)
    return buffer.getvalue()


class FakeLLM:
    """In-memory :class:`LLMClient` used by the reviewer and CLI tests."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        # Captured messages let tests assert on the prompt without depending
        # on the formatting helpers in the reviewer module.
        self.calls: list[list[ChatMessage]] = []

    def complete_json(self, messages: list[ChatMessage]) -> str:
        self.calls.append(messages)
        return self.payload


# ---------------------------------------------------------------------------
# SpecReview schema
# ---------------------------------------------------------------------------


def test_spec_review_accepts_minimal_payload() -> None:
    # A score with empty findings is the simplest valid review and must
    # round-trip through validation without errors.
    review = SpecReview.model_validate({"score": 80})
    assert review.score == 80
    assert review.ambiguities == []
    assert review.missing_acceptance_criteria == []
    assert review.recommendations == []
    assert review.risks == []
    assert review.file is None


def test_spec_review_rejects_score_out_of_range() -> None:
    # Scores are constrained to 0..100 so reports can be compared across
    # tools without normalising each source.
    with pytest.raises(ValidationError):
        SpecReview.model_validate({"score": 150})
    with pytest.raises(ValidationError):
        SpecReview.model_validate({"score": -1})


def test_spec_review_rejects_unknown_fields() -> None:
    # ``extra = "forbid"`` keeps the contract tight against LLMs that invent
    # extra keys; the failure surfaces before the data reaches the CLI.
    with pytest.raises(ValidationError):
        SpecReview.model_validate({"score": 80, "summary": "not in schema"})


def test_spec_review_strips_whitespace_in_strings() -> None:
    # ``str_strip_whitespace = True`` removes the leading/trailing whitespace
    # that some models include around bullet items.
    review = SpecReview.model_validate(
        {"score": 50, "ambiguities": ["  what does 'fast' mean?  "]}
    )
    assert review.ambiguities == ["what does 'fast' mean?"]


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def test_build_review_messages_includes_system_and_user() -> None:
    messages = build_review_messages("## Goal\nShip a small thing.")
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == REVIEW_SYSTEM_PROMPT
    assert messages[1]["role"] == "user"
    # The spec body is wrapped in BEGIN/END markers so prompt injection in the
    # spec is at least visible to the model.
    assert "BEGIN SPEC" in messages[1]["content"]
    assert "## Goal" in messages[1]["content"]


# ---------------------------------------------------------------------------
# Response parsing and validation
# ---------------------------------------------------------------------------


def test_parse_review_response_validates_and_attaches_file() -> None:
    payload = json.dumps(
        {
            "score": 72,
            "ambiguities": ["the term 'fast' is undefined"],
            "missing_acceptance_criteria": ["pagination semantics"],
            "recommendations": ["specify pagination limits"],
            "risks": ["timezone handling"],
        }
    )
    review = parse_review_response(payload, file="todo-module.md")
    assert review.score == 72
    # ``file`` is attached after validation so the LLM does not need to
    # include the path in its response.
    assert review.file == "todo-module.md"
    assert review.ambiguities == ["the term 'fast' is undefined"]


def test_parse_review_response_prefers_caller_file_over_llm_field() -> None:
    # A response may claim a different source file to spoof the report
    # (issue #79). The local caller-provided path is the source of truth and
    # must always override the model-supplied value.
    payload = json.dumps({"score": 50, "file": "from-llm.md"})
    review = parse_review_response(payload, file="from-cli.md")
    assert review.file == "from-cli.md"


def test_parse_review_response_keeps_llm_file_for_inline_reviews() -> None:
    # Without a caller-provided path there is no local truth to prefer; the
    # schema-validated value is retained and neutralised at render time.
    payload = json.dumps({"score": 50, "file": "from-llm.md"})
    review = parse_review_response(payload)
    assert review.file == "from-llm.md"


def test_parse_review_response_raises_on_invalid_json() -> None:
    with pytest.raises(LLMError, match="valid JSON"):
        parse_review_response("not a json document")


def test_parse_review_response_raises_on_schema_violation() -> None:
    payload = json.dumps({"score": 200})
    with pytest.raises(LLMError, match="SpecReview schema"):
        parse_review_response(payload)


# A recognisable marker standing in for reviewed spec or prompt content that a
# malformed/attacker-influenced response could try to smuggle into an error.
# No user-facing error string may contain it (issue #78).
_LEAK_MARKER = "LEAKED-SPEC-CONTENT-zzz999"


def test_parse_review_response_redacts_malformed_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Invalid JSON whose raw bytes embed the secret must surface as an LLMError
    # that names the failure mode without echoing the malformed document.
    payload = f'{{"score": "{_LEAK_MARKER}" broken'
    with pytest.raises(LLMError) as exc_info:
        parse_review_response(payload)
    message = str(exc_info.value)
    assert "valid JSON" in message
    assert _LEAK_MARKER not in message
    assert _LEAK_MARKER not in caplog.text


def test_parse_review_response_redacts_schema_input_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A schema violation where the offending value is prompt-derived text: the
    # error must keep the field path and category but drop the raw input value
    # that Pydantic would otherwise embed.
    payload = json.dumps({"score": _LEAK_MARKER})
    with pytest.raises(LLMError) as exc_info:
        parse_review_response(payload)
    message = str(exc_info.value)
    assert "SpecReview schema" in message
    # Diagnostic context is retained: the failing field and a stable category.
    assert "score" in message
    assert _LEAK_MARKER not in message
    assert _LEAK_MARKER not in caplog.text


def test_parse_review_response_redacts_unexpected_extra_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ``extra="forbid"`` rejects an unexpected key; the rogue key is
    # provider-controlled, so a key named after spec content must not be
    # reflected into the error at all — not even a short prefix of it.
    payload = json.dumps({"score": 80, _LEAK_MARKER * 5: "x"})
    with pytest.raises(LLMError) as exc_info:
        parse_review_response(payload)
    message = str(exc_info.value)
    assert _LEAK_MARKER not in message
    assert "<extra field>" in message
    assert _LEAK_MARKER not in caplog.text


# ---------------------------------------------------------------------------
# End-to-end reviewer flow with a fake LLM
# ---------------------------------------------------------------------------


def test_review_spec_text_routes_through_client() -> None:
    fake = FakeLLM(json.dumps({"score": 65, "recommendations": ["add examples"]}))
    review = review_spec_text(
        "## Goal\nShip a feature.\n",
        fake,
        file="inline.md",
    )
    assert review.score == 65
    assert review.recommendations == ["add examples"]
    assert review.file == "inline.md"
    # The reviewer must call the client exactly once per spec.
    assert len(fake.calls) == 1


def test_review_spec_file_reads_path_and_attaches_name(tmp_path: Path) -> None:
    spec_path = tmp_path / "todo-module.md"
    spec_path.write_text(
        "## Goal\nProvide a todo API.\n## Acceptance Criteria\n- create\n",
        encoding="utf-8",
    )
    fake = FakeLLM(json.dumps({"score": 80}))
    review = review_spec_file(spec_path, fake)
    assert review.file == "todo-module.md"
    # The reviewer must have forwarded the on-disk content into the prompt.
    body = fake.calls[0][1]["content"]
    assert "Provide a todo API." in body


def test_review_spec_file_overrides_model_supplied_file(tmp_path: Path) -> None:
    # End-to-end spoofing guard (issue #79): when the spec is read from disk,
    # a ``file`` value injected into the response — here a spreadsheet formula
    # payload — must never displace the on-disk filename.
    spec_path = tmp_path / "todo-module.md"
    spec_path.write_text("## Goal\nShip it.\n", encoding="utf-8")
    fake = FakeLLM(
        json.dumps({"score": 80, "file": '=HYPERLINK("http://evil.example","open")'})
    )
    review = review_spec_file(spec_path, fake)
    assert review.file == "todo-module.md"


# ---------------------------------------------------------------------------
# OpenAIChatClient: HTTP behaviour
# ---------------------------------------------------------------------------


def test_openai_client_rejects_empty_key() -> None:
    # Empty keys are a common ``.env`` placeholder; failing fast keeps the
    # error message close to the misconfiguration site.
    with pytest.raises(ValueError, match="non-empty"):
        OpenAIChatClient(api_key="")


def test_openai_client_strips_trailing_slash_in_base_url() -> None:
    client = OpenAIChatClient(api_key="sk-test", base_url="https://example.com/v1/")
    assert client._base_url == "https://example.com/v1"


def test_openai_client_posts_json_mode_to_chat_completions() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # Persist enough of the request for the assertions below; this lets
        # one handler cover URL, headers, body, and JSON-mode contract.
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"score": 90})}}]},
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        body = client.complete_json([{"role": "user", "content": "review this"}])
    finally:
        http_client.close()

    assert json.loads(body) == {"score": 90}
    assert captured["url"] == f"{DEFAULT_BASE_URL}/chat/completions"
    assert captured["authorization"] == "Bearer sk-test"
    sent = captured["body"]
    assert isinstance(sent, dict)
    # JSON-mode is what guarantees that the response body parses; without it
    # the reviewer would have to retry on free-form text.
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["temperature"] == 0.0


def test_openai_client_wraps_http_errors_as_llm_error() -> None:
    # Persistent 429 responses exhaust the retry budget and surface as a
    # single ``LLMError`` (the retryable subclass) to the caller. Each
    # attempt hits the mocked transport, so the call counter confirms the
    # retry loop is the path being exercised here, not a single-shot.
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(429, text="rate limited")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        with pytest.raises(LLMError, match="HTTP call failed"):
            client.complete_json([{"role": "user", "content": "hi"}])
    finally:
        http_client.close()
    assert call_count == MAX_ATTEMPTS


def test_openai_client_retries_on_429_then_succeeds() -> None:
    # A flaky upstream that rate-limits the first call and returns a clean
    # 200 on the second must succeed without surfacing an exception: this
    # is the core promise of the tenacity retry policy.
    statuses = [429, 200]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        status = statuses[call_count]
        call_count += 1
        if status == 200:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps({"score": 75})}}]},
            )
        return httpx.Response(status, text="rate limited")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        body = client.complete_json([{"role": "user", "content": "hi"}])
    finally:
        http_client.close()
    assert json.loads(body) == {"score": 75}
    assert call_count == 2


def test_openai_client_retries_on_500_then_succeeds() -> None:
    # Same contract as the 429 test, but exercises the 5xx branch of the
    # retry classifier. Keeps the two error families covered independently
    # so a future regression in one does not silently mask the other.
    statuses = [500, 200]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        status = statuses[call_count]
        call_count += 1
        if status == 200:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps({"score": 50})}}]},
            )
        return httpx.Response(status, text="server error")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        body = client.complete_json([{"role": "user", "content": "hi"}])
    finally:
        http_client.close()
    assert json.loads(body) == {"score": 50}
    assert call_count == 2


def test_openai_client_does_not_retry_on_400() -> None:
    # 4xx responses other than 429 are caller mistakes (bad request, auth);
    # retrying them only wastes time and may dispatch the same malformed
    # call repeatedly. The handler counter guards against that.
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(400, text="bad request")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        with pytest.raises(LLMError, match="HTTP call failed"):
            client.complete_json([{"role": "user", "content": "hi"}])
    finally:
        http_client.close()
    assert call_count == 1


def test_openai_client_retries_on_transport_error_then_succeeds() -> None:
    # Network-level failures (DNS, refused, timeout) reach the client as
    # ``httpx.TransportError``; the retry policy treats them like 5xx so a
    # transient connectivity blip does not poison a whole review run.
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"score": 80})}}]},
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        body = client.complete_json([{"role": "user", "content": "hi"}])
    finally:
        http_client.close()
    assert json.loads(body) == {"score": 80}
    assert call_count == 2


def test_llm_retryable_error_is_llm_error() -> None:
    # Callers that catch the broad ``LLMError`` base type must also catch
    # the retryable variant so existing code keeps working after the retry
    # policy lands.
    assert issubclass(LLMRetryableError, LLMError)


def test_openai_client_wraps_missing_choices_as_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A response without the expected choices array must still produce a
        # typed error instead of a KeyError leaking to the caller.
        return httpx.Response(200, json={"unexpected": "shape"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        with pytest.raises(LLMError, match="choices"):
            client.complete_json([{"role": "user", "content": "hi"}])
    finally:
        http_client.close()


def test_openai_client_redacts_unexpected_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An unexpected provider response is attacker-/provider-controlled: when the
    # expected choices path is missing, the resulting LLMError must describe the
    # payload shape only, never echo the decoded values back to the caller.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": _LEAK_MARKER, "detail": _LEAK_MARKER})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        with pytest.raises(LLMError) as exc_info:
            client.complete_json([{"role": "user", "content": "hi"}])
    finally:
        http_client.close()
    message = str(exc_info.value)
    assert "choices" in message
    # The shape summary survives, but neither the values nor the rogue keys do.
    assert "JSON object with 2 field(s)" in message
    assert _LEAK_MARKER not in message
    assert _LEAK_MARKER not in caplog.text


def test_openai_client_redacts_malformed_json_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A 2xx body that is not JSON reaches the client as a decode error whose
    # document holds the untrusted bytes; the LLMError must not reflect them.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"<html>{_LEAK_MARKER}</html>")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        with pytest.raises(LLMError) as exc_info:
            client.complete_json([{"role": "user", "content": "hi"}])
    finally:
        http_client.close()
    message = str(exc_info.value)
    assert "not valid JSON" in message
    assert _LEAK_MARKER not in message
    assert _LEAK_MARKER not in caplog.text


def test_openai_client_wraps_non_json_body_as_llm_error() -> None:
    # A 2xx response with a non-JSON body raises ``json.JSONDecodeError``
    # inside ``Response.json()``. The client must convert that into the same
    # single ``LLMError`` it advertises for HTTP failures so the CLI keeps
    # surfacing one stable error type regardless of how the upstream
    # endpoint or proxy misbehaves.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        with pytest.raises(LLMError, match="not valid JSON"):
            client.complete_json([{"role": "user", "content": "hi"}])
    finally:
        http_client.close()


def test_openai_client_satisfies_protocol() -> None:
    # ``runtime_checkable`` lets the reviewer accept any object exposing
    # ``complete_json``; the concrete client must remain compatible.
    client = OpenAIChatClient(api_key="sk-test")
    assert isinstance(client, LLMClient)


def test_openai_client_creates_and_closes_default_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Covers the path where ``OpenAIChatClient`` was constructed without an
    # injected ``httpx.Client``: it must create one on demand and close it
    # once the request finishes so connections do not leak between calls.
    close_calls: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"score": 50})}}]},
        )

    real_client_cls = httpx.Client

    def fake_client_factory(*args: object, **kwargs: object) -> httpx.Client:
        # Build a real client wired to MockTransport so the request still
        # round-trips through httpx; only the close() call is instrumented.
        instance = real_client_cls(transport=httpx.MockTransport(handler))
        real_close = instance.close

        def tracked_close() -> None:
            close_calls.append(True)
            real_close()

        # ``method-assign`` keeps mypy happy on the monkey-patched method.
        instance.close = tracked_close  # type: ignore[method-assign]
        return instance

    monkeypatch.setattr("forgeplane.llm.client.httpx.Client", fake_client_factory)
    client = OpenAIChatClient(api_key="sk-test")
    body = client.complete_json([{"role": "user", "content": "hi"}])
    assert json.loads(body) == {"score": 50}
    # Exactly one auto-created client must be closed; the test fails if a
    # future refactor stops releasing the resource.
    assert close_calls == [True]


def _capture_default_client_kwargs(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]
) -> None:
    # Replace ``httpx.Client`` with a factory that records the kwargs the
    # client is constructed with, then returns a real client wired to a
    # MockTransport so ``complete_json`` still completes a round-trip.
    real_client_cls = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"score": 50})}}]},
        )

    def fake_client_factory(*args: object, **kwargs: object) -> httpx.Client:
        captured.update(kwargs)
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("forgeplane.llm.client.httpx.Client", fake_client_factory)


def test_openai_client_default_transport_disables_trust_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Security regression guard (issue #77): the auto-created transport must run
    # with ``trust_env=False`` so proxy or certificate variables that leaked
    # from an untrusted project directory cannot redirect or intercept the
    # outbound request, which carries the API key and the reviewed spec.
    captured: dict[str, object] = {}
    _capture_default_client_kwargs(monkeypatch, captured)
    client = OpenAIChatClient(api_key="sk-test")
    client.complete_json([{"role": "user", "content": "hi"}])
    assert captured["trust_env"] is False


def test_openai_client_trust_env_opt_in_is_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An operator who genuinely runs behind a corporate proxy can opt back into
    # ambient transport configuration; the explicit flag must reach the
    # underlying httpx client unchanged.
    captured: dict[str, object] = {}
    _capture_default_client_kwargs(monkeypatch, captured)
    client = OpenAIChatClient(api_key="sk-test", trust_env=True)
    client.complete_json([{"role": "user", "content": "hi"}])
    assert captured["trust_env"] is True


# ---------------------------------------------------------------------------
# CLI rendering helpers
# ---------------------------------------------------------------------------


def test_build_review_table_lists_every_finding() -> None:
    review = SpecReview(
        file="todo-module.md",
        score=72,
        ambiguities=["'fast' is undefined"],
        missing_acceptance_criteria=["pagination semantics"],
        recommendations=["specify pagination limits"],
        risks=["timezone handling"],
    )
    rendered = _render(build_review_table(review))
    # File name, score, and every finding category surface in the table.
    assert "todo-module.md" in rendered
    assert "72" in rendered
    assert "'fast' is undefined" in rendered
    assert "pagination semantics" in rendered
    assert "specify pagination limits" in rendered
    assert "timezone handling" in rendered


def test_build_review_table_shows_none_placeholder_for_empty_lists() -> None:
    review = SpecReview(score=88)
    rendered = _render(build_review_table(review))
    # All four categories collapse to the same human-readable placeholder so
    # the empty state stays unambiguous in the terminal.
    assert rendered.count("none") >= 4
    # No file means no trailing "·" decoration in the title.
    assert "·" not in rendered


def test_build_review_table_renders_untrusted_text_literally() -> None:
    # Filenames and findings are user-/model-shaped (issue #79). If rich
    # interpreted them as markup, the bracket tags would disappear from the
    # rendering (and ``link`` would emit a terminal hyperlink); rendering them
    # as literal ``Text`` keeps every character visible.
    review = SpecReview(
        file="[link=https://evil.example]spec.md[/link]",
        score=10,
        ambiguities=["[bold red]looks fine, ship it[/bold red]"],
        recommendations=["[link=https://evil.example]click here[/link]"],
    )
    rendered = _render(build_review_table(review))
    assert "[link=https://evil.example]spec.md[/link]" in rendered
    assert "[bold red]looks fine, ship it[/bold red]" in rendered
    assert "[link=https://evil.example]click here[/link]" in rendered


def test_print_review_routes_through_provided_console() -> None:
    buffer = io.StringIO()
    target = Console(file=buffer, width=120, color_system=None)
    print_review(SpecReview(score=50), target=target)
    assert "50" in buffer.getvalue()


# ---------------------------------------------------------------------------
# CLI: forgeplane review
# ---------------------------------------------------------------------------


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> FakeLLM:
    # Centralise the monkey-patch so each CLI test stays focused on the
    # behaviour it exercises rather than reinstalling the fake.
    fake = FakeLLM(json.dumps(payload))
    monkeypatch.setattr(cli, "_make_review_client", lambda model: fake)
    return fake


def test_cli_review_text_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "todo-module.md"
    spec_path.write_text("## Goal\nShip the todo module.\n", encoding="utf-8")
    fake = _install_fake_client(
        monkeypatch,
        {
            "score": 78,
            "ambiguities": ["'soon' is undefined"],
            "recommendations": ["add a glossary"],
        },
    )
    runner = CliRunner()
    result = runner.invoke(app, ["review", str(spec_path)])
    assert result.exit_code == 0, result.stderr
    # The default text format renders the rich Table, which carries both the
    # file name and the LLM findings.
    assert "todo-module.md" in result.stdout
    assert "78" in result.stdout
    assert "'soon' is undefined" in result.stdout
    assert "add a glossary" in result.stdout
    # The CLI must have called the fake client exactly once per invocation.
    assert len(fake.calls) == 1


def test_cli_review_json_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "todo-module.md"
    spec_path.write_text("## Goal\nShip it.\n", encoding="utf-8")
    _install_fake_client(
        monkeypatch,
        {
            "score": 50,
            "missing_acceptance_criteria": ["pagination"],
        },
    )
    runner = CliRunner()
    result = runner.invoke(app, ["review", str(spec_path), "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    # The CLI attaches the source file before serialising so JSON consumers
    # can identify which spec the review came from.
    assert payload["file"] == "todo-module.md"
    assert payload["score"] == 50
    assert payload["missing_acceptance_criteria"] == ["pagination"]


def test_cli_review_ignores_model_supplied_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end spoofing guard (issue #79): a response claiming a different
    # ``file`` must not displace the on-disk filename in the CLI output.
    spec_path = tmp_path / "todo-module.md"
    spec_path.write_text("## Goal\nShip it.\n", encoding="utf-8")
    _install_fake_client(
        monkeypatch,
        {"score": 60, "file": "[link=https://evil.example]spoof.md[/link]"},
    )
    runner = CliRunner()
    result = runner.invoke(app, ["review", str(spec_path), "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["file"] == "todo-module.md"


def test_cli_review_yaml_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "todo-module.md"
    spec_path.write_text("## Goal\nShip it.\n", encoding="utf-8")
    _install_fake_client(monkeypatch, {"score": 95, "risks": ["scope creep"]})
    runner = CliRunner()
    result = runner.invoke(app, ["review", str(spec_path), "--format", "yaml"])
    assert result.exit_code == 0, result.stderr
    payload = yaml.safe_load(result.stdout)
    assert payload["score"] == 95
    assert payload["risks"] == ["scope creep"]


def test_cli_review_surfaces_llm_error_as_bad_parameter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "todo-module.md"
    spec_path.write_text("## Goal\nShip it.\n", encoding="utf-8")
    # An invalid score forces the reviewer to raise LLMError when validating
    # the response; the CLI must convert that into a non-zero exit code.
    _install_fake_client(monkeypatch, {"score": 999})
    runner = CliRunner()
    result = runner.invoke(app, ["review", str(spec_path)])
    assert result.exit_code != 0


def test_cli_review_does_not_leak_response_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end guard (issue #78): a malformed response whose value carries
    # reviewed-spec text must fail the command without that text reaching the
    # terminal output or the logged error line.
    spec_path = tmp_path / "todo-module.md"
    spec_path.write_text("## Goal\nShip it.\n", encoding="utf-8")
    # ``score`` must be an int; a string forces a schema error whose offending
    # value is the secret marker the CLI must not echo.
    _install_fake_client(monkeypatch, {"score": _LEAK_MARKER})

    # Capture exactly what the CLI logs by swapping in a logger wired to an
    # in-memory handler; the command logs the error via ``%s`` so this records
    # the same string a real run would emit through the rich handler.
    logged: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logged.append(record.getMessage())

    def fake_configure_logging(*args: object, **kwargs: object) -> logging.Logger:
        logger = logging.getLogger("forgeplane.test-redaction")
        logger.handlers = [_ListHandler()]
        logger.setLevel("DEBUG")
        logger.propagate = False
        return logger

    monkeypatch.setattr(cli, "configure_logging", fake_configure_logging)

    runner = CliRunner()
    result = runner.invoke(app, ["review", str(spec_path)])
    assert result.exit_code != 0
    # Neither the rendered CLI output nor any logged line may contain the secret.
    assert _LEAK_MARKER not in result.output
    assert _LEAK_MARKER not in "\n".join(logged)
    # The diagnostic is still useful: a failure was logged for the bad response.
    assert any("LLM review failed" in line for line in logged)


def test_cli_review_missing_api_key_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Strip the variable and chdir away from any developer .env so the loader
    # cannot recover a key from the environment.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    spec_path = tmp_path / "todo-module.md"
    spec_path.write_text("## Goal\nShip it.\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["review", str(spec_path)])
    assert result.exit_code != 0


def test_make_review_client_returns_openai_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Covers the success path of the factory: when ``OPENAI_API_KEY`` is
    # set the CLI must hand back a real ``OpenAIChatClient`` rather than the
    # ``BadParameter`` that fires on the missing-key path above.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = cli._make_review_client("gpt-4o-mini")
    assert isinstance(client, OpenAIChatClient)
