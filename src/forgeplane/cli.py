# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface for Forgeplane.

This module defines the Typer app, CLI commands, and helper functions used to
scan documentation folders and print reports in different formats.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

import typer
import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from forgeplane.core.config import configure_logging, load_settings
from forgeplane.llm.client import DEFAULT_MODEL, LLMClient, LLMError, OpenAIChatClient
from forgeplane.specs.reviewer import review_spec_file
from forgeplane.specs.scanner import (
    PARTIAL_THRESHOLD,
    READY_THRESHOLD,
    ScanReport,
    build_scan_summary,
    markdown_entries,
    score_spec_file,
)
from forgeplane.specs.schemas import Readiness, ScanResult, SpecReview

# Typer uses this object to collect commands and expose them as a CLI.
# The console script in pyproject.toml points to this app object.
app = typer.Typer(help="Forgeplane - a tool for working with API specifications")

# Rich is used for formatted text reports in the terminal.
console = Console()

# Keep output formats type-safe and limited to the values supported below.
OutputFormat: TypeAlias = Literal["json", "text", "yaml"]

# Output formats that can be persisted to disk. Text rendering is for the
# terminal only because the rich tables embed ANSI control codes that aren't
# meant to be parsed by downstream CI tooling.
SERIALISABLE_FORMATS: frozenset[OutputFormat] = frozenset({"json", "yaml"})

# Filename-friendly timestamp pattern for saved reports. ``%Y%m%dT%H%M%SZ``
# keeps the value sortable, free of characters that need shell quoting, and
# self-documents the UTC zone via the trailing ``Z``.
_TIMESTAMP_FORMAT: str = "%Y%m%dT%H%M%SZ"

# Readiness label → rich colour used by the readiness table. Centralising the
# mapping keeps the table cell and the score column in sync.
_READINESS_COLORS: dict[Readiness, str] = {
    "ready": "green",
    "partial": "yellow",
    "not_ready": "red",
}


def score_color(score: int) -> str:
    """Return the rich colour name for a numeric readiness score."""
    # Green for production-grade specs, yellow for partial drafts, red for
    # specs that still need substantial work. Thresholds are defined in the
    # scanner module so the CLI stays a thin presentation layer.
    if score >= READY_THRESHOLD:
        return "green"
    if score >= PARTIAL_THRESHOLD:
        return "yellow"
    return "red"


def readiness_color(readiness: Readiness) -> str:
    """Return the rich colour name for a readiness enum value."""
    return _READINESS_COLORS[readiness]


def print_text_report(report: ScanReport, target: Console | None = None) -> None:
    # ``target`` lets a caller (for example the artifact script) redirect output
    # to a recording console; the CLI itself always uses the module default.
    out = target or console
    # Rich Table renders a clean table in the terminal.
    table = Table(title="Forgeplane scan report")
    table.add_column("Metric")
    table.add_column("Value")

    table.add_row("Path", report["path"])
    table.add_row("Files", str(report["files_count"]))
    table.add_row("Total size", f"{report['total_size_bytes']} bytes")
    table.add_row(
        "Extensions",
        ", ".join(f"{ext}: {count}" for ext, count in report["extensions"].items())
        or "none",
    )

    out.print(table)

    if report["files"]:
        # Print the relative file list below the summary table.
        out.print("\nFiles:")
        for file in report["files"]:
            out.print(f"  - {file}")


def build_readiness_table(results: list[ScanResult]) -> Table:
    """Build the rich Table that renders per-file readiness results."""
    # ``title_style`` keeps the table heading aligned with the rest of the CLI
    # which is otherwise rendered without explicit styling.
    table = Table(
        title="Spec readiness",
        title_style="bold",
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("File", overflow="fold")
    table.add_column("Score", justify="right")
    table.add_column("Missing", overflow="fold")
    table.add_column("Readiness")

    for result in results:
        score_style = score_color(result.score)
        readiness_style = readiness_color(result.readiness)
        # Apply colours via rich markup so a downstream ``Console`` capture
        # (HTML/SVG export, recording) preserves the styling.
        score_cell = f"[bold {score_style}]{result.score}[/]"
        readiness_cell = f"[bold {readiness_style}]{result.readiness}[/]"
        missing_cell = ", ".join(result.missing) if result.missing else "[green]none[/]"
        table.add_row(result.file, score_cell, missing_cell, readiness_cell)

    return table


def print_readiness_table(
    results: list[ScanResult], target: Console | None = None
) -> None:
    """Render the readiness table for the given results, if any."""
    # Suppress the table entirely when no Markdown specs were discovered so
    # the CLI output stays uncluttered for non-spec directories.
    if not results:
        return
    (target or console).print(build_readiness_table(results))


def _serialise_report(report: ScanReport, output_format: OutputFormat) -> str:
    """Render the report payload as a string in the requested machine format.

    The returned string always ends with exactly one ``\n`` so the stdout
    branch and the on-disk save path can emit byte-identical content. A CI
    job that diffs the captured stdout against the archived file then sees
    no spurious mismatch, regardless of whether the underlying serialiser
    happened to append a trailing newline.
    """
    if output_format == "json":
        body = json.dumps(report, ensure_ascii=False, indent=2)
    elif output_format == "yaml":
        body = yaml.safe_dump(report, allow_unicode=True, sort_keys=False)
    else:
        # ``text`` is intentionally rejected here; both call sites gate on
        # SERIALISABLE_FORMATS, so this branch is unreachable in normal use
        # and exists only as a defensive guard for future call sites.
        raise ValueError(  # pragma: no cover
            f"Unsupported serialisable format: {output_format!r}"
        )
    # ``json.dumps`` omits the trailing newline; ``yaml.safe_dump`` includes
    # one. Normalise both so the payload ends with exactly one ``\n``.
    return body.rstrip("\n") + "\n"


def _build_report_filename(
    output_format: OutputFormat, *, now: datetime | None = None
) -> str:
    """Return a ``scan_{timestamp}.{ext}`` filename for the chosen format."""
    # Default to a fresh UTC timestamp; tests pin ``now`` to assert on the
    # produced filename without depending on wall-clock time.
    moment = now or datetime.now(UTC)
    return f"scan_{moment.strftime(_TIMESTAMP_FORMAT)}.{output_format}"


def save_report(
    report: ScanReport,
    output_dir: Path,
    output_format: OutputFormat,
    *,
    now: datetime | None = None,
) -> Path:
    """Write ``report`` under ``output_dir`` and return the written path."""
    # The CLI rejects ``text`` before reaching this helper, but library callers
    # benefit from an explicit, typed error rather than a malformed file on
    # disk.
    if output_format not in SERIALISABLE_FORMATS:
        raise ValueError(
            "save_report only supports the json and yaml formats; "
            f"got {output_format!r}."
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / _build_report_filename(output_format, now=now)
    path.write_text(_serialise_report(report, output_format), encoding="utf-8")
    return path


def _build_progress() -> Progress:
    # Spinner + bar + "n of m" + elapsed time produce a readable progress line
    # without overwhelming narrow terminals. The progress bar is visual
    # feedback, not report data, so route it to stderr (alongside the rich
    # log handler) and keep stdout reserved for the JSON/YAML payload that
    # CI jobs pipe into jq or yq.
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=Console(stderr=True),
        transient=True,
    )


@app.callback()
def main() -> None:
    # The callback makes Typer create an app with subcommands:
    # forgeplane scan ..., forgeplane generate ...
    """Forgeplane CLI."""


@app.command()
def generate(
    # Annotated lets Typer combine Python types with CLI argument metadata.
    url: Annotated[str, typer.Argument(help="URL to generate the specification from")],
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output file"),
    ] = "spec.yaml",
) -> None:
    """Generate an API specification."""
    typer.echo(f"Generating a specification for {url} into {output}...")


def build_review_table(review: SpecReview) -> Table:
    """Render a :class:`SpecReview` as a two-column rich Table."""
    # ``show_lines=True`` keeps multi-line bullet lists visually separated so
    # users can scan categories quickly when several findings line up.
    title = f"Spec review · {review.file}" if review.file is not None else "Spec review"
    table = Table(
        title=title,
        title_style="bold",
        header_style="bold cyan",
        show_lines=True,
    )
    table.add_column("Category", overflow="fold")
    table.add_column("Findings", overflow="fold")

    # Score gets its own row at the top with the same colour scheme used by
    # the scan readiness table so users can intuit the value without
    # re-reading the legend.
    score_style = score_color(review.score)
    table.add_row("Score", f"[bold {score_style}]{review.score}[/]")

    # Each category renders as a bullet list, or an explicit "none" placeholder
    # when the LLM did not flag anything. Keeping the empty-state visible
    # prevents users from confusing an empty list with a missing key.
    _add_review_findings(table, "Ambiguities", review.ambiguities)
    _add_review_findings(
        table, "Missing acceptance criteria", review.missing_acceptance_criteria
    )
    _add_review_findings(table, "Recommendations", review.recommendations)
    _add_review_findings(table, "Risks", review.risks)
    return table


def _add_review_findings(table: Table, label: str, items: list[str]) -> None:
    """Append a category row that bullet-lists ``items`` under ``label``."""
    if not items:
        table.add_row(label, "[green]none[/]")
        return
    body = "\n".join(f"• {item}" for item in items)
    table.add_row(label, body)


def print_review(review: SpecReview, target: Console | None = None) -> None:
    """Render a :class:`SpecReview` to the terminal as a rich Table."""
    (target or console).print(build_review_table(review))


def _make_review_client(model: str) -> LLMClient:
    """Build the default LLM client used by ``forgeplane review``.

    The CLI calls this once per invocation; tests monkey-patch the function so
    they can substitute a deterministic in-memory client without touching the
    network or the environment.
    """
    settings = load_settings()
    if not settings.openai_api_key:
        # ``BadParameter`` keeps the error surface consistent with the rest of
        # the CLI: Typer renders it as a single red line with no traceback.
        raise typer.BadParameter(
            "OPENAI_API_KEY is required for `forgeplane review`. "
            "Add it to your .env file or set it in the process environment.",
            param_hint="OPENAI_API_KEY",
        )
    return OpenAIChatClient(api_key=settings.openai_api_key, model=model)


@app.command()
def review(
    # File must exist before we make any LLM call so users get an immediate
    # error on typos instead of paying for a no-op API request.
    path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Path to the Markdown specification to review",
        ),
    ],
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            "--format",
            "-f",
            case_sensitive=False,
            help="Review format: json, text, or yaml",
        ),
    ] = "text",
    model: Annotated[
        str,
        typer.Option(
            "--model",
            "-m",
            help=(
                "Chat completions model name. Defaults to a small, fast model "
                "suitable for review-time prompts."
            ),
        ),
    ] = DEFAULT_MODEL,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Log review steps at DEBUG level using a rich handler.",
        ),
    ] = False,
) -> None:
    """Send a Markdown spec to an LLM reviewer and render the structured result."""
    logger = configure_logging(verbose=verbose)
    logger.info("Reviewing spec: %s", path)
    client = _make_review_client(model)
    try:
        result = review_spec_file(path, client)
    except LLMError as exc:
        # ``BadParameter`` makes Typer exit with a clean non-zero status and
        # the error text rendered on stderr, which is what CI jobs want.
        logger.error("LLM review failed: %s", exc)
        raise typer.BadParameter(str(exc), param_hint="LLM") from exc

    logger.debug("Rendering review in %s format", output_format)
    if output_format == "json":
        # ``model_dump_json`` already guarantees a JSON object with all
        # schema-validated fields present; the trailing ``\n`` mirrors the
        # scan command's byte-identity contract.
        typer.echo(result.model_dump_json(indent=2))
    elif output_format == "yaml":
        typer.echo(
            yaml.safe_dump(result.model_dump(), allow_unicode=True, sort_keys=False),
            nl=False,
        )
    else:
        # ``text`` is the default human-facing format.
        print_review(result)


@app.command()
def scan(
    # This argument becomes the required PATH value in `forgeplane scan PATH`.
    path: Annotated[
        Path,
        typer.Argument(
            # Typer checks that the path exists and is a directory, not a file.
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Path to the documentation directory",
        ),
    ],
    # Format controls how the assembled report is rendered to the user.
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            # Users can write --format json or the shorter -f json.
            "--format",
            "-f",
            case_sensitive=False,
            help="Report format: json, text, or yaml",
        ),
    ] = "text",
    # --verbose lifts the Forgeplane logger to DEBUG for the current run so
    # users can trace every scan step without editing .env.
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Log scan steps at DEBUG level using a rich handler.",
        ),
    ] = False,
    # CI integration: when set, the JSON or YAML payload is persisted to
    # ``{output_dir}/scan_{timestamp}.{ext}`` alongside being printed to stdout
    # so downstream jobs can archive the artifact without parsing logs.
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            file_okay=False,
            dir_okay=True,
            writable=True,
            resolve_path=True,
            help=(
                "Directory where the JSON or YAML report is written as "
                "scan_{timestamp}.{ext}. Requires --format json or yaml. "
                "Created if missing."
            ),
        ),
    ] = None,
) -> None:
    """Scan a documentation directory and print a report."""
    logger = configure_logging(verbose=verbose)
    # Reject the unsupported combination up front so users do not pay for a
    # full scan only to receive a usage error at the end.
    if output_dir is not None and output_format not in SERIALISABLE_FORMATS:
        raise typer.BadParameter(
            "--output-dir requires --format json or yaml; the text format is "
            "for terminal rendering only.",
            param_hint="--output-dir",
        )
    logger.info("Scanning directory: %s", path)
    summary = build_scan_summary(path)
    logger.debug(
        "Discovered %d file(s); total size %d bytes",
        len(summary.files),
        summary.total_size_bytes,
    )

    # Score each Markdown spec under a rich Progress so multi-file scans give
    # visible feedback. The text-format branch later renders a coloured table
    # built from these results.
    md_entries = markdown_entries(summary)
    results: list[ScanResult] = []
    if md_entries:
        logger.debug("Scoring %d Markdown spec(s)", len(md_entries))
        with _build_progress() as progress:
            task_id = progress.add_task("Scoring specs", total=len(md_entries))
            for entry in md_entries:
                results.append(score_spec_file(entry))
                progress.advance(task_id)

    report = summary.to_report(results=results)
    logger.debug("Rendering report in %s format", output_format)

    # The same report can be rendered in different formats.
    if output_format in SERIALISABLE_FORMATS:
        # JSON is useful for scripts and CI pipelines; YAML is friendlier in
        # documentation workflows. Both share the same serialised payload.
        payload = _serialise_report(report, output_format)
        # ``nl=False`` keeps stdout byte-identical to the saved file: the
        # payload already ends with a single ``\n`` from _serialise_report,
        # and echo's default trailing newline would otherwise duplicate it.
        typer.echo(payload, nl=False)
        if output_dir is not None:
            saved = save_report(report, output_dir, output_format)
            # Log to stderr (rich handler) so stdout stays a clean payload
            # that CI jobs can pipe into jq/yq without filtering noise.
            logger.info("Report saved to %s", saved)
    else:
        # Text output is the default for humans using the CLI directly.
        print_text_report(report)
        print_readiness_table(results)
