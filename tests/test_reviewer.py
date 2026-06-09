# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the structured spec reviewer and SpecReview schema."""

import io
import json
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
    ChatMessage,
    LLMClient,
    LLMError,
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


def test_parse_review_response_keeps_file_when_llm_provides_it() -> None:
    # When the LLM returns a ``file`` field, it should not be overwritten by
    # the path passed at call time.
    payload = json.dumps({"score": 50, "file": "from-llm.md"})
    review = parse_review_response(payload, file="from-cli.md")
    assert review.file == "from-llm.md"


def test_parse_review_response_raises_on_invalid_json() -> None:
    with pytest.raises(LLMError, match="valid JSON"):
        parse_review_response("not a json document")


def test_parse_review_response_raises_on_schema_violation() -> None:
    payload = json.dumps({"score": 200})
    with pytest.raises(LLMError, match="SpecReview schema"):
        parse_review_response(payload)


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
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        client = OpenAIChatClient(api_key="sk-test", client=http_client)
        with pytest.raises(LLMError, match="HTTP call failed"):
            client.complete_json([{"role": "user", "content": "hi"}])
    finally:
        http_client.close()


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


def test_openai_client_satisfies_protocol() -> None:
    # ``runtime_checkable`` lets the reviewer accept any object exposing
    # ``complete_json``; the concrete client must remain compatible.
    client = OpenAIChatClient(api_key="sk-test")
    assert isinstance(client, LLMClient)


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
