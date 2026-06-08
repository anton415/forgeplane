# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the rich-powered CLI rendering helpers."""

import io
import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from forgeplane import cli
from forgeplane.cli import (
    app,
    build_readiness_table,
    print_readiness_table,
    readiness_color,
    score_color,
)
from forgeplane.specs.schemas import ScanResult


def _render(table_or_text: object) -> str:
    # Render to a string buffer with ANSI styling disabled so assertions can
    # match the readiness keywords without escape-code noise.
    buffer = io.StringIO()
    console = Console(file=buffer, width=120, color_system=None)
    console.print(table_or_text)
    return buffer.getvalue()


def test_score_color_thresholds() -> None:
    # Boundary values match the colour legend documented in the README.
    assert score_color(100) == "green"
    assert score_color(80) == "green"
    assert score_color(79) == "yellow"
    assert score_color(60) == "yellow"
    assert score_color(59) == "red"
    assert score_color(0) == "red"


def test_readiness_color_covers_every_label() -> None:
    # Every readiness enum value must resolve to a colour so the table cell
    # never falls back to the terminal default.
    assert readiness_color("ready") == "green"
    assert readiness_color("partial") == "yellow"
    assert readiness_color("not_ready") == "red"


def test_build_readiness_table_lists_every_result() -> None:
    results = [
        ScanResult(
            file="good.md",
            score=100,
            missing=[],
            weak=[],
            todos_found=[],
            readiness="ready",
        ),
        ScanResult(
            file="weak.md",
            score=20,
            missing=["Context", "Risks", "Open Questions"],
            weak=["Acceptance Criteria"],
            todos_found=["Acceptance Criteria"],
            readiness="not_ready",
        ),
    ]
    rendered = _render(build_readiness_table(results))
    # Both rows render with their file name, score, and readiness label, and
    # the empty-missing cell collapses to the textual "none" placeholder.
    assert "good.md" in rendered
    assert "100" in rendered
    assert "ready" in rendered
    assert "none" in rendered
    assert "weak.md" in rendered
    assert "Context" in rendered
    assert "not_ready" in rendered


def test_print_readiness_table_skips_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buffer, width=120, color_system=None)
    )
    print_readiness_table([])
    # No rows means no output; the CLI should not even draw the table header.
    assert buffer.getvalue() == ""


def test_scan_json_output_includes_results(tmp_path: Path) -> None:
    # Persist a tiny spec and confirm the JSON payload now carries readiness
    # results, which downstream CI integrations can consume.
    (tmp_path / "spec.md").write_text(
        "## Goal\nTODO write the goal\n## Acceptance Criteria\nTODO\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["files_count"] == 1
    assert len(payload["results"]) == 1
    record = payload["results"][0]
    assert record["file"] == "spec.md"
    # TODO markers in two sections must be flagged in the serialised result.
    assert "Acceptance Criteria" in record["todos_found"]
    assert record["readiness"] in {"not_ready", "partial"}


def test_scan_text_output_renders_readiness_table(tmp_path: Path) -> None:
    (tmp_path / "spec.md").write_text(
        "## Goal\n"
        + ("Provide a reusable module for downstream services. " * 2)
        + "\n## Context\n"
        + ("Reference implementation for the new pipeline. " * 2)
        + "\n## Acceptance Criteria\n"
        + ("- Item one.\n- Item two.\n- Item three.\n- Item four.\n")
        + "## Risks\n"
        + ("Timezone-sensitive ordering may diverge across regions. " * 2)
        + "\n## Open Questions\n"
        + ("Are quotas needed in the first release? " * 2)
        + "\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path)])
    assert result.exit_code == 0, result.stderr
    # The readiness table title and the "ready" label both appear in the
    # human-facing output. ANSI codes are stripped by typer's CliRunner.
    assert "Spec readiness" in result.stdout
    assert "ready" in result.stdout
    assert "spec.md" in result.stdout
