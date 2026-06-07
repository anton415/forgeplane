# Contributing to Forgeplane

Forgeplane is in early development. The public API and CLI commands may change as the project takes shape.

Small, focused pull requests are preferred. For larger changes, please open an issue first so the direction can be discussed before implementation work begins.

## Code of Conduct

By participating in this project you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md), which is based on the Contributor Covenant 2.1.

To report unacceptable behavior, contact anton415@gmail.com. To report a security vulnerability, follow the process described in [SECURITY.md](SECURITY.md) instead of opening a public issue.

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

Run a basic scan check against a documentation directory:

```bash
uv run forgeplane scan path/to/docs
```

## Contribution Licensing

By contributing to Forgeplane, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
