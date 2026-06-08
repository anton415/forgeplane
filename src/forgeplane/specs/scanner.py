# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Typed scanner for API documentation directories."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict

from forgeplane.core.config import get_logger
from forgeplane.specs.files import FileEntry, collect_files
from forgeplane.specs.schemas import Readiness, ScanResult

# Module-level logger so scan steps surface under --verbose without each
# function re-deriving the namespace.
_logger = get_logger(__name__)

# Spec sections we expect every Forgeplane-managed Markdown spec to contain.
EXPECTED_SPEC_SECTIONS: Final[tuple[str, ...]] = (
    "Goal",
    "Context",
    "Acceptance Criteria",
    "Risks",
    "Open Questions",
)

# Match a level-2 ATX heading per CommonMark §4.2 and capture the heading text.
# Pattern breakdown:
#   ^ {0,3}             up to three spaces of indent (four spaces becomes a code
#                       block, so deeper indents must not match);
#   ##                  the opening level-2 marker;
#   [ \t]+              at least one space or tab between the marker and content
#                       (``##Goal`` is not a heading per spec);
#   (.+?)               the heading text, captured non-greedily so the optional
#                       closing run can claim its trailing hashes;
#   (?:[ \t]+#+[ \t]*)? optional closing run of ``#`` characters that must be
#                       preceded by whitespace and may be followed by trailing
#                       spaces/tabs only (so ``## Goal ##`` resolves to ``Goal``).
_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^ {0,3}##[ \t]+(.+?)(?:[ \t]+#+[ \t]*)?$"
)

# Match the opening of a fenced code block; the captured run drives close
# detection so a ``~~~~`` open is not closed by a shorter ``~~~`` run.
_FENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*(`{3,}|~{3,})")

# Each expected section contributes the same share to the readiness score.
# Five sections at twenty points each cap the score at the 0..100 range
# enforced by ScanResult.
SECTION_WEIGHT: Final[int] = 20

# Minimum body length, in characters, for a section to count as "strong".
# Acceptance Criteria carries a richer expectation (checklist or list of
# behaviours), so its bar is higher than the rest of the sections.
SECTION_MIN_LENGTHS: Final[dict[str, int]] = {
    "Goal": 40,
    "Context": 40,
    "Acceptance Criteria": 80,
    "Risks": 40,
    "Open Questions": 40,
}

# Words that flag unfinished content inside a section body. Matched as whole
# words to avoid false positives on identifiers like ``todos_found``.
_TODO_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(?:TODO|FIXME|TBD)\b")

# Score thresholds that map a numeric score onto the readiness enum exposed
# by ScanResult. Keeping the values here (and not in the schema) avoids
# circular imports when CLI helpers want to colour-code a raw score.
READY_THRESHOLD: Final[int] = 80
PARTIAL_THRESHOLD: Final[int] = 60


class ScanResultRecord(TypedDict):
    """Serialised ScanResult kept compatible with the Pydantic schema."""

    # Mirrors ``ScanResult.model_dump`` so JSON/YAML emitters can round-trip
    # the per-file readiness results without an extra conversion step.
    file: str
    score: int
    missing: list[str]
    weak: list[str]
    todos_found: list[str]
    readiness: Readiness


class ScanReport(TypedDict):
    """Report structure returned by scan_docs."""

    # Keep this shape JSON/YAML-friendly because CLI output serializes it
    # directly without additional model conversion.
    path: str
    files_count: int
    total_size_bytes: int
    extensions: dict[str, int]
    files: list[str]
    # Per-file readiness results for every discovered ``.md`` spec. Empty when
    # the scan target contains no Markdown files.
    results: list[ScanResultRecord]


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """Aggregated scan metadata before serialization."""

    # The summary keeps Path and FileEntry objects while scanning is still in
    # Python space; to_report converts them to plain serializable values.
    path: Path
    files: list[FileEntry]
    total_size_bytes: int
    extensions: dict[str, int]

    def to_report(self, results: list[ScanResult] | None = None) -> ScanReport:
        # Spell out each field so mypy can type-check the TypedDict literal
        # against the Pydantic model rather than splatting an opaque dict.
        result_records: list[ScanResultRecord] = [
            ScanResultRecord(
                file=result.file,
                score=result.score,
                missing=result.missing,
                weak=result.weak,
                todos_found=result.todos_found,
                readiness=result.readiness,
            )
            for result in (results or [])
        ]
        return {
            "path": str(self.path),
            "files_count": len(self.files),
            "total_size_bytes": self.total_size_bytes,
            "extensions": self.extensions,
            "files": [file.relative_path for file in self.files],
            "results": result_records,
        }


def extension_label(extension: str | None) -> str:
    # Files without suffixes are grouped under a stable report key.
    if extension is None:
        return "no_extension"
    return extension


def build_scan_summary(path: Path, files: list[FileEntry] | None = None) -> ScanSummary:
    # Passing files is useful for tests or future scanners that already have
    # collected FileEntry objects; otherwise the directory is scanned here.
    if files is None:
        _logger.debug("Collecting files under %s", path)
        file_entries = collect_files(path)
    else:
        file_entries = files
    extensions: dict[str, int] = {}
    total_size_bytes = 0

    # Aggregate only data needed by the public report and leave per-file details
    # in the FileEntry list.
    for file in file_entries:
        label = extension_label(file.extension)
        extensions[label] = extensions.get(label, 0) + 1
        total_size_bytes += file.size_bytes
        _logger.debug(
            "Indexed %s (%s, %d bytes)", file.relative_path, label, file.size_bytes
        )

    _logger.debug(
        "Aggregated %d file(s) into scan summary for %s", len(file_entries), path
    )
    return ScanSummary(
        path=path,
        files=file_entries,
        total_size_bytes=total_size_bytes,
        extensions=extensions,
    )


def scan_docs(path: Path) -> ScanReport:
    # Score every discovered Markdown spec so the public scanner entry point
    # honours the ``ScanReport.results`` contract for direct API callers.
    # Without this, only the CLI (which scores results itself) would populate
    # the readiness data; library users would always see ``results == []``.
    summary = build_scan_summary(path)
    results = [score_spec_file(entry) for entry in markdown_entries(summary)]
    return summary.to_report(results=results)


def parse_sections(text: str) -> dict[str, str | None]:
    """Return body text for each expected spec section, or None when missing."""
    bodies: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []
    # Track fenced code blocks so a ``## Context`` line inside ``` ... ``` is not
    # mistaken for a real section boundary.
    fence_marker: str | None = None

    for line in text.splitlines():
        if fence_marker is None:
            fence_open = _FENCE_PATTERN.match(line)
            if fence_open is not None:
                fence_marker = fence_open.group(1)
                if current_name is not None:
                    current_lines.append(line)
                continue

            heading = _HEADING_PATTERN.match(line)
            if heading is not None:
                if current_name is not None:
                    bodies[current_name] = "\n".join(current_lines).strip()
                current_name = heading.group(1).strip()
                current_lines = []
                continue

            if current_name is not None:
                current_lines.append(line)
            continue

        # Inside a fence: keep the content verbatim and look for the matching close.
        if current_name is not None:
            current_lines.append(line)
        fence_close = _FENCE_PATTERN.match(line)
        if (
            fence_close is not None
            and fence_close.group(1)[0] == fence_marker[0]
            and len(fence_close.group(1)) >= len(fence_marker)
        ):
            fence_marker = None

    if current_name is not None:
        bodies[current_name] = "\n".join(current_lines).strip()

    # Empty bodies collapse to None so downstream readiness checks treat them
    # as missing.
    return {
        section: (bodies.get(section) or None) for section in EXPECTED_SPEC_SECTIONS
    }


def parse_spec_file(path: Path) -> dict[str, str | None]:
    """Read a Markdown spec from disk and parse its expected sections."""
    return parse_sections(path.read_text(encoding="utf-8"))


def _section_min_length(name: str) -> int:
    # Unknown sections fall back to the same baseline as Goal/Context so the
    # scoring stays deterministic if EXPECTED_SPEC_SECTIONS gains new entries
    # before SECTION_MIN_LENGTHS is updated.
    return SECTION_MIN_LENGTHS.get(name, 40)


@dataclass(frozen=True, slots=True)
class SectionScoring:
    """Per-spec score breakdown produced by :func:`score_sections`."""

    # Splitting the breakdown into a dataclass keeps :func:`score_spec_file`
    # readable and avoids a fragile multi-value tuple at the call sites.
    score: int
    missing: list[str]
    weak: list[str]
    todos: list[str]


def score_sections(sections: dict[str, str | None]) -> SectionScoring:
    """Score parsed sections against the readiness expectations."""
    score = 0
    missing: list[str] = []
    weak: list[str] = []
    todos: list[str] = []

    for name in EXPECTED_SPEC_SECTIONS:
        body = sections.get(name)
        if not body:
            # A missing section forfeits its full weight in the score.
            missing.append(name)
            continue
        has_todo = bool(_TODO_PATTERN.search(body))
        if has_todo:
            todos.append(name)
        too_short = len(body) < _section_min_length(name)
        if has_todo or too_short:
            # Half credit for present-but-weak sections so partial work still
            # moves the score above zero.
            weak.append(name)
            score += SECTION_WEIGHT // 2
        else:
            score += SECTION_WEIGHT

    return SectionScoring(score=score, missing=missing, weak=weak, todos=todos)


def classify_readiness(score: int) -> Readiness:
    """Map a numeric score to the discrete readiness enum."""
    if score >= READY_THRESHOLD:
        return "ready"
    if score >= PARTIAL_THRESHOLD:
        return "partial"
    return "not_ready"


def score_spec_file(file: FileEntry) -> ScanResult:
    """Parse a single Markdown spec and turn it into a ScanResult."""
    _logger.debug("Scoring spec %s", file.relative_path)
    sections = parse_spec_file(file.path)
    scoring = score_sections(sections)
    return ScanResult(
        file=file.relative_path,
        score=scoring.score,
        missing=scoring.missing,
        weak=scoring.weak,
        todos_found=scoring.todos,
        readiness=classify_readiness(scoring.score),
    )


def markdown_entries(summary: ScanSummary) -> list[FileEntry]:
    """Return the subset of summary files that the scorer can handle."""
    # Only Markdown specs are parseable today; future scanners (OpenAPI, etc.)
    # will extend this filter with additional readers.
    return [file for file in summary.files if file.extension == ".md"]
