# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface for Forgeplane.

This module defines the Typer app, CLI commands, and helper functions used to
scan documentation folders and print reports in different formats.
"""

from pathlib import Path
from typing import Annotated, Literal, TypeAlias
import json

import typer
import yaml
from rich.console import Console
from rich.table import Table

from forgeplane.specs.scanner import ScanReport, scan_docs

# Typer uses this object to collect commands and expose them as a CLI.
# The console script in pyproject.toml points to this app object.
app = typer.Typer(help="Forgeplane - a tool for working with API specifications")

# Rich is used for formatted text reports in the terminal.
console = Console()

# Keep output formats type-safe and limited to the values supported below.
OutputFormat: TypeAlias = Literal["json", "text", "yaml"]


def print_text_report(report: ScanReport) -> None:
    # Rich Table renders a clean table in the terminal.
    table = Table(title="Forgeplane scan report")
    table.add_column("Metric")
    table.add_column("Value")

    table.add_row("Path", report["path"])
    table.add_row("Files", str(report["files_count"]))
    table.add_row("Total size", f'{report["total_size_bytes"]} bytes')
    table.add_row(
        "Extensions",
        ", ".join(f"{ext}: {count}" for ext, count in report["extensions"].items()) or "none",
    )

    console.print(table)

    if report["files"]:
        # Print the relative file list below the summary table.
        console.print("\nFiles:")
        for file in report["files"]:
            console.print(f"  - {file}")


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
    # This option controls how the report is printed after scan_docs returns it.
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
) -> None:
    """Scan a documentation directory and print a report."""
    report = scan_docs(path)

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
