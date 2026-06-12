# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the pandas-backed eval reporter."""

import io
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from rich.console import Console

from forgeplane.evals.reporter import (
    DATAFRAME_COLUMNS,
    DEFAULT_PASS_THRESHOLD,
    EvalReport,
    EvalSummary,
    _neutralize_csv_cell,
    build_eval_table,
    build_review_dataframe,
    compute_summary,
    generate_eval_report,
    print_eval_table,
    save_eval_csv,
)
from forgeplane.specs.schemas import SpecReview


def _render(table_or_text: object) -> str:
    # Render to a string buffer with ANSI disabled so assertions can match
    # plain text without escape-code noise.
    buffer = io.StringIO()
    console = Console(file=buffer, width=160, color_system=None)
    console.print(table_or_text)
    return buffer.getvalue()


def _make_review(
    *,
    file: str | None = "spec.md",
    score: int = 80,
    ambiguities: list[str] | None = None,
    missing_acceptance_criteria: list[str] | None = None,
    recommendations: list[str] | None = None,
    risks: list[str] | None = None,
) -> SpecReview:
    # Factory keeps each test focused on the attribute under exercise rather
    # than re-spelling the SpecReview defaults.
    return SpecReview(
        file=file,
        score=score,
        ambiguities=ambiguities or [],
        missing_acceptance_criteria=missing_acceptance_criteria or [],
        recommendations=recommendations or [],
        risks=risks or [],
    )


# ---------------------------------------------------------------------------
# build_review_dataframe
# ---------------------------------------------------------------------------


def test_build_review_dataframe_pins_columns_for_empty_batch() -> None:
    # The contract is that the DataFrame always exposes the same columns so
    # downstream CSVs are interchangeable across runs.
    df = build_review_dataframe([])
    assert list(df.columns) == list(DATAFRAME_COLUMNS)
    assert len(df) == 0


def test_build_review_dataframe_captures_counts_per_row() -> None:
    reviews = [
        _make_review(
            file="a.md",
            score=90,
            ambiguities=["one"],
            recommendations=["x", "y"],
        ),
        _make_review(
            file="b.md",
            score=40,
            risks=["t"],
            missing_acceptance_criteria=["a", "b", "c"],
        ),
    ]
    df = build_review_dataframe(reviews)
    assert list(df.columns) == list(DATAFRAME_COLUMNS)
    assert df["file"].tolist() == ["a.md", "b.md"]
    assert df["score"].tolist() == [90, 40]
    assert df["ambiguities_count"].tolist() == [1, 0]
    assert df["missing_acceptance_criteria_count"].tolist() == [0, 3]
    assert df["recommendations_count"].tolist() == [2, 0]
    assert df["risks_count"].tolist() == [0, 1]


def test_build_review_dataframe_uses_placeholder_for_missing_file() -> None:
    # In-memory reviews lack a source path; the placeholder keeps the column
    # non-null without inventing a filename.
    df = build_review_dataframe([_make_review(file=None, score=50)])
    assert df["file"].tolist() == ["<inline>"]


def test_build_review_dataframe_consumes_generators_once() -> None:
    # Generators must not be exhausted twice; the reporter materialises the
    # iterable internally so the DataFrame ends up with the original rows.
    def gen() -> list[SpecReview]:
        return [_make_review(file=f"s{i}.md", score=70 + i) for i in range(3)]

    df = build_review_dataframe(iter(gen()))
    assert df["file"].tolist() == ["s0.md", "s1.md", "s2.md"]
    assert df["score"].tolist() == [70, 71, 72]


# ---------------------------------------------------------------------------
# compute_summary
# ---------------------------------------------------------------------------


def test_compute_summary_returns_zeroes_for_empty_dataframe() -> None:
    # An empty batch must produce explicit zeros, not pandas NaN, so JSON/CSV
    # consumers do not need to special-case missing values.
    summary = compute_summary(build_review_dataframe([]))
    assert summary.total == 0
    assert summary.mean_score == 0.0
    assert summary.pass_rate == 0.0
    assert summary.passing == 0
    assert summary.pass_threshold == DEFAULT_PASS_THRESHOLD


def test_compute_summary_computes_mean_and_pass_rate() -> None:
    # Two of three rows clear the default threshold (>= 80); the resulting
    # pass_rate must be 2/3 with the mean over all three rows.
    df = build_review_dataframe(
        [
            _make_review(score=90),
            _make_review(score=80),
            _make_review(score=40),
        ]
    )
    summary = compute_summary(df)
    assert summary.total == 3
    assert summary.passing == 2
    assert summary.pass_rate == 2 / 3
    assert summary.mean_score == 70.0
    assert summary.pass_threshold == DEFAULT_PASS_THRESHOLD


def test_compute_summary_honours_custom_threshold() -> None:
    df = build_review_dataframe(
        [_make_review(score=60), _make_review(score=70), _make_review(score=80)]
    )
    summary = compute_summary(df, pass_threshold=70)
    # 70 and 80 clear the bar; 60 does not.
    assert summary.passing == 2
    assert summary.pass_rate == 2 / 3
    assert summary.pass_threshold == 70


# ---------------------------------------------------------------------------
# save_eval_csv
# ---------------------------------------------------------------------------


def test_save_eval_csv_writes_timestamped_file(tmp_path: Path) -> None:
    reviews = [_make_review(file="spec.md", score=85, recommendations=["x"])]
    df = build_review_dataframe(reviews)
    fixed_now = datetime(2026, 6, 9, 12, 34, 56, tzinfo=UTC)
    target = save_eval_csv(df, tmp_path, now=fixed_now)
    # The filename is fully determined by the pinned timestamp so a CI job
    # can predict the artifact path without parsing the directory listing.
    assert target.name == "eval_20260609T123456Z.csv"
    assert target.parent == tmp_path
    # Round-trip the CSV to confirm the schema matches the DataFrame columns
    # and the data survives the persistence step intact.
    reloaded = pd.read_csv(target)
    assert list(reloaded.columns) == list(DATAFRAME_COLUMNS)
    assert reloaded["file"].tolist() == ["spec.md"]
    assert reloaded["score"].tolist() == [85]
    assert reloaded["recommendations_count"].tolist() == [1]


def test_save_eval_csv_creates_missing_output_dir(tmp_path: Path) -> None:
    # The helper must create the directory on demand so callers do not have
    # to mkdir before every save.
    nested = tmp_path / "nested" / "deep"
    df = build_review_dataframe([_make_review(score=70)])
    target = save_eval_csv(df, nested)
    assert target.parent == nested
    assert target.is_file()


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r", "\n"])
def test_neutralize_csv_cell_defuses_each_formula_prefix(prefix: str) -> None:
    # Every spreadsheet formula metacharacter (and the CR/LF/tab structure
    # controls) must gain the quote prefix so Excel/LibreOffice render the
    # cell as literal text instead of evaluating it (issue #79).
    assert _neutralize_csv_cell(f"{prefix}payload.md") == f"'{prefix}payload.md"


def test_neutralize_csv_cell_leaves_benign_values_unchanged() -> None:
    # Ordinary filenames and the inline placeholder must round-trip without
    # the quote prefix; metacharacters are only dangerous in first position.
    assert _neutralize_csv_cell("spec.md") == "spec.md"
    assert _neutralize_csv_cell("<inline>") == "<inline>"
    assert _neutralize_csv_cell("a=b.md") == "a=b.md"
    assert _neutralize_csv_cell("") == ""


def test_save_eval_csv_neutralizes_formula_cells(tmp_path: Path) -> None:
    # A model-influenced filename carrying a spreadsheet formula must reach
    # the artifact defused, while the in-memory DataFrame keeps the raw value
    # for library callers doing their own analysis.
    payload = '=HYPERLINK("http://evil.example","open")'
    df = build_review_dataframe([_make_review(file=payload, score=10)])
    target = save_eval_csv(df, tmp_path)
    reloaded = pd.read_csv(target)
    assert reloaded["file"].tolist() == [f"'{payload}"]
    # The neutralization happens on a copy at write time only.
    assert df["file"].tolist() == [payload]


# ---------------------------------------------------------------------------
# build_eval_table / print_eval_table
# ---------------------------------------------------------------------------


def test_build_eval_table_renders_rows_and_summary() -> None:
    reviews = [
        _make_review(file="a.md", score=90, recommendations=["x"]),
        _make_review(file="b.md", score=40, risks=["r"]),
    ]
    df = build_review_dataframe(reviews)
    summary = compute_summary(df)
    rendered = _render(build_eval_table(df, summary))
    # File-level rows surface alongside the aggregate metrics so a developer
    # can compare individual scores against the mean in one view.
    assert "a.md" in rendered
    assert "b.md" in rendered
    assert "Mean score" in rendered
    assert "65.00" in rendered
    assert "Pass rate" in rendered
    assert "50.00%" in rendered
    assert "Total reviews" in rendered


def test_build_eval_table_renders_markup_filenames_literally() -> None:
    # A filename shaped like rich markup (issue #79) must surface as literal
    # characters: interpreted markup would swallow the bracket tags and could
    # emit a terminal hyperlink pointing wherever the model chose.
    spoofed = "[link=https://evil.example]spec.md[/link]"
    df = build_review_dataframe([_make_review(file=spoofed, score=90)])
    rendered = _render(build_eval_table(df, compute_summary(df)))
    assert spoofed in rendered


def test_print_eval_table_routes_through_provided_console() -> None:
    df = build_review_dataframe([_make_review(score=50)])
    summary = compute_summary(df)
    buffer = io.StringIO()
    target = Console(file=buffer, width=160, color_system=None)
    print_eval_table(df, summary, target=target)
    assert "Mean score" in buffer.getvalue()


# ---------------------------------------------------------------------------
# generate_eval_report
# ---------------------------------------------------------------------------


def test_generate_eval_report_bundles_results_in_memory() -> None:
    # No output_dir means the CSV path is None and only the in-memory shape
    # is populated; this is the path notebooks and tests use.
    report = generate_eval_report([_make_review(score=85)])
    assert isinstance(report, EvalReport)
    assert isinstance(report.summary, EvalSummary)
    assert report.csv_path is None
    assert report.summary.total == 1
    assert report.summary.passing == 1


def test_generate_eval_report_persists_csv_when_output_dir_set(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    report = generate_eval_report(
        [_make_review(file="x.md", score=95)],
        output_dir=tmp_path,
        now=fixed_now,
    )
    assert report.csv_path is not None
    assert report.csv_path.name == "eval_20260102T030405Z.csv"
    assert report.csv_path.is_file()


def test_generate_eval_report_propagates_pass_threshold(tmp_path: Path) -> None:
    # A custom threshold must reach compute_summary so the summary mirrors
    # the caller's intent and the persisted CSV stays alongside the metric
    # used to compute it.
    report = generate_eval_report(
        [_make_review(score=60), _make_review(score=70)],
        output_dir=tmp_path,
        pass_threshold=65,
    )
    assert report.summary.pass_threshold == 65
    assert report.summary.passing == 1
    assert report.summary.pass_rate == 0.5
