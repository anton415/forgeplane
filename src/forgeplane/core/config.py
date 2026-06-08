# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Environment-driven configuration and logging setup for Forgeplane.

This module loads runtime settings from a ``.env`` file (and, with lower
precedence, the process environment) and wires the Python ``logging`` module
to a ``rich`` handler. CLI commands call :func:`configure_logging` once on
startup so every Forgeplane subpackage can emit log records through
:func:`get_logger` without duplicating handler setup.
"""

import logging
import os
from dataclasses import dataclass
from typing import Final

from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.logging import RichHandler

# Namespace for every Forgeplane logger. Child loggers are derived via
# ``logging.getLogger(LOGGER_NAME).getChild(...)`` so they all share the
# same handler configuration applied in :func:`configure_logging`.
LOGGER_NAME: Final[str] = "forgeplane"

# Fallback log level used when LOG_LEVEL is missing or unrecognised; chosen
# to match the standard library default so unconfigured environments behave
# predictably.
DEFAULT_LOG_LEVEL: Final[str] = "INFO"

# Accepted values for LOG_LEVEL. Matching the stdlib level names lets us pass
# the string directly to ``Logger.setLevel`` without an extra translation step.
_VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved runtime settings loaded from the environment."""

    # OPENAI_API_KEY is optional at this stage because no LLM-backed command
    # is wired in yet; downstream code can require it explicitly when needed.
    openai_api_key: str | None
    # Normalised log level name (one of the stdlib threshold names).
    log_level: str


def _normalize_log_level(raw: str | None) -> str:
    # Silently fall back to the default so an unrecognised value cannot crash
    # the CLI before logging is configured.
    if raw is None:
        return DEFAULT_LOG_LEVEL
    candidate = raw.strip().upper()
    if candidate in _VALID_LOG_LEVELS:
        return candidate
    return DEFAULT_LOG_LEVEL


def load_settings() -> Settings:
    """Load environment variables from ``.env`` and return parsed settings."""
    # ``find_dotenv(usecwd=True)`` anchors the search at the user's current
    # working directory rather than the package install location, so a ``.env``
    # placed next to where ``forgeplane`` was invoked is picked up even when
    # Forgeplane is installed into site-packages.
    # ``load_dotenv`` is a no-op when no file is found, which keeps CI safe
    # when configuration arrives through real environment variables.
    # ``override=False`` (the default) preserves values already set in the
    # process environment.
    load_dotenv(find_dotenv(usecwd=True))
    return Settings(
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        log_level=_normalize_log_level(os.environ.get("LOG_LEVEL")),
    )


def _build_rich_handler(level: str) -> RichHandler:
    # Route log records to stderr so machine-readable command output
    # (for example ``scan --format json``) on stdout stays parseable when
    # logging runs at INFO or above.
    handler = RichHandler(
        level=level,
        console=Console(stderr=True),
        rich_tracebacks=True,
        show_time=True,
        show_path=False,
        markup=True,
    )
    # RichHandler already renders the level and timestamp; keep the formatter
    # minimal so the message is not prefixed twice.
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def configure_logging(
    level: str | None = None, *, verbose: bool = False
) -> logging.Logger:
    """Configure the Forgeplane root logger with a Rich handler.

    Resolution order for the effective level:

    1. ``verbose=True`` forces ``DEBUG`` so ``--verbose`` always wins;
    2. an explicit ``level`` argument (mainly for tests or library callers);
    3. the value parsed from ``LOG_LEVEL`` via :func:`load_settings`.
    """
    settings = load_settings()
    effective_level = "DEBUG" if verbose else (level or settings.log_level)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(effective_level)
    # Replace existing handlers so repeated CLI invocations within one process
    # (notably in tests) do not duplicate log records.
    logger.handlers = [_build_rich_handler(effective_level)]
    # Forgeplane owns its own formatting through RichHandler; propagating to
    # the root logger would cause duplicate output when the host application
    # configures logging on its own.
    logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger under the Forgeplane namespace.

    Pass ``__name__`` from the calling module to keep log records traceable
    back to their origin without each module re-declaring the namespace.
    """
    root = logging.getLogger(LOGGER_NAME)
    if name is None or name == LOGGER_NAME:
        return root
    # Strip the Forgeplane prefix if a caller passes ``__name__`` so the child
    # logger name stays compact (``forgeplane.specs.scanner`` rather than
    # ``forgeplane.forgeplane.specs.scanner``).
    suffix = name.removeprefix(f"{LOGGER_NAME}.")
    return root.getChild(suffix)
