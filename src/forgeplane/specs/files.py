# Copyright 2026 Anton Serdyuchenko
# SPDX-License-Identifier: Apache-2.0

"""Typed filesystem helpers for documentation scans."""

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict

from forgeplane.core.config import get_logger

# Module-level logger so skipped-symlink warnings surface through the same
# rich handler as the rest of the scan pipeline.
_logger = get_logger(__name__)

# Default resource safeguards for a documentation scan. The values are
# deliberately generous for real docs trees (a Markdown spec is rarely larger
# than a few hundred kilobytes) while still bounding the work an untrusted
# third-party repository can force onto a local machine or a CI runner.
DEFAULT_MAX_FILE_SIZE_BYTES: Final[int] = 10 * 1024 * 1024  # 10 MiB per file
DEFAULT_MAX_FILE_COUNT: Final[int] = 10_000
DEFAULT_MAX_TOTAL_BYTES: Final[int] = 200 * 1024 * 1024  # 200 MiB per scan


class ScanError(Exception):
    """Base error for documentation-scan failures that must fail closed.

    Callers (notably the CLI) catch this single type to convert any scan
    failure — policy violations and unreadable files alike — into one clear
    non-zero exit instead of a traceback mid-report.
    """


class ScanPolicyError(ScanError):
    """Raised when a scan target violates the resource policy limits."""


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    """Resource limits applied while collecting and reading scanned files.

    Symlink handling is not configurable: the scanner never follows symlinks,
    so a docs tree cannot point a ``.md`` path outside the selected root.
    """

    # Hard ceiling for a single file; enforced again at read time so a file
    # that grows between stat and read still fails closed.
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES
    # Maximum number of files the scan will index before failing closed.
    max_file_count: int = DEFAULT_MAX_FILE_COUNT
    # Maximum cumulative size of all indexed files.
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES


# Shared default so call sites do not rebuild an identical frozen instance.
DEFAULT_SCAN_POLICY: Final[ScanPolicy] = ScanPolicy()


class FileRecord(TypedDict):
    """Serializable metadata for one scanned file."""

    # This record uses only plain values so it can be emitted as JSON/YAML.
    path: str
    extension: str | None
    size_bytes: int


@dataclass(frozen=True, slots=True)
class FileEntry:
    """Filesystem metadata for one scanned file."""

    # Keep the original Path for internal use and a relative string for reports.
    path: Path
    relative_path: str
    extension: str | None
    size_bytes: int

    def to_record(self) -> FileRecord:
        # Convert the internal dataclass to a stable report-friendly shape.
        return {
            "path": self.relative_path,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
        }


def file_extension(path: Path) -> str | None:
    suffix = path.suffix.lower()
    # Returning None lets scanner.py decide how extensionless files are labeled.
    if suffix == "":
        return None
    return suffix


def path_sort_key(path: Path) -> str:
    # Sorting by POSIX-style text keeps scan output deterministic across runs.
    return path.as_posix()


def build_file_entry(root: Path, path: Path, *, size_bytes: int) -> FileEntry:
    # The caller supplies the size from the lstat it already performed, so the
    # entry never re-stats the path (and never follows a symlink to do so).
    return FileEntry(
        path=path,
        relative_path=str(path.relative_to(root)),
        extension=file_extension(path),
        size_bytes=size_bytes,
    )


def collect_files(
    path: Path, policy: ScanPolicy = DEFAULT_SCAN_POLICY
) -> list[FileEntry]:
    """Index regular files under ``path`` subject to the scan policy.

    Symlink policy: symlinked files are skipped (with a warning), and
    ``rglob`` never descends into symlinked directories, so every returned
    entry physically lives under the scan root. Resource policy: the per-file
    size, file count, and total byte limits raise :class:`ScanPolicyError` as
    soon as they are exceeded, before any file content is read.
    """
    entries: list[FileEntry] = []
    total_size_bytes = 0

    for item in sorted(path.rglob("*"), key=path_sort_key):
        try:
            # lstat describes the path itself without following a symlink, so
            # one syscall covers the type check and the size read with no
            # follow-the-link race between them.
            item_stat = item.lstat()
        except OSError as exc:
            # A path that vanished or cannot be inspected makes the report
            # unreliable; fail closed instead of silently dropping it.
            raise ScanError(f"Cannot stat {item}: {exc.strerror or exc}") from exc
        if stat.S_ISLNK(item_stat.st_mode):
            # Never follow symlinks: a docs tree must not be able to point a
            # scanned path outside the selected root.
            _logger.warning(
                "Skipping symlink %s: the scanner never follows symlinks", item
            )
            continue
        if not stat.S_ISREG(item_stat.st_mode):
            # Directories and special files (sockets, FIFOs) carry no content
            # for the report.
            continue
        if item_stat.st_size > policy.max_file_size_bytes:
            raise ScanPolicyError(
                f"{item} is {item_stat.st_size} bytes, which exceeds the "
                f"per-file limit of {policy.max_file_size_bytes} bytes"
            )
        if len(entries) >= policy.max_file_count:
            raise ScanPolicyError(
                f"{path} contains more than {policy.max_file_count} files, "
                "which exceeds the file-count limit"
            )
        total_size_bytes += item_stat.st_size
        if total_size_bytes > policy.max_total_bytes:
            raise ScanPolicyError(
                f"Files under {path} exceed the total-size limit of "
                f"{policy.max_total_bytes} bytes"
            )
        entries.append(build_file_entry(path, item, size_bytes=item_stat.st_size))

    return entries
