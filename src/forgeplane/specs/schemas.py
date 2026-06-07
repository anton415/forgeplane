# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Schemas for API specification readiness scan results."""

from typing import Literal, TypeAlias

from pydantic import BaseModel, Field

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
