# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Markdown discovery and read helpers."""

from pathlib import Path

from forgeplane.core.files import find_markdown_files, read_markdown


def test_empty_dir_returns_no_files(tmp_path: Path) -> None:
    assert find_markdown_files(tmp_path) == []


def test_nested_dirs_are_walked(tmp_path: Path) -> None:
    top = tmp_path / "top.md"
    nested = tmp_path / "a" / "b" / "nested.md"
    nested.parent.mkdir(parents=True)
    top.write_text("# top", encoding="utf-8")
    nested.write_text("# nested", encoding="utf-8")

    # Sorted by POSIX path: ``a/b/nested.md`` precedes ``top.md``.
    assert find_markdown_files(tmp_path) == [nested, top]


def test_non_markdown_files_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("md", encoding="utf-8")
    (tmp_path / "other.txt").write_text("txt", encoding="utf-8")
    (tmp_path / "data.json").write_text("{}", encoding="utf-8")

    assert find_markdown_files(tmp_path) == [tmp_path / "note.md"]


def test_directory_named_like_markdown_is_excluded(tmp_path: Path) -> None:
    (tmp_path / "looks_like.md").mkdir()
    real = tmp_path / "real.md"
    real.write_text("# real", encoding="utf-8")

    assert find_markdown_files(tmp_path) == [real]


def test_read_markdown_returns_utf8_content(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("# заголовок\ncontent", encoding="utf-8")

    assert read_markdown(path) == "# заголовок\ncontent"
