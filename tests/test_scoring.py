# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for spec readiness scoring and the per-file scoring helper."""

from pathlib import Path

from forgeplane.specs.files import FileEntry
from forgeplane.specs.scanner import (
    PARTIAL_THRESHOLD,
    READY_THRESHOLD,
    SECTION_WEIGHT,
    build_scan_summary,
    classify_readiness,
    markdown_entries,
    scan_docs,
    score_sections,
    score_spec_file,
)


def test_full_spec_scores_max(good_spec_sections: dict[str, str | None]) -> None:
    scoring = score_sections(good_spec_sections)
    # A fully populated spec collects the full weight from every section.
    assert scoring.score == SECTION_WEIGHT * 5
    assert scoring.missing == []
    assert scoring.weak == []
    assert scoring.todos == []


def test_weak_spec_flags_missing_and_todos(
    weak_spec_sections: dict[str, str | None],
) -> None:
    scoring = score_sections(weak_spec_sections)
    # The weak example omits Context/Risks/Open Questions and ships a TODO
    # in Acceptance Criteria, with a one-line Goal below the strong-length bar.
    assert scoring.missing == ["Context", "Risks", "Open Questions"]
    assert "Acceptance Criteria" in scoring.weak
    assert "Acceptance Criteria" in scoring.todos
    assert "Goal" in scoring.weak
    # Two weak sections × half weight = full SECTION_WEIGHT total.
    assert scoring.score == SECTION_WEIGHT


def test_classify_readiness_thresholds() -> None:
    # Exact threshold values must land in the higher bucket so the CLI legend
    # in the README stays accurate.
    assert classify_readiness(READY_THRESHOLD) == "ready"
    assert classify_readiness(READY_THRESHOLD - 1) == "partial"
    assert classify_readiness(PARTIAL_THRESHOLD) == "partial"
    assert classify_readiness(PARTIAL_THRESHOLD - 1) == "not_ready"
    assert classify_readiness(0) == "not_ready"


def test_score_spec_file_uses_relative_path(tmp_path: Path) -> None:
    # Persist a minimal spec on disk and exercise the FileEntry-level helper.
    spec_path = tmp_path / "nested" / "spec.md"
    spec_path.parent.mkdir()
    spec_path.write_text(
        "## Goal\n"
        + ("Provide a reusable Todo module for downstream services. " * 2)
        + "\n## Context\n"
        + ("Reference implementation for the new task pipeline. " * 2)
        + "\n## Acceptance Criteria\n"
        + (
            "- Create todos with title and due date.\n"
            "- List todos ordered by creation time.\n"
            "- Complete todos with timestamp tracking.\n"
        )
        + "## Risks\n"
        + ("Timezone-sensitive ordering may diverge across regions. " * 2)
        + "\n## Open Questions\n"
        + ("Are quotas needed in the first release? " * 2)
        + "\n",
        encoding="utf-8",
    )
    entry = FileEntry(
        path=spec_path,
        relative_path="nested/spec.md",
        extension=".md",
        size_bytes=spec_path.stat().st_size,
    )
    result = score_spec_file(entry)
    # ScanResult preserves the relative path so reports surface the directory
    # layout rather than absolute filesystem locations.
    assert result.file == "nested/spec.md"
    assert result.readiness == "ready"
    assert result.score == SECTION_WEIGHT * 5
    assert result.missing == []


def test_markdown_entries_filters_non_markdown(tmp_path: Path) -> None:
    (tmp_path / "intro.md").write_text("# intro", encoding="utf-8")
    (tmp_path / "data.yaml").write_text("title: x\n", encoding="utf-8")
    summary = build_scan_summary(tmp_path)
    entries = markdown_entries(summary)
    # Only ``.md`` files reach the scorer; YAML and other formats need their
    # own scanners and are skipped here.
    assert [entry.relative_path for entry in entries] == ["intro.md"]


def test_to_report_serialises_results(
    good_spec_sections: dict[str, str | None],
) -> None:
    # The serialised report must include the ``results`` field even when the
    # caller passes no ScanResult objects, so JSON/YAML consumers can rely on
    # the key always being present.
    summary = build_scan_summary(Path("."), files=[])
    report = summary.to_report()
    assert report["results"] == []


def test_scan_docs_populates_results_for_markdown_specs(tmp_path: Path) -> None:
    # Regression guard: ``scan_docs`` is the library entry point used by
    # callers that bypass the CLI. It must score every discovered ``.md``
    # file so the ``ScanReport.results`` contract holds regardless of caller.
    (tmp_path / "weak.md").write_text(
        "## Goal\nTODO write the goal\n## Acceptance Criteria\nTODO\n",
        encoding="utf-8",
    )
    report = scan_docs(tmp_path)
    assert report["files_count"] == 1
    assert len(report["results"]) == 1
    record = report["results"][0]
    assert record["file"] == "weak.md"
    assert "Acceptance Criteria" in record["todos_found"]
    # ``not_ready`` is expected for a spec built from two TODO placeholders.
    assert record["readiness"] == "not_ready"
