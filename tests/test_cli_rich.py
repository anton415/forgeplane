# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the rich-powered CLI rendering helpers."""

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from forgeplane import cli
from forgeplane.cli import (
    _build_report_filename,
    app,
    build_readiness_table,
    print_readiness_table,
    readiness_color,
    save_report,
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


def test_scan_text_output_handles_empty_directory(tmp_path: Path) -> None:
    # Covers the ``if report["files"]:`` false branch in print_text_report:
    # an empty directory must still produce a summary table without listing
    # any files or rendering a readiness table.
    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path)])
    assert result.exit_code == 0, result.stderr
    assert "Forgeplane scan report" in result.stdout
    # No file list, no readiness table.
    assert "Files:" not in result.stdout
    assert "Spec readiness" not in result.stdout


def test_scan_skips_scoring_when_no_markdown_files(tmp_path: Path) -> None:
    # Covers the ``if md_entries:`` false branch in :func:`scan`: a directory
    # without Markdown files must still produce a valid report with an empty
    # ``results`` array and no readiness table in the text output.
    (tmp_path / "data.yaml").write_text("title: x\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["files_count"] == 1
    assert payload["results"] == []


def test_scan_yaml_output_carries_results(tmp_path: Path) -> None:
    # Covers the ``--format yaml`` branch in :func:`scan` and confirms the
    # serialised payload round-trips through PyYAML with the readiness data.
    (tmp_path / "spec.md").write_text(
        "## Goal\nTODO\n## Acceptance Criteria\nTODO\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path), "--format", "yaml"])
    assert result.exit_code == 0, result.stderr
    payload = yaml.safe_load(result.stdout)
    assert payload["files_count"] == 1
    assert len(payload["results"]) == 1
    assert payload["results"][0]["file"] == "spec.md"


def test_build_report_filename_uses_format_and_timestamp() -> None:
    # The filename must be deterministic for a given moment so CI artifacts
    # can be looked up without scanning the directory.
    moment = datetime(2026, 6, 8, 12, 34, 56, tzinfo=UTC)
    assert _build_report_filename("json", now=moment) == "scan_20260608T123456Z.json"
    assert _build_report_filename("yaml", now=moment) == "scan_20260608T123456Z.yaml"


def test_save_report_writes_payload_to_directory(tmp_path: Path) -> None:
    # The helper creates the directory on demand and returns the written path
    # so the CLI can log it for the user.
    report: cli.ScanReport = {
        "path": "/tmp/docs",
        "files_count": 0,
        "total_size_bytes": 0,
        "extensions": {},
        "files": [],
        "results": [],
    }
    output_dir = tmp_path / "reports"
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    written = save_report(report, output_dir, "json", now=moment)
    assert written == output_dir / "scan_20260102T030405Z.json"
    # ``json.loads(saved_payload) == expected_structure`` — the exact contract
    # called out in the issue for CI integrations.
    assert json.loads(written.read_text(encoding="utf-8")) == report


def test_save_report_rejects_text_format(tmp_path: Path) -> None:
    # Saving the rich text rendering would embed ANSI control codes; surface
    # an explicit error instead of writing an unusable artifact.
    report: cli.ScanReport = {
        "path": "/tmp/docs",
        "files_count": 0,
        "total_size_bytes": 0,
        "extensions": {},
        "files": [],
        "results": [],
    }
    with pytest.raises(ValueError, match="json and yaml"):
        save_report(report, tmp_path, "text")


def test_scan_saves_json_report_to_output_dir(tmp_path: Path) -> None:
    # End-to-end: --format json + --output-dir writes a scan_*.json file the
    # next CI step can pick up without parsing stdout.
    (tmp_path / "spec.md").write_text(
        "## Goal\nShip a typed scanner.\n## Acceptance Criteria\nWritten.\n",
        encoding="utf-8",
    )
    reports_dir = tmp_path / "reports"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "scan",
            str(tmp_path),
            "--format",
            "json",
            "--output-dir",
            str(reports_dir),
        ],
    )
    assert result.exit_code == 0, result.stderr
    saved_files = sorted(reports_dir.glob("scan_*.json"))
    assert len(saved_files) == 1
    saved_text = saved_files[0].read_text(encoding="utf-8")
    # Byte-identity (not just structural equality) is the stronger contract:
    # a CI job can ``diff`` stdout against the archived file and expect a
    # clean match. ``json.dumps`` omits the trailing newline, so the payload
    # ends with exactly one ``\n`` in both sources.
    assert saved_text == result.stdout
    assert saved_text.endswith("\n")
    saved_payload = json.loads(saved_text)
    assert saved_payload["files_count"] == 1
    assert saved_payload["results"][0]["file"] == "spec.md"


def test_scan_saves_yaml_report_to_output_dir(tmp_path: Path) -> None:
    # YAML branch mirrors the JSON contract; the file extension follows the
    # chosen --format.
    (tmp_path / "spec.md").write_text(
        "## Goal\nShip a typed scanner.\n## Acceptance Criteria\nWritten.\n",
        encoding="utf-8",
    )
    reports_dir = tmp_path / "reports"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "scan",
            str(tmp_path),
            "--format",
            "yaml",
            "--output-dir",
            str(reports_dir),
        ],
    )
    assert result.exit_code == 0, result.stderr
    saved_files = sorted(reports_dir.glob("scan_*.yaml"))
    assert len(saved_files) == 1
    saved_text = saved_files[0].read_text(encoding="utf-8")
    # Same byte-identity contract as the JSON branch: ``yaml.safe_dump``
    # already terminates with one ``\n`` and stdout must mirror that.
    assert saved_text == result.stdout
    assert saved_text.endswith("\n")
    saved_payload = yaml.safe_load(saved_text)
    assert saved_payload["files_count"] == 1
    assert saved_payload["results"][0]["file"] == "spec.md"


def test_scan_rejects_output_dir_with_text_format(tmp_path: Path) -> None:
    # Combining --output-dir with the default text format is a usage error;
    # the CLI should fail fast before scanning anything.
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scan", str(tmp_path), "--output-dir", str(tmp_path / "reports")],
    )
    assert result.exit_code != 0
    # No partial artifact should be created when the invocation is rejected.
    assert not (tmp_path / "reports").exists()


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
