# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the untrusted-response redaction helpers (issue #78).

These cover the building blocks directly: every helper must keep enough
diagnostic context (shape, field path, error category, position) while never
echoing an untrusted value back into the summary it returns.
"""

import json

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forgeplane.llm.redaction import (
    _redact_location,
    redact_json_decode_error,
    summarize_payload_shape,
    summarize_validation_error,
)

# A recognisable marker standing in for spec or prompt content that an
# attacker-influenced response might smuggle into an error string. No redacted
# summary may contain it.
SECRET = "SENSITIVE-SPEC-abc123"


class _Sample(BaseModel):
    """Minimal schema mirroring SpecReview's constraints for error coverage."""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# summarize_payload_shape
# ---------------------------------------------------------------------------


def test_summarize_payload_shape_object_reports_field_count() -> None:
    # A dict is described by its field count only — the keys and values, which
    # could carry untrusted text, never appear.
    summary = summarize_payload_shape({"choices": SECRET, "model": SECRET})
    assert summary == "JSON object with 2 field(s)"
    assert SECRET not in summary


def test_summarize_payload_shape_array_reports_item_count() -> None:
    summary = summarize_payload_shape([SECRET, SECRET, SECRET])
    assert summary == "JSON array with 3 item(s)"
    assert SECRET not in summary


def test_summarize_payload_shape_string_reports_length_only() -> None:
    # The string length is useful for debugging a truncated response without
    # exposing any of its characters.
    summary = summarize_payload_shape(SECRET)
    assert summary == f"JSON string of length {len(SECRET)}"
    assert SECRET not in summary


def test_summarize_payload_shape_distinguishes_bool_from_number() -> None:
    # ``bool`` subclasses ``int``; the helper must not mislabel it as a number.
    assert summarize_payload_shape(True) == "JSON boolean"
    assert summarize_payload_shape(10) == "JSON number (int)"
    assert summarize_payload_shape(1.5) == "JSON number (float)"


def test_summarize_payload_shape_handles_null() -> None:
    assert summarize_payload_shape(None) == "JSON null"


def test_summarize_payload_shape_falls_back_to_type_name() -> None:
    # An exotic decoded value is named by its Python type, never its repr.
    summary = summarize_payload_shape(object())
    assert summary == "value of type object"


# ---------------------------------------------------------------------------
# redact_json_decode_error
# ---------------------------------------------------------------------------


def test_redact_json_decode_error_omits_document() -> None:
    # The malformed document holds the untrusted payload; the redacted summary
    # must expose only the reason and position, not the document bytes.
    payload = f'{{"score": "{SECRET}" ' + "broken"
    with pytest.raises(json.JSONDecodeError) as exc_info:
        json.loads(payload)
    summary = redact_json_decode_error(exc_info.value)
    assert SECRET not in summary
    # Position information survives so the failure stays diagnosable.
    assert "line" in summary
    assert "column" in summary


# ---------------------------------------------------------------------------
# summarize_validation_error
# ---------------------------------------------------------------------------


def test_summarize_validation_error_drops_input_value() -> None:
    # A string where an int is expected: Pydantic's default rendering would
    # embed ``input_value='<secret>'``; the summary must not.
    with pytest.raises(ValidationError) as exc_info:
        _Sample.model_validate({"score": SECRET})
    summary = summarize_validation_error(exc_info.value)
    assert SECRET not in summary
    # Field path and a stable error category are preserved for debugging.
    assert "score" in summary
    assert "1 validation error(s)" in summary


def test_summarize_validation_error_reports_nested_location() -> None:
    # A bad list element keeps its index in the path so the failing position is
    # still identifiable, without echoing the offending value.
    with pytest.raises(ValidationError) as exc_info:
        _Sample.model_validate({"score": 50, "notes": [SECRET, 123]})
    summary = summarize_validation_error(exc_info.value)
    assert SECRET not in summary
    assert "notes.1" in summary


def test_summarize_validation_error_hides_short_extra_key() -> None:
    # ``extra_forbidden`` carries the provider-supplied key in ``loc``. Even a
    # short rogue key (well under the length cap) must not be echoed, since the
    # key name itself can be reviewed-spec content.
    with pytest.raises(ValidationError) as exc_info:
        _Sample.model_validate({"score": 50, SECRET: "x"})
    summary = summarize_validation_error(exc_info.value)
    assert SECRET not in summary
    # The summary still records that an unexpected field was rejected.
    assert "<extra field>: extra_forbidden" in summary


def test_summarize_validation_error_hides_long_extra_key() -> None:
    # A long attacker-chosen key must not survive even as a truncated prefix.
    long_key = SECRET * 10
    with pytest.raises(ValidationError) as exc_info:
        _Sample.model_validate({"score": 50, long_key: "x"})
    summary = summarize_validation_error(exc_info.value)
    assert SECRET not in summary
    assert "<extra field>" in summary
    assert "extra_forbidden" in summary


def test_redact_location_caps_long_schema_path() -> None:
    # Backstop guard: a legitimately long (non-extra) schema location is bounded
    # by the length cap so an unusually deep path cannot bloat the summary. The
    # truncation marker signals that the rendered path was cut.
    rendered = _redact_location(("recommendations", "a" * 80), "string_type")
    marker = "…(truncated)"
    assert rendered.endswith(marker)
    # The path kept before the marker is capped at the 60-char budget.
    assert len(rendered) - len(marker) == 60


def test_summarize_validation_error_counts_multiple_errors() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _Sample.model_validate({"score": 999, "notes": "not-a-list"})
    summary = summarize_validation_error(exc_info.value)
    assert "2 validation error(s)" in summary
    assert "score" in summary
    assert "notes" in summary
