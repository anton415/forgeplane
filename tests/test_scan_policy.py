# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the scan symlink policy and resource-exhaustion safeguards."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forgeplane.cli import app
from forgeplane.core.files import find_markdown_files
from forgeplane.specs.files import (
    ScanError,
    ScanPolicy,
    ScanPolicyError,
    collect_files,
)
from forgeplane.specs.scanner import SpecReadError, read_spec_text, score_spec_file


def _write_spec(path: Path, body: str = "## Goal\nShip it.\n") -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _plain_stderr(stderr: str) -> str:
    # Typer renders BadParameter inside a rich panel that wraps long messages
    # across bordered lines; strip the borders and collapse whitespace so
    # substring assertions hold regardless of where the wrap lands.
    return " ".join(stderr.replace("│", " ").split())


# ---------------------------------------------------------------------------
# Symlink policy: the scanner never follows symlinks.
# ---------------------------------------------------------------------------


def test_symlinked_file_outside_root_is_skipped(tmp_path: Path) -> None:
    # A docs tree must not be able to point a scanned ``.md`` path outside
    # the selected scan root — the core escape from issue #80.
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _write_spec(outside / "secret.md")
    root = tmp_path / "docs"
    root.mkdir()
    _write_spec(root / "real.md")
    (root / "leak.md").symlink_to(secret)

    entries = collect_files(root)
    assert [entry.relative_path for entry in entries] == ["real.md"]


def test_symlinked_file_inside_root_is_skipped(tmp_path: Path) -> None:
    # The policy skips every symlink, even one resolving inside the root, so
    # behaviour does not depend on where the target happens to live.
    real = _write_spec(tmp_path / "real.md")
    (tmp_path / "alias.md").symlink_to(real)

    entries = collect_files(tmp_path)
    assert [entry.relative_path for entry in entries] == ["real.md"]


def test_symlinked_directory_is_not_traversed(tmp_path: Path) -> None:
    # rglob must not descend through a directory symlink into a tree that
    # physically lives outside the scan root.
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_spec(outside / "secret.md")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    assert collect_files(root) == []


def test_find_markdown_files_skips_symlinks(tmp_path: Path) -> None:
    # The discovery helper applies the same symlink policy as collect_files.
    real = _write_spec(tmp_path / "real.md")
    (tmp_path / "alias.md").symlink_to(real)

    assert find_markdown_files(tmp_path) == [real]


def test_broken_symlink_is_skipped_not_fatal(tmp_path: Path) -> None:
    # A dangling symlink is skipped by the symlink policy before any stat
    # of its target could fail the scan.
    (tmp_path / "dangling.md").symlink_to(tmp_path / "missing.md")

    assert collect_files(tmp_path) == []


# ---------------------------------------------------------------------------
# Resource limits: file size, file count, total bytes.
# ---------------------------------------------------------------------------


def test_oversized_file_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("x" * 64, encoding="utf-8")
    policy = ScanPolicy(max_file_size_bytes=10)

    with pytest.raises(ScanPolicyError, match="per-file limit of 10 bytes"):
        collect_files(tmp_path, policy)


def test_file_count_over_limit_fails_closed(tmp_path: Path) -> None:
    # A high file-count directory must abort before indexing the whole tree.
    for index in range(3):
        _write_spec(tmp_path / f"spec_{index}.md")
    policy = ScanPolicy(max_file_count=2)

    with pytest.raises(ScanPolicyError, match="more than 2 files"):
        collect_files(tmp_path, policy)


def test_total_bytes_over_limit_fails_closed(tmp_path: Path) -> None:
    for index in range(2):
        (tmp_path / f"part_{index}.md").write_text("x" * 30, encoding="utf-8")
    policy = ScanPolicy(max_total_bytes=40)

    with pytest.raises(ScanPolicyError, match="total-size limit of 40 bytes"):
        collect_files(tmp_path, policy)


def test_default_policy_accepts_normal_tree(tmp_path: Path) -> None:
    # Sanity check: the defaults stay invisible for an ordinary docs tree
    # with nested directories and extensionless files.
    nested = tmp_path / "guides"
    nested.mkdir()
    _write_spec(nested / "spec.md")
    (tmp_path / "LICENSE").write_text("text\n", encoding="utf-8")

    entries = collect_files(tmp_path)
    assert [entry.relative_path for entry in entries] == ["LICENSE", "guides/spec.md"]
    # The serialisable record mirrors the entry metadata, with the missing
    # suffix collapsing to None.
    assert entries[0].to_record() == {
        "path": "LICENSE",
        "extension": None,
        "size_bytes": 5,
    }


def test_vanished_file_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A path that disappears between rglob and lstat makes the report
    # unreliable; the scan reports it instead of silently dropping it.
    _write_spec(tmp_path / "spec.md")

    def raise_enoent(self: Path) -> os.stat_result:
        raise FileNotFoundError(2, "No such file or directory", str(self))

    monkeypatch.setattr(Path, "lstat", raise_enoent)
    with pytest.raises(ScanError, match="Cannot stat"):
        collect_files(tmp_path)


# ---------------------------------------------------------------------------
# Fail-closed spec reads: unreadable, non-UTF-8, oversized at read time.
# ---------------------------------------------------------------------------


def test_non_utf8_spec_fails_closed_without_leaking_bytes(tmp_path: Path) -> None:
    spec = tmp_path / "binary.md"
    spec.write_bytes(b"## Goal\n\xff\xfe binary tail")

    with pytest.raises(SpecReadError, match="not valid UTF-8") as excinfo:
        read_spec_text(spec)
    # The message carries the byte offset only, never the raw bytes.
    message = str(excinfo.value)
    assert "offset 8" in message
    assert "\\xff" not in message and "0xff" not in message


def test_oversized_spec_fails_at_read_time(tmp_path: Path) -> None:
    # The size limit is re-checked at read time, so a file that grew after
    # the stat pass still fails closed instead of being read whole.
    spec = _write_spec(tmp_path / "grown.md", "x" * 32)

    with pytest.raises(SpecReadError, match="per-file limit of 8 bytes"):
        read_spec_text(spec, max_size_bytes=8)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_spec_fails_closed(tmp_path: Path) -> None:
    spec = _write_spec(tmp_path / "locked.md")
    spec.chmod(0o000)
    try:
        with pytest.raises(SpecReadError, match="Cannot read spec file"):
            read_spec_text(spec)
    finally:
        # Restore permissions so pytest can clean up the tmp directory.
        spec.chmod(0o644)


def test_score_spec_file_honours_policy_limit(tmp_path: Path) -> None:
    # The scoring entry point threads the policy size limit into the read.
    spec = _write_spec(tmp_path / "spec.md", "## Goal\n" + "x" * 64)
    entries = collect_files(tmp_path)
    policy = ScanPolicy(max_file_size_bytes=16)

    with pytest.raises(SpecReadError, match="per-file limit of 16 bytes"):
        score_spec_file(entries[0], policy=policy)
    assert spec.exists()


# ---------------------------------------------------------------------------
# CLI integration: limits are configurable and errors stay clean.
# ---------------------------------------------------------------------------


def test_scan_cli_excludes_symlinks_from_report(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _write_spec(outside / "secret.md")
    root = tmp_path / "docs"
    root.mkdir()
    _write_spec(root / "real.md")
    (root / "leak.md").symlink_to(secret)

    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(root), "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["files"] == ["real.md"]
    assert [record["file"] for record in payload["results"]] == ["real.md"]


def test_scan_cli_max_files_fails_with_clear_error(tmp_path: Path) -> None:
    # High file-count directories abort with a clean usage error, not a
    # traceback and not a partial report on stdout.
    for index in range(3):
        _write_spec(tmp_path / f"spec_{index}.md")

    runner = CliRunner()
    result = runner.invoke(
        app, ["scan", str(tmp_path), "--format", "json", "--max-files", "2"]
    )
    assert result.exit_code != 0
    assert "more than 2 files" in _plain_stderr(result.stderr)
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_scan_cli_max_file_size_fails_with_clear_error(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("x" * 100, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path), "--max-file-size", "10"])
    assert result.exit_code != 0
    assert "per-file limit of 10 bytes" in _plain_stderr(result.stderr)
    assert "Traceback" not in result.stderr


def test_scan_cli_max_total_bytes_fails_with_clear_error(tmp_path: Path) -> None:
    for index in range(2):
        (tmp_path / f"part_{index}.md").write_text("x" * 30, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path), "--max-total-bytes", "40"])
    assert result.exit_code != 0
    assert "total-size limit of 40 bytes" in _plain_stderr(result.stderr)
    assert "Traceback" not in result.stderr


def test_scan_cli_non_utf8_spec_fails_cleanly(tmp_path: Path) -> None:
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe not text")

    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path)])
    assert result.exit_code != 0
    assert "not valid UTF-8" in _plain_stderr(result.stderr)
    assert "Traceback" not in result.stderr
