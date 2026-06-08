# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for environment loading and logger configuration."""

import json
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
    handler = logger.handlers[0]
    assert isinstance(handler, RichHandler)
    # Records must land on stderr so machine-readable command output on stdout
    # (json/yaml report formats) stays parseable.
    assert handler.console.stderr is True
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
    assert result.exit_code == 0, result.stderr
    # Logs are routed to stderr to keep stdout machine-parseable; the info
    # line from cli.py and the per-file debug line from the scanner must
    # both be present there under --verbose.
    assert "Scanning directory" in result.stderr
    assert "Indexed intro.md" in result.stderr


def test_scan_json_output_is_pure_stdout(
    tmp_path: Path,
    reset_forgeplane_logger: None,
) -> None:
    # Regression guard for the P1 finding: log records on stdout would render
    # ``scan --format json`` unparseable for downstream tools.
    (tmp_path / "intro.md").write_text("# intro", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["files_count"] == 1
    # The default INFO log line must not contaminate stdout.
    assert "Scanning directory" not in result.stdout


def test_load_settings_finds_dotenv_in_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Regression guard for the P2 finding: python-dotenv's default search
    # starts at the calling module, which after installation lives in
    # site-packages. The loader must anchor on the user's cwd instead.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=from-cwd\nLOG_LEVEL=warning\n",
        encoding="utf-8",
    )

    settings = load_settings()
    assert settings.openai_api_key == "from-cwd"
    assert settings.log_level == "WARNING"
