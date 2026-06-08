# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Markdown spec section parser."""

from collections.abc import Callable
from pathlib import Path

from forgeplane.specs.files import FileEntry
from forgeplane.specs.scanner import (
    EXPECTED_SPEC_SECTIONS,
    build_scan_summary,
    extension_label,
)

# Callable signature exposed by the ``parse`` fixture in conftest.
SectionParser = Callable[[str], dict[str, str | None]]


# Threshold used by the readiness check for the Acceptance Criteria section.
ACCEPTANCE_CRITERIA_MIN_LENGTH = 80


def test_complete_spec_has_every_section(
    good_spec_sections: dict[str, str | None],
) -> None:
    # A fully populated spec produces a non-empty body for every expected section.
    for section in EXPECTED_SPEC_SECTIONS:
        assert good_spec_sections[section], f"{section} should be populated"


def test_missing_sections_collapse_to_none(
    weak_spec_sections: dict[str, str | None],
) -> None:
    # Sections omitted from the file must surface as None, not empty strings.
    assert weak_spec_sections["Context"] is None
    assert weak_spec_sections["Risks"] is None
    # An H2 heading with no body still counts as missing because no content follows it.
    assert weak_spec_sections["Open Questions"] is None


def test_empty_file_returns_all_none(parse: SectionParser) -> None:
    # The parser must return the full schema even for empty input.
    parsed = parse("")
    assert set(parsed) == set(EXPECTED_SPEC_SECTIONS)
    assert all(value is None for value in parsed.values())


def test_todos_found_in_section_body(weak_spec_sections: dict[str, str | None]) -> None:
    # Detecting TODO markers is the first signal of weak content.
    acceptance = weak_spec_sections["Acceptance Criteria"]
    assert acceptance is not None
    assert "TODO" in acceptance


def test_short_acceptance_criteria_is_flagged(
    weak_spec_sections: dict[str, str | None],
    good_spec_sections: dict[str, str | None],
) -> None:
    # The weak spec ships with a one-liner AC, the good spec with a full checklist.
    weak_ac = weak_spec_sections["Acceptance Criteria"]
    good_ac = good_spec_sections["Acceptance Criteria"]
    assert weak_ac is not None and len(weak_ac) < ACCEPTANCE_CRITERIA_MIN_LENGTH
    assert good_ac is not None and len(good_ac) >= ACCEPTANCE_CRITERIA_MIN_LENGTH


def test_atx_heading_variants_resolve_to_expected_names(parse: SectionParser) -> None:
    # CommonMark allows up to three spaces of indent and an optional closing
    # run of ``#`` characters; both must still map to the canonical section key.
    spec = (
        "## Goal ##\n"
        "goal body\n"
        "   ## Context\n"
        "context body\n"
        "  ## Acceptance Criteria  ##\n"
        "ac body\n"
        "## Risks #\n"
        "risk body\n"
        "## Open Questions\n"
        "oq body\n"
    )
    parsed = parse(spec)
    assert parsed["Goal"] == "goal body"
    assert parsed["Context"] == "context body"
    assert parsed["Acceptance Criteria"] == "ac body"
    assert parsed["Risks"] == "risk body"
    assert parsed["Open Questions"] == "oq body"


def test_invalid_atx_forms_are_not_treated_as_headings(parse: SectionParser) -> None:
    # Four or more spaces of indent become a code block; ``##Goal`` lacks the
    # required separator. Neither should populate the Goal section.
    spec = (
        "## Context\ncontext body\n    ## Goal\nindented body\n##Goal\nno-space body\n"
    )
    parsed = parse(spec)
    assert parsed["Goal"] is None
    assert parsed["Context"] is not None
    assert "indented body" in parsed["Context"]
    assert "no-space body" in parsed["Context"]


def test_fenced_heading_does_not_split_section(parse: SectionParser) -> None:
    # Regression: a ``## Context`` line inside a fenced block must stay under Goal.
    spec = (
        "## Goal\n"
        "Walk through the request lifecycle:\n"
        "\n"
        "```markdown\n"
        "## Context\n"
        "not a real heading\n"
        "```\n"
        "\n"
        "Goal continues.\n"
        "\n"
        "## Context\n"
        "Real context.\n"
    )
    parsed = parse(spec)
    goal = parsed["Goal"]
    assert goal is not None
    assert "Goal continues." in goal
    assert "## Context\nnot a real heading" in goal
    assert parsed["Context"] == "Real context."


def test_build_scan_summary_accepts_prebuilt_file_list(tmp_path: Path) -> None:
    # Exercises the explicit-files branch of build_scan_summary, used by future
    # scanners that already have FileEntry objects in hand. Also covers the
    # extension-less aggregation path, where extension_label collapses to
    # ``no_extension`` in the report.
    entries = [
        FileEntry(
            path=tmp_path / "intro.md",
            relative_path="intro.md",
            extension=".md",
            size_bytes=10,
        ),
        FileEntry(
            path=tmp_path / "README",
            relative_path="README",
            extension=None,
            size_bytes=5,
        ),
    ]
    summary = build_scan_summary(tmp_path, files=entries)
    report = summary.to_report()
    assert report["files_count"] == 2
    assert report["total_size_bytes"] == 15
    assert report["extensions"] == {".md": 1, "no_extension": 1}
    assert report["files"] == ["intro.md", "README"]


def test_extension_label_handles_missing_suffix() -> None:
    # Direct check on the labeling helper used by the aggregation loop.
    assert extension_label(None) == "no_extension"
    assert extension_label(".yaml") == ".yaml"
