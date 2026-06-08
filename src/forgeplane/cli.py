# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface for Forgeplane.

This module defines the Typer app, CLI commands, and helper functions used to
scan documentation folders and print reports in different formats.
"""

import json
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

from forgeplane.core.config import configure_logging
from forgeplane.specs.scanner import (
    PARTIAL_THRESHOLD,
    READY_THRESHOLD,
    ScanReport,
    build_scan_summary,
    markdown_entries,
    score_spec_file,
)
from forgeplane.specs.schemas import Readiness, ScanResult

# Typer uses this object to collect commands and expose them as a CLI.
# The console script in pyproject.toml points to this app object.
app = typer.Typer(help="Forgeplane - a tool for working with API specifications")

# Rich is used for formatted text reports in the terminal.
console = Console()

# Keep output formats type-safe and limited to the values supported below.
OutputFormat: TypeAlias = Literal["json", "text", "yaml"]

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


def _build_progress() -> Progress:
    # Spinner + bar + "n of m" + elapsed time produce a readable progress line
    # without overwhelming narrow terminals.
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
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
) -> None:
    """Scan a documentation directory and print a report."""
    logger = configure_logging(verbose=verbose)
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
    if output_format == "json":
        # JSON output is useful for scripts and other tools.
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    elif output_format == "yaml":
        # YAML output is easier to read in some documentation workflows.
        typer.echo(yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    else:
        # Text output is the default for humans using the CLI directly.
        print_text_report(report)
        print_readiness_table(results)
