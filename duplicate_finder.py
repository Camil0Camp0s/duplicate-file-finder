"""Facade over the filesystem + hashlib for duplicate-file detection.

The GUI (app.py) only calls scan_for_duplicates() and never touches
os.walk/hashlib directly — this module is the single place that knows how
files are read and hashed.

Detection runs in three passes so the expensive part (reading full file
content) only happens for files that already look like real candidates:

1. Group all files by size (dict lookup, O(n)). A file whose size is
   unique among the whole scan cannot have a duplicate, so it is dropped
   immediately without ever being opened.
2. Within each size group, group by a partial hash of the first 4 KB
   (constant-size read per file, regardless of file size). This filters
   out most same-size-but-different-content files cheaply.
3. Only files that still collide after steps 1-2 get a full SHA-256 hash
   of their entire content. Files sharing (size, partial_hash, full_hash)
   are byte-identical duplicates.

This avoids the naive O(n^2) approach of comparing every file against
every other file, and avoids fully reading large files (videos, archives)
unless they are genuine duplicate candidates.
"""
from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

PARTIAL_HASH_BYTES = 4096
HASH_CHUNK_SIZE = 1024 * 1024  # 1 MB, streamed so large files never fully load into memory


@dataclass
class FileInfo:
    """One file on disk, as tracked by a duplicate group."""

    path: Path
    size: int
    modified: float


@dataclass
class DuplicateGroup:
    """A set of files that are byte-identical to each other."""

    files: list[FileInfo]

    @property
    def size(self) -> int:
        return self.files[0].size

    @property
    def wasted_bytes(self) -> int:
        """Space that could be freed by keeping only one copy."""
        return self.size * (len(self.files) - 1)


def _iter_files(root: Path) -> Iterable[Path]:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            yield Path(dirpath) / name


def _partial_hash(path: Path) -> str:
    with open(path, "rb") as f:
        chunk = f.read(PARTIAL_HASH_BYTES)
    return hashlib.sha256(chunk).hexdigest()


def _full_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_for_duplicates(
    root: Path,
    progress_callback: Callable[[str], None] | None = None,
) -> list[DuplicateGroup]:
    """Scan `root` recursively and return groups of byte-identical files.

    `progress_callback(message)` is called with short status updates so a
    caller (e.g. a GUI running this in a background thread) can display
    progress without this module knowing anything about the UI.
    """

    def report(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    report("Listing files...")
    by_size: dict[int, list[Path]] = defaultdict(list)
    total_files = 0
    for path in _iter_files(root):
        try:
            size = path.stat().st_size
        except OSError:
            continue  # broken symlink, permission error, etc. — skip, don't crash the scan
        if size == 0:
            continue  # empty files are trivially "identical" but not useful duplicates to report
        by_size[size].append(path)
        total_files += 1

    candidates = [paths for paths in by_size.values() if len(paths) > 1]
    report(f"{total_files} files found, {sum(len(p) for p in candidates)} share a size with another file")

    by_partial: dict[tuple[int, str], list[Path]] = defaultdict(list)
    for paths in candidates:
        size = paths[0].stat().st_size
        for path in paths:
            try:
                key = (size, _partial_hash(path))
            except OSError:
                continue
            by_partial[key].append(path)

    partial_candidates = [paths for paths in by_partial.values() if len(paths) > 1]

    groups: list[DuplicateGroup] = []
    by_full: dict[tuple[int, str], list[FileInfo]] = defaultdict(list)
    checked = 0
    for paths in partial_candidates:
        size = paths[0].stat().st_size
        for path in paths:
            checked += 1
            report(f"Hashing file {checked}...")
            try:
                stat = path.stat()
                full = _full_hash(path)
            except OSError:
                continue
            by_full[(size, full)].append(FileInfo(path=path, size=stat.st_size, modified=stat.st_mtime))

    for infos in by_full.values():
        if len(infos) > 1:
            infos.sort(key=lambda info: info.modified)  # oldest first — treated as the "original" to keep
            groups.append(DuplicateGroup(files=infos))

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    report(f"Done — {len(groups)} duplicate group(s) found")
    return groups
