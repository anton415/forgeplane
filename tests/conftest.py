# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Shared pytest fixtures backed by the spec section parser."""

from pathlib import Path
from typing import Callable, Dict, Optional

import pytest

from forgeplane.specs.scanner import parse_sections, parse_spec_file


# Repository root is two levels above this file (tests/conftest.py).
REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

# Function shape used by the inline-parse fixture.
SectionParser = Callable[[str], Dict[str, Optional[str]]]


@pytest.fixture(scope="session")
def examples_dir() -> Path:
    """Absolute path to the bundled example specs."""
    return EXAMPLES_DIR


@pytest.fixture(scope="session")
def good_spec_sections() -> Dict[str, Optional[str]]:
    """Parsed sections from a fully populated spec."""
    return parse_spec_file(EXAMPLES_DIR / "good_spec.md")


@pytest.fixture(scope="session")
def weak_spec_sections() -> Dict[str, Optional[str]]:
    """Parsed sections from a partially populated spec."""
    return parse_spec_file(EXAMPLES_DIR / "weak_spec.md")


@pytest.fixture()
def parse() -> SectionParser:
    """Shortcut for parsing inline Markdown strings inside tests."""
    return parse_sections
