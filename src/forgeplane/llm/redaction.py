# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Redaction helpers for untrusted LLM/provider response data.

Malformed or unexpected LLM responses must never be reflected verbatim into
CLI stderr or log output: a provider- or prompt-controlled payload could carry
reviewed spec content, injected instructions, or other sensitive text straight
into CI logs and terminals. The helpers here turn such payloads into concise,
content-free summaries that keep the useful diagnostic signal — the payload
shape, the failing field path, the error category, and bounded sizes — without
echoing any untrusted value.

The functions are deliberately small and side-effect free so the security
property they guarantee (no raw response value reaches a user-facing string)
can be asserted directly in unit tests.
"""

import json
from typing import Final

from pydantic import ValidationError

# Upper bound on how many characters of a single error location we echo. Schema
# field names are short, so this only bites when a provider returns an
# unexpected — and possibly attacker-chosen — extra key, which must not be
# reflected wholesale into an error message.
_MAX_LOCATION_CHARS: Final[int] = 60

# Suffix appended to any value we shorten so a reader can tell the original was
# longer than what is shown.
_TRUNCATION_MARKER: Final[str] = "…(truncated)"


def summarize_payload_shape(value: object) -> str:
    """Describe a decoded payload by type and bounded size only.

    Returns a structural summary such as ``"JSON object with 2 field(s)"`` so an
    unexpected provider response can be reported without echoing any of its keys
    or values. Only the container type and an element/length count are exposed;
    the values themselves never appear in the result.
    """
    # ``bool`` is a subclass of ``int``, so it must be checked first to avoid
    # mislabelling ``True``/``False`` as numbers.
    if isinstance(value, bool):
        return "JSON boolean"
    if value is None:
        return "JSON null"
    if isinstance(value, dict):
        return f"JSON object with {len(value)} field(s)"
    if isinstance(value, list):
        return f"JSON array with {len(value)} item(s)"
    if isinstance(value, str):
        return f"JSON string of length {len(value)}"
    if isinstance(value, (int, float)):
        return f"JSON number ({type(value).__name__})"
    # Any exotic type a custom provider might decode to is described by its
    # Python type name alone, never by its ``repr``.
    return f"value of type {type(value).__name__}"


def redact_json_decode_error(exc: json.JSONDecodeError) -> str:
    """Summarise a JSON decode failure without echoing the malformed document.

    ``json.JSONDecodeError`` retains the offending document on ``exc.doc``. Its
    default string form omits the document and exposes only a reason and a
    position, but we rebuild the message from those safe positional fields so
    the redaction is explicit in code and stays robust even if a non-stdlib
    decoder produced the exception.
    """
    return f"{exc.msg} (line {exc.lineno} column {exc.colno})"


def summarize_validation_error(exc: ValidationError) -> str:
    """Summarise a Pydantic error without echoing untrusted ``input_value``.

    Pydantic's default string form embeds the failing ``input_value`` for every
    error, which for an LLM response is provider- or prompt-derived text. This
    rebuilds the message from the structured error list using only the field
    location and the error category (``type``), so the diagnostic still names
    which field failed and why without reflecting the raw value into stderr or
    logs.
    """
    errors = exc.errors()
    summaries = [
        f"{_redact_location(error['loc'])}: {error['type']}" for error in errors
    ]
    return f"{len(errors)} validation error(s): " + "; ".join(summaries)


def _redact_location(location: tuple[str | int, ...]) -> str:
    """Render a Pydantic error ``loc`` tuple, bounding its length.

    Most locations are short schema field names, but an ``extra_forbidden``
    error carries the provider-supplied key, which could be attacker-chosen and
    arbitrarily long; truncate the rendered path so it cannot be reflected
    wholesale into the summary.
    """
    rendered = ".".join(str(part) for part in location) or "<root>"
    if len(rendered) > _MAX_LOCATION_CHARS:
        return rendered[:_MAX_LOCATION_CHARS] + _TRUNCATION_MARKER
    return rendered
