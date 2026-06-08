# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for environment loading and logger configuration."""

import logging
from pathlib import Path

import pytest
from rich.logging import RichHandler
from typer.testing import CliRunner

from forgeplane.cli import app
from forgeplane.core.config import (
    DEFAULT_LOG_LEVEL,
    LOGGER_NAME,
    Settings,
    _normalize_log_level,
    configure_logging,
    get_logger,
    load_settings,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Drop variables that would otherwise leak from the developer environment
    # and run inside an empty cwd so python-dotenv finds no real .env file.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture()
def reset_forgeplane_logger() -> None:
    # configure_logging mutates module-level handler state, so each test starts
    # from a clean slate to avoid leaking handlers across tests.
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers = []
    logger.setLevel(logging.NOTSET)


def test_load_settings_defaults_when_env_missing() -> None:
    settings = load_settings()
    assert settings == Settings(openai_api_key=None, log_level=DEFAULT_LOG_LEVEL)


def test_load_settings_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    settings = load_settings()
    assert settings.openai_api_key == "sk-test"
    # Stdlib level names are upper-case; the normalizer must canonicalize.
    assert settings.log_level == "DEBUG"


def test_load_settings_treats_empty_api_key_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty OPENAI_API_KEY entry (a common .env placeholder) should not be
    # surfaced as a usable key to downstream consumers.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    settings = load_settings()
    assert settings.openai_api_key is None


def test_load_settings_falls_back_on_bad_log_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "loud")
    settings = load_settings()
    assert settings.log_level == DEFAULT_LOG_LEVEL


def test_normalize_log_level_accepts_known_values() -> None:
    assert _normalize_log_level("warning") == "WARNING"
    assert _normalize_log_level(" Error ") == "ERROR"
    assert _normalize_log_level(None) == DEFAULT_LOG_LEVEL
    assert _normalize_log_level("nonsense") == DEFAULT_LOG_LEVEL


def test_configure_logging_uses_rich_handler(
    reset_forgeplane_logger: None,
) -> None:
    logger = configure_logging()
    assert logger.name == LOGGER_NAME
    assert logger.level == logging.getLevelName(DEFAULT_LOG_LEVEL)
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], RichHandler)
    # Propagation must be disabled so host applications do not double-log.
    assert logger.propagate is False


def test_configure_logging_verbose_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
    reset_forgeplane_logger: None,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    logger = configure_logging(verbose=True)
    assert logger.level == logging.DEBUG


def test_configure_logging_explicit_level_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
    reset_forgeplane_logger: None,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    logger = configure_logging(level="WARNING")
    assert logger.level == logging.WARNING


def test_configure_logging_replaces_existing_handlers(
    reset_forgeplane_logger: None,
) -> None:
    # Two back-to-back configure calls must not stack handlers.
    configure_logging()
    logger = configure_logging()
    assert len(logger.handlers) == 1


def test_get_logger_returns_namespaced_child() -> None:
    child = get_logger("forgeplane.specs.scanner")
    assert child.name == "forgeplane.specs.scanner"
    # An unprefixed name still lands under the Forgeplane namespace.
    other = get_logger("custom")
    assert other.name == "forgeplane.custom"
    # ``None`` and the root namespace return the root logger itself.
    assert get_logger(None) is get_logger(LOGGER_NAME)


def test_scan_verbose_emits_debug_logs(
    tmp_path: Path,
    reset_forgeplane_logger: None,
) -> None:
    # Create a small docs tree so the scanner has something to log about.
    (tmp_path / "intro.md").write_text("# intro", encoding="utf-8")
    (tmp_path / "api.md").write_text("# api", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path), "--verbose"])
    assert result.exit_code == 0, result.output
    # The info line from cli.py and the per-file debug line from the scanner
    # must both be present under --verbose.
    assert "Scanning directory" in result.output
    assert "Indexed intro.md" in result.output
