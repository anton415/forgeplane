# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Render a deterministic SVG "screenshot" of the rich readiness report.

The bundled examples/ directory is scanned, each spec is scored, and the
resulting summary + readiness tables are captured with ``Console.save_svg``.
Committing the SVG keeps the issue #36 artifact reproducible and lets the
README embed a visual sample without binary screenshots.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from forgeplane.cli import print_readiness_table, print_text_report
from forgeplane.specs.scanner import (
    build_scan_summary,
    markdown_entries,
    score_spec_file,
)

# Project layout: this script lives in ``scripts/`` next to the source tree.
REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
OUTPUT = REPO_ROOT / "docs" / "reports" / "scan_report.svg"


def main() -> None:
    # Use a relative path so the rendered "Path" cell stays portable and the
    # committed SVG does not embed the developer's home directory.
    target = EXAMPLES.relative_to(REPO_ROOT)
    summary = build_scan_summary(target)
    results = [score_spec_file(entry) for entry in markdown_entries(summary)]
    report = summary.to_report(results=results)

    # ``record=True`` captures every printed cell so save_svg can later
    # serialise the whole frame. Fixed width keeps the SVG layout stable
    # across machines and CI environments.
    console = Console(record=True, width=100)
    print_text_report(report, target=console)
    console.print()
    print_readiness_table(results, target=console)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUTPUT), title="forgeplane scan examples/")


if __name__ == "__main__":
    main()
