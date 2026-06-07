# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Markdown discovery and read helpers for spec scans."""

from pathlib import Path
from typing import List


def find_markdown_files(root: Path) -> List[Path]:
    """Return every ``.md`` file under ``root``, sorted for deterministic output."""
    # is_file() drops directories named ``*.md`` and broken symlinks so callers
    # can safely read every returned path without an extra guard.
    # Sorting by POSIX text keeps scan order stable across platforms.
    return sorted(
        (path for path in root.rglob("*.md") if path.is_file()),
        key=lambda path: path.as_posix(),
    )


def read_markdown(path: Path) -> str:
    """Read a Markdown file as UTF-8 text."""
    return path.read_text(encoding="utf-8")
