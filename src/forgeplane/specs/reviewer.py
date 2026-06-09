# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""LLM-backed spec reviewer.

The reviewer pipes a Markdown specification through an :class:`LLMClient`,
asks the model to return a JSON document describing readiness findings, and
validates the response against the :class:`SpecReview` Pydantic schema. The
end result is a typed Python object — never free-form text — which downstream
CLI rendering, JSON export, and eval reporting can rely on.
"""

import json
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from forgeplane.core.config import get_logger
from forgeplane.llm.client import ChatMessage, LLMClient, LLMError
from forgeplane.specs.schemas import SpecReview

# Module-level logger so review steps surface under --verbose.
_logger = get_logger(__name__)

# System prompt sent on every review call. The instructions intentionally
# enumerate the JSON keys and reject prose outside the JSON object: this is
# the contract that lets Pydantic validate the response without ad-hoc
# parsing. The schema description mirrors the SpecReview fields so a future
# refactor that renames a field also has to update this prompt.
REVIEW_SYSTEM_PROMPT: Final[str] = (
    "You are a senior software engineer reviewing a Markdown specification "
    "document. Your job is to surface ambiguities, missing acceptance "
    "criteria, additional risks, and concrete recommendations.\n"
    "\n"
    "Return ONLY a JSON object (no prose, no Markdown fences) that conforms "
    "to the following schema:\n"
    "{\n"
    '  "score": integer in 0..100 representing overall readiness '
    "(higher is better),\n"
    '  "ambiguities": array of short strings, each describing one '
    "ambiguous statement,\n"
    '  "missing_acceptance_criteria": array of short strings, each '
    "describing one acceptance criterion the spec should add,\n"
    '  "recommendations": array of short strings, each describing one '
    "actionable improvement,\n"
    '  "risks": array of short strings, each describing one risk the spec '
    "does not already document\n"
    "}\n"
    "Use empty arrays when a category has no findings. Keep every entry to "
    "one sentence."
)

# Wrapper template applied to the user-provided spec body. Tagging the body
# with explicit delimiters makes it harder for prompt-injection attempts in
# the spec to disrupt the system instructions above.
_REVIEW_USER_TEMPLATE: Final[str] = (
    "Review the following Markdown specification and return the JSON object "
    "described in the system prompt.\n"
    "\n"
    "----- BEGIN SPEC -----\n"
    "{spec}\n"
    "----- END SPEC -----\n"
)


def build_review_messages(spec_text: str) -> list[ChatMessage]:
    """Build the chat-completion messages used to review ``spec_text``."""
    # Keeping prompt assembly in one place lets tests assert on the exact
    # payload shape without re-deriving it from the reviewer call site.
    return [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": _REVIEW_USER_TEMPLATE.format(spec=spec_text)},
    ]


def parse_review_response(payload: str, *, file: str | None = None) -> SpecReview:
    """Parse a raw LLM JSON payload into a validated :class:`SpecReview`.

    ``file`` is attached after validation so callers that read the spec from
    disk can preserve the source path on the resulting review even when the
    LLM omits the field from its response.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        # The OpenAI client requests JSON mode, but other providers might
        # not enforce it; surface the decoding failure as an LLMError so the
        # CLI can render a single, stable error class to the user.
        raise LLMError(f"LLM did not return valid JSON: {exc}") from exc

    try:
        review = SpecReview.model_validate(data)
    except ValidationError as exc:
        # Surface the validation error inside an LLMError so callers can
        # catch a single type, while ``__cause__`` keeps the Pydantic detail
        # available for debugging.
        raise LLMError(
            f"LLM JSON does not match the SpecReview schema:\n{exc}"
        ) from exc

    if file is not None and review.file is None:
        # ``model_copy`` keeps the original instance immutable from the
        # caller's perspective while still attaching the source path.
        review = review.model_copy(update={"file": file})
    return review


def review_spec_text(
    spec_text: str,
    client: LLMClient,
    *,
    file: str | None = None,
) -> SpecReview:
    """Send ``spec_text`` through ``client`` and return a validated review."""
    _logger.debug(
        "Reviewing %s (%d chars)",
        file or "<inline>",
        len(spec_text),
    )
    messages = build_review_messages(spec_text)
    response = client.complete_json(messages)
    return parse_review_response(response, file=file)


def review_spec_file(path: Path, client: LLMClient) -> SpecReview:
    """Read ``path`` from disk and produce a validated :class:`SpecReview`."""
    # ``Path.name`` keeps the review portable across machines, where the
    # absolute path differs but the spec filename does not.
    return review_spec_text(
        path.read_text(encoding="utf-8"),
        client,
        file=path.name,
    )
