# Forgeplane

[![Tests](https://github.com/anton415/forgeplane/actions/workflows/test.yml/badge.svg)](https://github.com/anton415/forgeplane/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/anton415/forgeplane/graph/badge.svg)](https://codecov.io/gh/anton415/forgeplane)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)

Forgeplane is an early-stage CLI tool for working with API specifications and documentation.

The project is intended to help developers analyze API documentation, generate or prepare API specifications, and gradually move toward a more structured, specification-driven engineering workflow.

> Status: early development. The public API and CLI commands may change.

## Why Forgeplane?

Modern software development increasingly depends on clear API contracts. Forgeplane is designed as a practical tool for turning API-related materials into more useful engineering artifacts: reports, specifications, checks, and eventually generated API specs.

The long-term goal is to make API specification work faster, more repeatable, and more suitable for AI-assisted development workflows.

## Current capabilities

Forgeplane currently provides a Python CLI with the following early commands:

- `scan` — scans a documentation directory and prints a report.
- `generate` — placeholder command for future API specification generation.

The `scan` command reports:

- scanned path;
- number of files;
- total file size;
- file extensions distribution;
- relative file list;
- per-`.md` readiness results (score, missing sections, weak sections, TODO findings, and a readiness label).

Output formats:

- `text` — human-friendly tables rendered through [`rich`](https://rich.readthedocs.io/), including a per-file readiness table with green/yellow/red colour coding and a transient progress bar while specs are scored.
- `json` — machine-readable payload that includes the `results` array with one entry per scanned `.md` file.
- `yaml` — same payload as `json`, formatted for documentation workflows.

The codebase also includes early Pydantic schemas for API readiness checks:

- `SectionCheck` — describes whether an expected documentation section exists, how much content it has, and whether it contains TODO markers.
- `ScanResult` — describes readiness for one file, including a normalized score from `0` to `100`, missing sections, weak sections, TODO findings, and a constrained readiness value: `ready`, `partial`, or `not_ready`.

The filesystem scanning logic is split into typed modules:

- `core/files.py` — recursively discovers Markdown spec files and exposes a typed UTF-8 read helper used by the spec scanners.
- `core/config.py` — loads runtime settings (`OPENAI_API_KEY`, `LOG_LEVEL`) from `.env` via `python-dotenv` and configures the Forgeplane logger with a `rich` handler.
- `specs/files.py` — collects file metadata, normalizes extensions, and keeps scan output deterministic.
- `specs/scanner.py` — aggregates file metadata into the public scan report used by the CLI and parses Markdown spec sections.

The Markdown section parser extracts the body of each expected `##` heading from a spec file and returns a `dict[str, str | None]` keyed by the expected section names: `Goal`, `Context`, `Acceptance Criteria`, `Risks`, `Open Questions`. Missing or empty sections collapse to `None`. Headings follow CommonMark ATX rules (up to three spaces of indent, an optional closing run of `#`s), and `##` lines that appear inside fenced code blocks are ignored. Sample inputs live in `examples/good_spec.md` and `examples/weak_spec.md`.

The source tree is checked with `mypy` in strict mode.

## Installation

Forgeplane uses Python `>=3.13`.

Clone the repository:

```bash
git clone https://github.com/anton415/forgeplane.git
cd forgeplane
```

Install dependencies with `uv`:

```bash
uv sync
```

Run the CLI:

```bash
uv run forgeplane --help
```

If you prefer editable installation with `pip`:

```bash
pip install -e .
forgeplane --help
```

## Configuration

Forgeplane loads runtime settings from a `.env` file in the current working
directory via `python-dotenv`. Variables already set in the process
environment take precedence over the file, which keeps CI configuration
stable when a `.env` file is missing.

Copy the bundled example to get started:

```bash
cp .env.example .env
```

Supported variables:

- `OPENAI_API_KEY` — reserved for future LLM-backed commands (review, eval).
  Leave empty until the LLM integration lands; Forgeplane never logs its
  value.
- `LOG_LEVEL` — threshold for the Forgeplane logger. One of `DEBUG`, `INFO`,
  `WARNING`, `ERROR`, `CRITICAL`. Defaults to `INFO` when unset or
  unrecognised. Pass `--verbose` on the CLI to force `DEBUG` for a single
  command invocation.

Logging is wired to `rich.logging.RichHandler`, so log records render with
the same styling as the rest of the CLI output.

## Usage

### Scan documentation

```bash
forgeplane scan path/to/docs
```

Text output is used by default. While the command processes Markdown specs,
`rich.progress` shows a spinner with the current file count and elapsed time.
Once scoring is finished, two tables are printed: a directory summary and a
readiness table with the columns **File**, **Score**, **Missing**, and
**Readiness**.

Colour coding follows the readiness thresholds:

- **green** — score ≥ 80, readiness `ready`;
- **yellow** — score ≥ 60, readiness `partial`;
- **red** — score < 60, readiness `not_ready`.

A reproducible SVG snapshot of the report against the bundled examples lives
at [`docs/reports/scan_report.svg`](docs/reports/scan_report.svg) and can be
regenerated with:

```bash
uv run python scripts/generate_report_artifact.py
```

![Forgeplane scan report](docs/reports/scan_report.svg)

### Verbose logging

Add `--verbose` (or `-v`) to log each scan step at `DEBUG` through a `rich`
handler. The flag overrides `LOG_LEVEL` for the current invocation:

```bash
forgeplane scan path/to/docs --verbose
```

### JSON output

```bash
forgeplane scan path/to/docs --format json
```

### YAML output

```bash
forgeplane scan path/to/docs --format yaml
```

### Generate API specification

```bash
forgeplane generate https://example.com/api --output spec.yaml
```

This command is currently a placeholder and will be implemented in future versions.

## Example

```bash
forgeplane scan path/to/docs --format json
```

Example output:

```json
{
  "path": "/path/to/docs",
  "files_count": 2,
  "total_size_bytes": 128,
  "extensions": {
    ".md": 1,
    ".yaml": 1
  },
  "files": [
    "api.md",
    "openapi.yaml"
  ],
  "results": [
    {
      "file": "api.md",
      "score": 80,
      "missing": [],
      "weak": ["Open Questions"],
      "todos_found": [],
      "readiness": "ready"
    }
  ]
}
```

The `results` array contains one entry per discovered `.md` file. Each entry
is the JSON form of the `ScanResult` Pydantic model defined in
`forgeplane/specs/schemas.py`.

## Project structure

```text
forgeplane/
├── src/
│   └── forgeplane/
│       ├── __init__.py
│       ├── cli.py
│       ├── main.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── config.py
│       │   └── files.py
│       └── specs/
│           ├── __init__.py
│           ├── files.py
│           ├── scanner.py
│           └── schemas.py
├── tests/
│   ├── conftest.py
│   ├── test_cli_rich.py
│   ├── test_config.py
│   ├── test_files.py
│   ├── test_scanner.py
│   └── test_scoring.py
├── examples/
│   ├── good_spec.md
│   └── weak_spec.md
├── scripts/
│   └── generate_report_artifact.py
├── docs/
│   └── reports/
│       └── scan_report.svg
├── .env.example
├── Makefile
├── main.py
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── LICENSE
├── NOTICE
├── SECURITY.md
├── pyproject.toml
├── uv.lock
└── README.md
```

## Development

Install dependencies:

```bash
uv sync
```

Run the CLI locally:

```bash
uv run forgeplane --help
```

Run a scan against a documentation directory:

```bash
uv run forgeplane scan path/to/docs
```

Run the full local lint gate:

```bash
make lint
```

`make lint` checks Ruff formatting, runs Ruff lint rules, and then runs strict
`mypy` against `src/`. Ruff is configured in `pyproject.toml` for Python 3.13,
import sorting, pyupgrade rules, bugbear checks, and safe simplifications.

Run Ruff directly:

```bash
uv run ruff check .
uv run ruff format .
```

Run strict type checking:

```bash
uv run mypy src/
```

Run the test suite:

```bash
uv run pytest -v
```

Tests live under `tests/` and share fixtures defined in `tests/conftest.py`, which load the bundled `examples/good_spec.md` and `examples/weak_spec.md` through the section parser. `test_files.py` covers Markdown discovery against empty and nested directories, `test_scanner.py` covers the Markdown section parser, `test_config.py` covers environment loading, log-level normalization, and the `--verbose` CLI flag, `test_scoring.py` covers readiness scoring and the threshold-to-enum mapping, and `test_cli_rich.py` covers the rich rendering helpers (colour mapping, readiness table, and the JSON `results` field).

## Roadmap

Planned directions:

- Improve documentation scanning and validation.
- Add checks for OpenAPI files.
- Implement real API specification generation.
- Support richer reports for API readiness.
- Add examples for real-world API documentation.
- Expand contribution guidelines as the project matures.
- Publish package metadata and release process.

## Design principles

Forgeplane should be:

- CLI-first — easy to run locally, in CI, and from scripts.
- Spec-oriented — focused on API contracts and structured engineering artifacts.
- AI-friendly — useful as part of AI-assisted development pipelines.
- Incremental — helpful even before full automatic generation exists.
- Transparent — reports should be readable by both humans and tools.

## Contributing

The project is at an early stage, so contributions should start with small, focused changes.

Good first contribution areas:

- improve CLI help text;
- add tests for `scan`;
- improve output formatting;
- add OpenAPI validation;
- improve example documentation;
- add GitHub Actions CI.

Before submitting larger changes, open an issue describing the proposed direction.

See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup and contribution licensing, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community guidelines.

## Security

To report a security vulnerability privately, please follow the process in [SECURITY.md](SECURITY.md).

## License

Forgeplane is licensed under the [Apache License 2.0](LICENSE). See the [NOTICE](NOTICE) file for attribution requirements.

Copyright 2026 Anton Serdyuchenko.
