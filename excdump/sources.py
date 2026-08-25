"""Captured source: the line windows in a dump, and the per-path sidecar.

Source is stored once per file rather than once per frame, and the full text of
each file is written beside the dumps under its content hash. The inspector
reads that instead of the file on disk, so line numbers still line up after the
code has moved on.
"""

import gzip
import hashlib
import inspect
import json
import linecache
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG
from .paths import SOURCE_DIR, SOURCE_SUFFIX, relative_path


# -- source storage ----------------------------------------------------------


class SourceFile:
    """Captured source of one file, stored once and shared by every frame.

    Only the line windows that some frame actually needs are kept, merged into
    non-overlapping segments so nearby frames in the same file share text.
    """

    __slots__ = ("path", "segments")

    def __init__(self, path: str):
        self.path = path
        self.segments: List[List[Any]] = []  # [[start_lineno, [line, ...]], ...]

    def add(self, start: int, lines: List[str]) -> None:
        if not lines:
            return
        end = start + len(lines)
        kept: List[List[Any]] = []
        for seg_start, seg_lines in self.segments:
            seg_end = seg_start + len(seg_lines)
            if seg_end < start or seg_start > end:
                kept.append([seg_start, seg_lines])
                continue
            # Overlapping or adjacent: fuse this segment into the pending one.
            if seg_start < start:
                lines = seg_lines[: start - seg_start] + lines
                start = seg_start
            if seg_end > end:
                lines = lines + seg_lines[len(seg_lines) - (seg_end - end) :]
                end = seg_end
        kept.append([start, lines])
        kept.sort(key=lambda segment: segment[0])
        self.segments = kept

    def window(self, lineno: int, radius: int) -> Tuple[int, List[str]]:
        """``(start_lineno, lines)`` around ``lineno``, or ``(0, [])``."""
        for start, lines in self.segments:
            if start <= lineno < start + len(lines):
                low = max(start, lineno - radius)
                high = min(start + len(lines), lineno + radius + 1)
                return low, lines[low - start : high - start]
        return 0, []


class SourceStore:
    """All source captured by a dump, keyed by path relative to :attr:`root`.

    Frames only store their file id, so a stack of ten frames in one module
    holds one copy of that module's source instead of ten overlapping windows.
    Relative ids also let a dump be inspected from a checkout of the same tree
    on another machine.

    Two copies of the source are kept, for two different jobs. The merged line
    windows in :attr:`files` ride inside the dump, so a ``.dump`` carried off on
    its own still shows the failing lines. The *full* text of each file is
    written beside the dump as a content-addressed sidecar (see
    :meth:`DumpStore.write_sources`) and referenced from :attr:`manifest` by
    hash, so the inspector can scroll the whole module as it was when the
    exception happened rather than re-reading a file that has been edited since.
    """

    __slots__ = ("root", "files", "manifest", "texts", "sidecar")

    def __init__(self, root: Optional[str] = None):
        self.root = root or os.getcwd()
        self.files: Dict[str, SourceFile] = {}
        #: ``{file_id: content hash}`` for the full text captured of that file.
        self.manifest: Dict[str, str] = {}
        #: ``{content hash: [line, ...]}``. Held only long enough to be written
        #: to the sidecar, or read back from it; never pickled into the dump.
        self.texts: Dict[str, List[str]] = {}
        #: Directory holding this dump's sidecar blobs, set by :func:`load_dump`.
        self.sidecar: Optional[str] = None

    def __getstate__(self) -> Dict[str, Any]:
        # The full texts belong to the sidecar; the dump carries only the hashes
        # that point at them, plus the small windows that make it self-contained.
        return {"root": self.root, "files": self.files, "manifest": self.manifest}

    def __setstate__(self, state: Any) -> None:
        if isinstance(state, tuple):  # dumps written before the sidecar existed
            state = state[1] or {}
        self.root = state.get("root") or os.getcwd()
        self.files = state.get("files") or {}
        self.manifest = state.get("manifest") or {}
        self.texts = {}
        self.sidecar = None

    def file_id(self, filename: str) -> str:
        """Same relative-to-root form used by exception paths."""
        return relative_path(filename, self.root)

    def capture(self, filename: str, lineno: int, code: Any = None,
                radius: Optional[int] = None) -> str:
        """Store the window around ``lineno`` and return the file id."""
        radius = CONFIG.source_radius if radius is None else radius
        file_id = sys.intern(self.file_id(filename))
        entry = self.files.get(file_id)
        if entry is None:
            entry = self.files[file_id] = SourceFile(file_id)
        try:
            source_lines = (
                inspect.findsource(code)[0] if code is not None
                else linecache.getlines(filename)
            )
        except Exception:
            source_lines = []
        if source_lines:
            start = max(1, lineno - radius)
            entry.add(start, [line.rstrip("\n") for line in source_lines[start - 1 : lineno + radius]])
            self._remember(file_id, source_lines)
        return file_id

    def _remember(self, file_id: str, source_lines: List[str]) -> None:
        """Hold a file's full text under its content hash, for the sidecar.

        First capture of a file wins: every frame in one dump saw the same
        bytes on disk, so re-hashing per frame would only cost time.
        """
        if file_id in self.manifest:
            return
        lines = [line.rstrip("\n") for line in source_lines]
        text = "\n".join(lines)
        if CONFIG.max_source_bytes and len(text) > CONFIG.max_source_bytes:
            return  # The window inside the dump still covers the failing lines.
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]
        self.manifest[file_id] = digest
        self.texts[digest] = lines

    def attach(self, directory: str) -> None:
        """Point the store at the directory its sidecar blobs live in."""
        self.sidecar = directory

    def full_lines(self, file_id: str) -> List[str]:
        """Whole captured text of ``file_id``, or ``[]`` if it was not stored.

        Read from the sidecar on first use and cached, so scrolling a large
        module in the inspector decompresses it once.
        """
        digest = self.manifest.get(file_id)
        if not digest:
            return []
        cached = self.texts.get(digest)
        if cached is not None:
            return cached
        if not self.sidecar:
            return []
        blob = os.path.join(self.sidecar, SOURCE_DIR, digest + SOURCE_SUFFIX)
        try:
            with gzip.open(blob, "rt", encoding="utf-8") as handle:
                lines = json.load(handle).get("lines") or []
        except (OSError, ValueError):
            lines = []
        self.texts[digest] = lines
        return lines

    def resolve(self, file_id: str) -> str:
        """Best-effort path on this machine for a stored file id."""
        if not file_id or file_id.startswith("<") or os.path.isabs(file_id):
            return file_id
        for base in (self.root, os.getcwd()):
            candidate = os.path.join(base, file_id)
            if os.path.exists(candidate):
                return candidate
        return os.path.join(self.root, file_id)

    def window(self, file_id: str, lineno: int, radius: Optional[int] = None):
        radius = CONFIG.source_radius if radius is None else radius
        lines = self.full_lines(file_id)
        if lines:
            low = max(1, lineno - radius)
            high = min(len(lines), lineno + radius)
            if low <= high:
                return low, lines[low - 1 : high]
        entry = self.files.get(file_id)
        return entry.window(lineno, radius) if entry is not None else (0, [])
