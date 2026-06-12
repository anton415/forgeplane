# Forgeplane architecture: core logic and the CLI layer

> Status: adopted (issue [#71](https://github.com/anton415/forgeplane/issues/71)).
> This document defines the boundary between Forgeplane's product logic and
> its command-line interface, so future interfaces (HTTP API, machine-readable
> pipelines) can reuse the same core without rewriting business logic.

## Why this boundary exists

Forgeplane is CLI-first today, but the roadmap adds interfaces that are not a
terminal: an HTTP API for workflow runs and artifacts
([#68](https://github.com/anton415/forgeplane/issues/68),
[#9](https://github.com/anton415/forgeplane/issues/9)), machine-readable
result and error formats ([#69](https://github.com/anton415/forgeplane/issues/69)),
and CI quality gates ([#54](https://github.com/anton415/forgeplane/issues/54)).
Every one of those consumers needs the same underlying capabilities — scan a
docs tree, score a spec, review a spec with an LLM, aggregate eval batches,
drive a workflow run — without inheriting Typer argument parsing or rich
terminal rendering.

The rule that follows from this is simple:

**Product logic must be callable as plain Python functions and classes.
Anything that parses command-line input or renders terminal output belongs to
the CLI layer and may never be imported by core code.**

## Layer model

Forgeplane uses three logical layers plus an adapter ring for external
systems. Dependencies point inward only.

```text
┌────────────────────────────────────────────────────────────┐
│ Interface layer (CLI today; HTTP API later)                │
│   Typer commands, flags, exit codes, rich tables,          │
│   progress bars, terminal logging handler                  │
├────────────────────────────────────────────────────────────┤
│ Application layer (use cases)                              │
│   scan docs, score spec, review spec, generate eval        │
│   report, settings loading; future: start run, approve run │
├────────────────────────────────────────────────────────────┤
│ Domain layer (models and rules)                            │
│   schemas, section parsing and scoring, readiness          │
│   thresholds, workflow state model and domain model        │
└────────────────────────────────────────────────────────────┘
   Infrastructure adapters (LLM HTTP client, redaction)
   implement ports defined by the inner layers.
```

### Domain layer

Models and validation rules. Pure logic: standard library plus Pydantic for
schema validation. No filesystem access beyond what a caller hands in, no
HTTP, no terminal.

| Code | Role |
| --- | --- |
| `specs/schemas.py` | `SectionCheck`, `ScanResult`, `SpecReview` Pydantic models |
| `specs/scanner.py` (pure functions) | `parse_sections`, `score_sections`, `classify_readiness`, the `READY_THRESHOLD` / `PARTIAL_THRESHOLD` constants |
| `workflow/states.py` | workflow/task state enums, transition tables, derivation rules |
| `workflow/model.py` | `WorkflowDefinition` / `WorkflowInstance` graph and runtime records |

### Application layer

Use cases that orchestrate domain logic and talk to the outside world through
typed values and ports. This is the public library surface: everything here
must be usable from a notebook, a test, or a future HTTP handler exactly as
the CLI uses it.

| Code | Use case |
| --- | --- |
| `specs/files.py` | collect files under a scan root, enforce `ScanPolicy` limits and the symlink policy |
| `specs/scanner.py` (orchestration) | `scan_docs`, `build_scan_summary`, `score_spec_file` — the "check spec" use case |
| `specs/reviewer.py` | `review_spec_text` / `review_spec_file` — the "review spec" use case, parameterised over the `LLMClient` port |
| `evals/reporter.py` (computation) | DataFrame building, `compute_summary`, CSV persistence, `generate_eval_report` |
| `core/config.py` (settings) | `load_settings` — `.env` parsing with the supported-keys allow-list |

Future workflow use cases — start run, pause run, approve a manual gate —
belong here too, layered on the `workflow/` domain model
([#5](https://github.com/anton415/forgeplane/issues/5),
[#8](https://github.com/anton415/forgeplane/issues/8)).

### Infrastructure adapters

Implementations of ports for external systems. The application layer defines
the contract; the adapter satisfies it.

| Code | Role |
| --- | --- |
| `llm/client.py` | `LLMClient` Protocol (the port) and `OpenAIChatClient` (the HTTP adapter with retry policy) |
| `llm/redaction.py` | security helpers that keep untrusted provider payloads out of error messages |

The reviewer never imports `OpenAIChatClient`; it accepts any
`LLMClient`-conformant object. Tests substitute an in-memory fake, and a
future interface can inject a different provider without touching the use
case.

### Interface layer (CLI)

Everything that exists because a human is sitting at a terminal: command and
flag parsing, exit codes, colour mapping, rich tables, progress bars, and the
rich logging handler. Typer and rich are interface-layer dependencies.

| Code | Role |
| --- | --- |
| `cli.py` | Typer app, `scan` / `review` / `generate` commands, rich rendering helpers |
| `main.py` | `python -m forgeplane.main` entry point |
| `scripts/generate_report_artifact.py` | captures the rich rendering as a committed SVG artifact |

## Dependency rules

1. **Imports point inward.** Interface → application → domain. The domain
   layer imports nothing from the layers above it; the application layer
   never imports the CLI.
2. **No `typer` anywhere outside the interface layer.** Core code signals
   failures with typed exceptions (`ScanError`, `LLMError`,
   `InvalidTransitionError`, …); only the CLI converts them into exit codes
   and `typer.BadParameter` messages.
3. **No `rich` rendering outside the interface layer.** Core functions return
   typed values (`ScanReport`, `SpecReview`, `EvalSummary`, DataFrames);
   tables, colours, and progress bars are built from those values in the CLI.
4. **Logging is namespace-only in core.** Core modules obtain loggers through
   `core/config.py`'s `get_logger` and emit records; only the interface layer
   installs a handler (today the rich handler via `configure_logging`). Core
   code never assumes a terminal is attached.
5. **External systems sit behind ports.** New integrations (LLM providers,
   persistence, HTTP) get a Protocol or typed contract in the inner layers
   and an adapter module outside them.

Until an automated guard exists, the rules are enforced in review; a grep for
`import typer` / `from rich` outside `cli.py`, `main.py`, and `scripts/` must
come back empty once the known violations below are fixed.

## Known violations and follow-up work

The current tree violates the boundary in three places. Each has a follow-up
implementation issue; the architecture is adopted now, and the code converges
issue by issue.

1. **Report serialization and persistence live in `cli.py`.**
   `_serialise_report`, `_build_report_filename`, `save_report`, and
   `SERIALISABLE_FORMATS` are application logic (library callers and CI
   integrations need them), but they sit in the Typer module, so saving a
   report requires importing the CLI. They move to an application module
   under `specs/`. Tracked in
   [#86](https://github.com/anton415/forgeplane/issues/86).
2. **`evals/reporter.py` mixes computation with rendering.**
   `build_eval_table` and `print_eval_table` pull `rich` into an application
   module. The DataFrame/summary/CSV pipeline stays; the table rendering
   moves to the CLI layer. Tracked in
   [#87](https://github.com/anton415/forgeplane/issues/87).
3. **`core/config.py` couples settings loading to the rich handler.**
   `load_settings` is application logic, but `_build_rich_handler` and the
   handler wiring in `configure_logging` are terminal presentation. The
   handler construction moves to the interface layer; core keeps the logger
   namespace and level resolution. Tracked in
   [#88](https://github.com/anton415/forgeplane/issues/88).

Once those land, an import-boundary guard test pins the rule so regressions
fail CI instead of waiting for review. Tracked in
[#89](https://github.com/anton415/forgeplane/issues/89).

What is already clean: `specs/scanner.py`, `specs/reviewer.py`,
`specs/files.py`, `workflow/`, and `llm/` import neither `typer` nor `rich`,
and the reviewer is already parameterised over the `LLMClient` port.

## Target package boundaries

The package names stay as they are — the layering is logical, not a rename.
The one structural change, delivered through the follow-up issues, is that
`cli.py` grows into a `cli/` package as presentation code consolidates there:

```text
src/forgeplane/
├── workflow/        # domain: state model, domain model
├── specs/           # domain models + scan/review use cases
│   └── report_io.py # NEW (issue #86): report serialization + persistence
├── evals/           # application: DataFrame, summary, CSV (rendering moves out)
├── llm/             # infrastructure: LLM port, OpenAI adapter, redaction
├── core/            # application: settings, logger namespace
└── cli/             # interface: everything typer/rich
    ├── __init__.py  # exposes `app`; `forgeplane = "forgeplane.cli:app"` keeps working
    ├── render.py    # rich tables for scan, review, and eval output
    └── logging.py   # rich handler installation (from core/config.py)
```

The console-script entry point in `pyproject.toml` keeps pointing at
`forgeplane.cli:app`; converting the module to a package is transparent to
installed users.

## Where new code goes

- **A new model, validation rule, or state machine** → domain layer
  (`specs/schemas.py`, `workflow/`).
- **A new capability or command's behaviour** (analyze, generate, start run,
  approve run) → an application-layer function that accepts typed inputs and
  returns typed results. Write it so a test can call it without a terminal.
- **A new external system** (LLM provider, storage, HTTP) → define a port in
  the inner layers, implement an adapter module outside them.
- **A new CLI command** → a thin function in the CLI layer: parse flags,
  call one application function, render the typed result, map typed
  exceptions to exit codes.
- **A new interface** (HTTP API, MCP server, TUI) → a new sibling of the CLI
  layer that imports the same application functions. If building it requires
  changing core code, that is a boundary bug — file it.

## Acceptance criteria of issue #71, mapped

- *Documented boundary between core logic and CLI code* — this document.
- *New features have a clear place to live* — the "Where new code goes"
  section above.
- *Future interfaces do not require rewriting CLI business logic* — the
  dependency rules guarantee every use case is a plain Python call; the
  follow-up issues remove the three places where that is not yet true.
