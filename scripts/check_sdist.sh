#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Anton Serdyuchenko
#
# Release gate for source distributions (issue #76). Lists every entry in
# dist/*.tar.gz with `tar -tzf` and fails if any local-only path leaked into
# the archive: .claude/ tool worktrees, .env files, virtualenvs, caches,
# coverage data, scan reports, or build leftovers. The deny list mirrors the
# sdist excludes in pyproject.toml so a drift between the two is caught here.
# Run via `make check-sdist`, which rebuilds dist/ first.

set -euo pipefail

# Resolve the repository root from this script's location so the check works
# from any working directory (local shells, CI steps, release runbooks).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${REPO_ROOT}/dist"

# Extended regex of forbidden path fragments. Every sdist entry is prefixed
# with "forgeplane-<version>/", so each pattern anchors on a "/" separator:
#   /.claude/        local AI tool worktrees and session metadata
#   /.env and .env.* real environment files (".env.example" is the committed
#                    template and is exempted below)
#   /.venv/          virtual environments
#   caches           __pycache__, pytest, mypy, ruff
#   coverage         .coverage, coverage.xml, htmlcov/
#   build leftovers  dist/, build/, wheels/, *.egg-info
#   /worktrees/      any stray git worktree checkout
#   /reports/        scan reports written by `forgeplane scan --output-dir`
DENY_PATTERN='/\.claude/|/\.env$|/\.env\.[^/]+$|/\.venv/|/__pycache__/|/\.pytest_cache/|/\.mypy_cache/|/\.ruff_cache/|/\.coverage$|/coverage\.xml$|/htmlcov/|/dist/|/build/|/wheels/|\.egg-info|/worktrees/|/reports/'

# Fail loudly when there is nothing to check: a release gate that silently
# passes on an empty dist/ would defeat its purpose.
shopt -s nullglob
sdists=("${DIST_DIR}"/*.tar.gz)
if [ "${#sdists[@]}" -eq 0 ]; then
    echo "error: no sdist found in ${DIST_DIR}; run 'uv build' first" >&2
    exit 1
fi

status=0
for sdist in "${sdists[@]}"; do
    # grep exits 1 on "no match", which is the good case here, so the || true
    # keeps set -e from aborting before we can report a clean result.
    matches="$(tar -tzf "${sdist}" | grep -E "${DENY_PATTERN}" | grep -v '/\.env\.example$' || true)"
    if [ -n "${matches}" ]; then
        echo "error: forbidden paths found in ${sdist}:" >&2
        printf '%s\n' "${matches}" >&2
        status=1
    else
        echo "ok: ${sdist} contains no local-only paths"
    fi
done

exit "${status}"
