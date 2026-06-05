"""Typed filesystem helpers for documentation scans."""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, TypedDict


class FileRecord(TypedDict):
    """Serializable metadata for one scanned file."""

    # This record uses only plain values so it can be emitted as JSON/YAML.
    path: str
    extension: Optional[str]
    size_bytes: int


@dataclass(frozen=True, slots=True)
class FileEntry:
    """Filesystem metadata for one scanned file."""

    # Keep the original Path for internal use and a relative string for reports.
    path: Path
    relative_path: str
    extension: Optional[str]
    size_bytes: int

    def to_record(self) -> FileRecord:
        # Convert the internal dataclass to a stable report-friendly shape.
        return {
            "path": self.relative_path,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
        }


def file_extension(path: Path) -> Optional[str]:
    suffix = path.suffix.lower()
    # Returning None lets scanner.py decide how extensionless files are labeled.
    if suffix == "":
        return None
    return suffix


def path_sort_key(path: Path) -> str:
    # Sorting by POSIX-style text keeps scan output deterministic across runs.
    return path.as_posix()


def build_file_entry(root: Path, path: Path) -> FileEntry:
    # FileEntry centralizes all per-file filesystem reads in one place.
    return FileEntry(
        path=path,
        relative_path=str(path.relative_to(root)),
        extension=file_extension(path),
        size_bytes=path.stat().st_size,
    )


def collect_files(path: Path) -> List[FileEntry]:
    entries: List[FileEntry] = []

    # rglob walks the full tree; directories are skipped so reports only include
    # actual files.
    for item in sorted(path.rglob("*"), key=path_sort_key):
        if item.is_file():
            entries.append(build_file_entry(path, item))

    return entries
