# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Schemas for API specification readiness scan and review results."""

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

# Readiness is intentionally limited to stable report states that downstream
# tools can branch on without parsing free-form text.
Readiness: TypeAlias = Literal["ready", "partial", "not_ready"]


class SectionCheck(BaseModel):
    """Readiness check for one expected documentation section."""

    name: str
    present: bool
    # Section length is measured as a non-negative character or token count,
    # depending on the scanner that produces the check.
    length: int = Field(ge=0)
    has_todo: bool


class ScanResult(BaseModel):
    """Structured scan result for one API documentation file."""

    file: str
    # Keep the score normalized so reports can be compared across files.
    score: int = Field(ge=0, le=100)
    missing: list[str]
    weak: list[str]
    todos_found: list[str]
    readiness: Readiness


class SpecReview(BaseModel):
    """Structured review of a single Markdown specification.

    Produced by the LLM-backed reviewer and validated through Pydantic so the
    raw model output never reaches downstream callers in a free-form shape.
    Each list entry is a short, human-readable sentence describing exactly one
    finding, recommendation, or risk — keeping the items atomic lets CI jobs
    surface them individually (for example, as PR comment threads).
    """

    # Reject keys the LLM might invent ("notes", "summary", ...) so any drift
    # from the schema fails fast during validation instead of silently
    # propagating to the report. ``str_strip_whitespace`` trims user-visible
    # noise that some models add around bullet points.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Optional pointer back to the reviewed file. Reviews can be produced
    # in-memory from a Markdown string, in which case the source path is not
    # known; the CLI populates it when reading from disk.
    file: str | None = None
    # Readiness score kept on the same 0..100 scale as ScanResult so a CI job
    # can apply a single threshold across both signals.
    score: int = Field(ge=0, le=100)
    # Ambiguous statements that should be clarified before the spec is shipped.
    ambiguities: list[str] = Field(default_factory=list)
    # Acceptance criteria the reviewer believes are missing from the spec.
    missing_acceptance_criteria: list[str] = Field(default_factory=list)
    # Concrete suggestions for improving the spec, ordered from most to least
    # impactful when the model can rank them.
    recommendations: list[str] = Field(default_factory=list)
    # Risks the reviewer surfaces beyond what the author already documented in
    # the ``## Risks`` section, if any.
    risks: list[str] = Field(default_factory=list)
