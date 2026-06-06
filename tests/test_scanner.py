"""Tests for the Markdown spec section parser."""

from typing import Callable, Dict, Optional

from forgeplane.specs.scanner import EXPECTED_SPEC_SECTIONS

# Callable signature exposed by the ``parse`` fixture in conftest.
SectionParser = Callable[[str], Dict[str, Optional[str]]]


# Threshold used by the readiness check for the Acceptance Criteria section.
ACCEPTANCE_CRITERIA_MIN_LENGTH = 80


def test_complete_spec_has_every_section(good_spec_sections: Dict[str, Optional[str]]) -> None:
    # A fully populated spec produces a non-empty body for every expected section.
    for section in EXPECTED_SPEC_SECTIONS:
        assert good_spec_sections[section], f"{section} should be populated"


def test_missing_sections_collapse_to_none(weak_spec_sections: Dict[str, Optional[str]]) -> None:
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


def test_todos_found_in_section_body(weak_spec_sections: Dict[str, Optional[str]]) -> None:
    # Detecting TODO markers is the first signal of weak content.
    acceptance = weak_spec_sections["Acceptance Criteria"]
    assert acceptance is not None
    assert "TODO" in acceptance


def test_short_acceptance_criteria_is_flagged(
    weak_spec_sections: Dict[str, Optional[str]],
    good_spec_sections: Dict[str, Optional[str]],
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
        "## Context\n"
        "context body\n"
        "    ## Goal\n"
        "indented body\n"
        "##Goal\n"
        "no-space body\n"
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
