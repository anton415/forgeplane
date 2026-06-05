# Forgeplane

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
- relative file list.

Output formats:

- `text`
- `json`
- `yaml`

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

## Usage

### Scan documentation

```bash
forgeplane scan path/to/docs
```

Text output is used by default.

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
  ]
}
```

## Project structure

```text
forgeplane/
├── src/
│   └── forgeplane/
│       ├── __init__.py
│       ├── cli.py
│       └── main.py
├── main.py
├── CONTRIBUTING.md
├── LICENSE
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

## Roadmap

Planned directions:

- Improve documentation scanning and validation.
- Add checks for OpenAPI files.
- Implement real API specification generation.
- Support richer reports for API readiness.
- Add tests.
- Add CI.
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

See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup and contribution licensing.

## License

Forgeplane is licensed under the Apache License 2.0.

Copyright 2026 Anton Serdyuchenko.
