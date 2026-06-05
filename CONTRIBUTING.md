# Contributing to Forgeplane

Forgeplane is in early development. The public API and CLI commands may change as the project takes shape.

Small, focused pull requests are preferred. For larger changes, please open an issue first so the direction can be discussed before implementation work begins.

## Local Development

Forgeplane uses Python `>=3.13` and `uv`.

Install dependencies:

```bash
uv sync
```

Run the CLI locally:

```bash
uv run forgeplane --help
```

Run a basic scan check:

```bash
uv run forgeplane scan docs
```

## Contribution Licensing

By contributing to Forgeplane, you agree that your contributions will be licensed under the Apache License 2.0.
