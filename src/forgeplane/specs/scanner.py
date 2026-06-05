"""Typed scanner for API documentation directories."""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, TypedDict

from forgeplane.specs.files import FileEntry, collect_files


class ScanReport(TypedDict):
    """Report structure returned by scan_docs."""

    # Keep this shape JSON/YAML-friendly because CLI output serializes it
    # directly without additional model conversion.
    path: str
    files_count: int
    total_size_bytes: int
    extensions: dict[str, int]
    files: List[str]


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """Aggregated scan metadata before serialization."""

    # The summary keeps Path and FileEntry objects while scanning is still in
    # Python space; to_report converts them to plain serializable values.
    path: Path
    files: List[FileEntry]
    total_size_bytes: int
    extensions: dict[str, int]

    def to_report(self) -> ScanReport:
        return {
            "path": str(self.path),
            "files_count": len(self.files),
            "total_size_bytes": self.total_size_bytes,
            "extensions": self.extensions,
            "files": [file.relative_path for file in self.files],
        }


def extension_label(extension: Optional[str]) -> str:
    # Files without suffixes are grouped under a stable report key.
    if extension is None:
        return "no_extension"
    return extension


def build_scan_summary(path: Path, files: Optional[List[FileEntry]] = None) -> ScanSummary:
    # Passing files is useful for tests or future scanners that already have
    # collected FileEntry objects; otherwise the directory is scanned here.
    file_entries = collect_files(path) if files is None else files
    extensions: dict[str, int] = {}
    total_size_bytes = 0

    # Aggregate only data needed by the public report and leave per-file details
    # in the FileEntry list.
    for file in file_entries:
        label = extension_label(file.extension)
        extensions[label] = extensions.get(label, 0) + 1
        total_size_bytes += file.size_bytes

    return ScanSummary(
        path=path,
        files=file_entries,
        total_size_bytes=total_size_bytes,
        extensions=extensions,
    )


def scan_docs(path: Path) -> ScanReport:
    return build_scan_summary(path).to_report()
