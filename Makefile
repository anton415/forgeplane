# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Anton Serdyuchenko

.PHONY: lint format check-sdist

# Keep local quality gates in the same order reviewers should run them.
lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy src/

# Apply only Ruff's deterministic formatting pass; lint fixes stay explicit.
format:
	uv run ruff format .

# Release gate (issue #76): rebuild dist/ from scratch, then verify the sdist
# contains no local-only paths (.claude/, .env, virtualenvs, caches, coverage,
# worktrees). Run before publishing any release artifact.
check-sdist:
	rm -rf dist
	uv build
	scripts/check_sdist.sh
