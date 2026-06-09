# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Eval reporting over batches of :class:`SpecReview` results.

The reporter ingests N reviews produced by the LLM-backed reviewer, builds a
typed pandas DataFrame, and computes aggregate quality metrics — mean score
and pass rate — that can be tracked over time. Results can be persisted to
CSV alongside a timestamp so a CI job can archive every run as a build
artifact and downstream tools can plot prompt quality across runs.

The same data is rendered through a ``rich`` Table for terminal consumption
so a developer iterating on prompts locally sees the per-file detail plus the
aggregate summary in one place.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pandas as pd
from rich.console import Console
from rich.table import Table

from forgeplane.core.config import get_logger
from forgeplane.specs.schemas import SpecReview

# Module-level logger so eval steps surface under ``--verbose``.
_logger = get_logger(__name__)

# Default pass threshold. Kept aligned with the scanner's ``READY_THRESHOLD``
# so a single 0..100 cut-off applies across the static scan and the LLM
# review signals; importing the constant directly would pull in the scanner
# module's heavy regex compilation just to read one integer, so the value is
# repeated here as a Final.
DEFAULT_PASS_THRESHOLD: Final[int] = 80

# Ordered DataFrame columns. Keeping the order explicit (rather than relying
# on dict insertion) makes the resulting CSV diff-friendly across releases:
# downstream dashboards can pin the schema and only break when the order is
# changed on purpose.
DATAFRAME_COLUMNS: Final[tuple[str, ...]] = (
    "file",
    "score",
    "ambiguities_count",
    "missing_acceptance_criteria_count",
    "recommendations_count",
    "risks_count",
)

# Filename-friendly timestamp pattern shared with the scan CLI so archived
# artifacts from both pipelines sort together in chronological order.
_TIMESTAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True, slots=True)
class EvalSummary:
    """Aggregate quality metrics computed across a batch of reviews."""

    # Total number of reviews that contributed to the aggregates. A separate
    # field (rather than reading ``len(df)``) keeps the summary self-contained
    # for callers that only persist the metrics.
    total: int
    # Arithmetic mean of the ``score`` column. ``0.0`` for an empty batch so
    # downstream consumers never have to special-case ``NaN``.
    mean_score: float
    # Fraction of reviews whose score is greater than or equal to the
    # threshold below, expressed in 0..1 so it composes with percentage
    # formatters without a second conversion.
    pass_rate: float
    # Threshold used to compute :attr:`pass_rate`. Carried with the summary so
    # a dashboard ingesting historical CSVs can reconstruct the cut-off used
    # at the time of each run.
    pass_threshold: int
    # Absolute number of passing reviews; exposed for dashboards that prefer
    # an explicit count alongside the rate.
    passing: int


def build_review_dataframe(reviews: Iterable[SpecReview]) -> pd.DataFrame:
    """Build a typed DataFrame from a batch of :class:`SpecReview` results.

    Each row captures one review: the source filename (or ``"<inline>"`` when
    the LLM was fed an in-memory spec), the numeric score, and the lengths of
    the four finding categories. Storing counts rather than the raw lists
    keeps the CSV narrow and well-typed; the original lists remain available
    on the source :class:`SpecReview` objects when finer detail is required.
    """
    # Materialise the records up front so the iterable is consumed exactly
    # once, regardless of how the caller produced the reviews (generator,
    # list, mapped iterator, …).
    records: list[dict[str, object]] = [
        {
            "file": review.file or "<inline>",
            "score": review.score,
            "ambiguities_count": len(review.ambiguities),
            "missing_acceptance_criteria_count": len(
                review.missing_acceptance_criteria
            ),
            "recommendations_count": len(review.recommendations),
            "risks_count": len(review.risks),
        }
        for review in reviews
    ]
    # Pin the column order even when ``records`` is empty so the resulting
    # DataFrame has the contracted shape (``df[col]`` never KeyErrors) and
    # CSVs are interchangeable across runs with zero or many reviews.
    df = pd.DataFrame.from_records(records, columns=list(DATAFRAME_COLUMNS))
    _logger.debug("Built review DataFrame with %d row(s)", len(df))
    return df


def compute_summary(
    df: pd.DataFrame, *, pass_threshold: int = DEFAULT_PASS_THRESHOLD
) -> EvalSummary:
    """Aggregate ``df`` into the headline mean-score and pass-rate metrics."""
    total = int(len(df))
    if total == 0:
        # An empty batch has no meaningful mean or rate; returning explicit
        # zeros (rather than ``NaN`` from pandas) keeps the JSON/CSV output
        # parseable by tools that do not handle ``NaN`` gracefully.
        return EvalSummary(
            total=0,
            mean_score=0.0,
            pass_rate=0.0,
            pass_threshold=pass_threshold,
            passing=0,
        )
    score_series = df["score"]
    mean_score = float(score_series.mean())
    passing = int((score_series >= pass_threshold).sum())
    pass_rate = passing / total
    return EvalSummary(
        total=total,
        mean_score=mean_score,
        pass_rate=pass_rate,
        pass_threshold=pass_threshold,
        passing=passing,
    )


def _build_eval_filename(*, now: datetime | None = None) -> str:
    """Return the timestamped ``eval_{timestamp}.csv`` filename."""
    moment = now or datetime.now(UTC)
    return f"eval_{moment.strftime(_TIMESTAMP_FORMAT)}.csv"


def save_eval_csv(
    df: pd.DataFrame,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> Path:
    """Persist ``df`` under ``output_dir`` as ``eval_{timestamp}.csv``.

    The directory is created if it does not exist. The returned path can be
    archived by CI as a build artifact so consecutive runs build up a history
    of prompt quality without anyone wiring a database.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / _build_eval_filename(now=now)
    # ``index=False`` keeps the CSV column layout aligned with
    # :data:`DATAFRAME_COLUMNS`; otherwise pandas would inject an extra
    # unnamed index column that downstream consumers would have to skip.
    df.to_csv(target, index=False)
    _logger.info("Eval CSV saved to %s", target)
    return target


def _format_pass_rate(pass_rate: float) -> str:
    # Two-decimal percentage matches the precision of ``mean_score`` below,
    # which keeps the summary row visually consistent regardless of how many
    # reviews contributed to the batch.
    return f"{pass_rate * 100:.2f}%"


def build_eval_table(df: pd.DataFrame, summary: EvalSummary) -> Table:
    """Render the per-file scores and the aggregate metrics as a rich Table.

    The table is split into a per-row section and a trailing summary block so
    a developer can read both the detail and the headline numbers in a single
    pass. The aggregate rows are emphasised through ``end_section=True`` to
    visually separate them from the per-file scores above.
    """
    table = Table(
        title="Forgeplane eval report",
        title_style="bold",
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("File", overflow="fold")
    table.add_column("Score", justify="right")
    table.add_column("Ambiguities", justify="right")
    table.add_column("Missing AC", justify="right")
    table.add_column("Recommendations", justify="right")
    table.add_column("Risks", justify="right")

    # Extract each column as a Python list so the values reach rich.Table as
    # plain ``str``/``int`` rather than pandas scalars: this keeps the loop
    # well-typed under strict mypy without leaning on ``Any``-shaped tuple
    # accessors that ``itertuples`` exposes.
    files = df["file"].astype(str).tolist()
    scores = df["score"].astype(int).tolist()
    ambiguities_counts = df["ambiguities_count"].astype(int).tolist()
    missing_ac_counts = df["missing_acceptance_criteria_count"].astype(int).tolist()
    recommendations_counts = df["recommendations_count"].astype(int).tolist()
    risks_counts = df["risks_count"].astype(int).tolist()

    for file, score, ambiguities, missing_ac, recommendations, risks in zip(
        files,
        scores,
        ambiguities_counts,
        missing_ac_counts,
        recommendations_counts,
        risks_counts,
        strict=True,
    ):
        table.add_row(
            file,
            str(score),
            str(ambiguities),
            str(missing_ac),
            str(recommendations),
            str(risks),
        )

    # Headline metrics live in a final block that spans the score column so
    # the eye can compare ``mean_score`` against the per-row column directly
    # above it.
    table.add_section()
    table.add_row(
        "[bold]Total reviews[/]",
        str(summary.total),
        "",
        "",
        "",
        "",
    )
    table.add_row(
        "[bold]Mean score[/]",
        f"{summary.mean_score:.2f}",
        "",
        "",
        "",
        "",
    )
    table.add_row(
        f"[bold]Pass rate (≥{summary.pass_threshold})[/]",
        _format_pass_rate(summary.pass_rate),
        "",
        "",
        "",
        "",
    )
    return table


def print_eval_table(
    df: pd.DataFrame,
    summary: EvalSummary,
    target: Console | None = None,
) -> None:
    """Render the eval table to ``target`` (defaults to a fresh stdout Console)."""
    # A fresh ``Console`` keeps this function independent of any module-level
    # singleton, which makes it straightforward to redirect output in tests.
    (target or Console()).print(build_eval_table(df, summary))


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Bundled result returned by :func:`generate_eval_report`."""

    # The DataFrame is retained so callers can perform follow-up analysis
    # (groupby, filter, plot) without re-deriving it from the reviews.
    dataframe: pd.DataFrame
    summary: EvalSummary
    # ``csv_path`` is ``None`` when no ``output_dir`` was supplied; CI callers
    # always set the directory and rely on the returned path to archive the
    # artifact.
    csv_path: Path | None


def generate_eval_report(
    reviews: Iterable[SpecReview],
    *,
    output_dir: Path | None = None,
    pass_threshold: int = DEFAULT_PASS_THRESHOLD,
    now: datetime | None = None,
) -> EvalReport:
    """Run the full eval pipeline: build → summarise → optionally persist.

    ``output_dir`` is optional so library callers driving the pipeline in
    memory (notebooks, tests) avoid writing files; passing a directory makes
    the function suitable for CI integrations that archive a CSV per run.
    """
    df = build_review_dataframe(reviews)
    summary = compute_summary(df, pass_threshold=pass_threshold)
    csv_path = (
        save_eval_csv(df, output_dir, now=now) if output_dir is not None else None
    )
    return EvalReport(dataframe=df, summary=summary, csv_path=csv_path)
