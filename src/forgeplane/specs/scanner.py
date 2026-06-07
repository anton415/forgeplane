# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Typed scanner for API documentation directories."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict

from forgeplane.specs.files import FileEntry, collect_files

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


class ScanReport(TypedDict):
    """Report structure returned by scan_docs."""

    # Keep this shape JSON/YAML-friendly because CLI output serializes it
    # directly without additional model conversion.
    path: str
    files_count: int
    total_size_bytes: int
    extensions: dict[str, int]
    files: list[str]


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """Aggregated scan metadata before serialization."""

    # The summary keeps Path and FileEntry objects while scanning is still in
    # Python space; to_report converts them to plain serializable values.
    path: Path
    files: list[FileEntry]
    total_size_bytes: int
    extensions: dict[str, int]

    def to_report(self) -> ScanReport:
        return {
            "path": str(self.path),
            "files_count": len(self.files),
            "total_size_bytes": self.total_size_bytes,
            "extensions": self.extensions,
            "files": [file.relative_path for file in self.files],
        }


def extension_label(extension: str | None) -> str:
    # Files without suffixes are grouped under a stable report key.
    if extension is None:
        return "no_extension"
    return extension


def build_scan_summary(path: Path, files: list[FileEntry] | None = None) -> ScanSummary:
    # Passing files is useful for tests or future scanners that already have
    # collected FileEntry objects; otherwise the directory is scanned here.
    file_entries = collect_files(path) if files is None else files
    extensions: dict[str, int] = {}
    total_size_bytes = 0

    # Aggregate only data needed by the public report and leave per-file details
    # in the FileEntry list.
    for file in file_entries:
        label = extension_label(file.extension)
        extensions[label] = extensions.get(label, 0) + 1
        total_size_bytes += file.size_bytes

    return ScanSummary(
        path=path,
        files=file_entries,
        total_size_bytes=total_size_bytes,
        extensions=extensions,
    )


def scan_docs(path: Path) -> ScanReport:
    return build_scan_summary(path).to_report()


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
