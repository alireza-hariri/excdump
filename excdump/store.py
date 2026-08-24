"""The on-disk dump store: layout, retention, and lookup.

Nothing is ever read to decide where a dump goes, so concurrent processes can
write into one store without coordination.
"""

import gzip
import json
import os
import pickle
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG
from .loading import load_dump
from .model import ExceptionDump
from .paths import (
    DUMP_SUFFIX,
    PATH_META,
    SOURCE_DIR,
    SOURCE_SUFFIX,
    _silent_remove,
    trace_path_id,
)


class DumpStore:
    """Directory of dumps grouped by exception path, with per-path retention.

    Layout::

        <root>/<path_id>/path.json          the (filename, lineno) list, for humans
        <root>/<path_id>/sources/<hash>.json.gz   full text of one captured file
        <root>/<path_id>/<trace_id>.dump

    Nothing is ever read to decide where a dump goes, so concurrent processes
    can write into the same store without coordination. Source blobs are named
    by the hash of their contents for the same reason: two processes writing
    the same file's text write the same bytes to the same name, and a file
    edited between two dumps of one path simply lands in a second blob.
    """

    def __init__(self, root: Optional[str] = None, max_per_path: Optional[int] = None):
        self.root = root or CONFIG.store_dir
        self.max_per_path = CONFIG.max_dumps_per_path if max_per_path is None else max_per_path

    # -- locations -----------------------------------------------------------

    def path_dir(self, pid: str) -> str:
        return os.path.join(self.root, pid)

    def dump_file(self, trace_id: str) -> str:
        return os.path.join(self.path_dir(trace_path_id(trace_id)), trace_id + DUMP_SUFFIX)

    # -- writing -------------------------------------------------------------

    def write(self, dump: ExceptionDump, module: Any) -> str:
        """Serialize ``dump`` under its path, prune the path, return its file."""
        directory = self.path_dir(trace_path_id(dump.trace_id))
        os.makedirs(directory, exist_ok=True)
        self._write_path_meta(directory, dump)
        self.write_sources(directory, dump)

        target = os.path.join(directory, dump.trace_id + DUMP_SUFFIX)
        # Write to a temporary name first: a reader (or a pruning peer) must
        # never observe a half-written dump.
        temporary = target + ".part"
        try:
            # gzip keeps the API as one portable file while compressing repeated
            # frame names, paths, source text, and traceback text very well.
            with gzip.open(temporary, "wb", compresslevel=9) as handle, warnings.catch_warnings():
                warnings.simplefilter("ignore")
                module.dump(dump, handle, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(temporary, target)
        except BaseException:
            _silent_remove(temporary)
            raise

        self.prune(trace_path_id(dump.trace_id))
        return target

    def write_sources(self, directory: str, dump: ExceptionDump) -> None:
        """Write each captured file's full text as a content-addressed blob.

        The dump itself only stores ``{file_id: hash}``, so the text is written
        once per version per path no matter how many dumps that path keeps. An
        existing blob is never rewritten: same hash means same bytes.
        """
        store = getattr(dump, "sources", None)
        if store is None or not getattr(store, "texts", None):
            return
        blob_dir = os.path.join(directory, SOURCE_DIR)
        try:
            os.makedirs(blob_dir, exist_ok=True)
        except OSError:
            return
        owners = {digest: file_id for file_id, digest in store.manifest.items()}
        for digest, lines in store.texts.items():
            target = os.path.join(blob_dir, digest + SOURCE_SUFFIX)
            if os.path.exists(target):
                continue
            # Self-describing, so a blob still means something on its own and
            # the sidecar needs no index file for a reader to make sense of it.
            payload = {"path": owners.get(digest, ""), "sha256": digest, "lines": lines}
            temporary = f"{target}.{os.getpid()}.part"
            try:
                with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=9) as handle:
                    json.dump(payload, handle)
                os.replace(temporary, target)
            except OSError:
                _silent_remove(temporary)

    def _write_path_meta(self, directory: str, dump: ExceptionDump) -> None:
        meta_file = os.path.join(directory, PATH_META)
        if os.path.exists(meta_file):
            return
        payload = {
            "path_id": trace_path_id(dump.trace_id),
            "exc_type": dump.exc_type,
            "path": [[name, line] for name, line in dump.path],
        }
        temporary = f"{meta_file}.{os.getpid()}.part"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(temporary, meta_file)
        except OSError:
            _silent_remove(temporary)

    def prune(self, pid: str, max_per_path: Optional[int] = None) -> List[str]:
        """Delete the oldest dumps of one path beyond the retention limit."""
        limit = self.max_per_path if max_per_path is None else max_per_path
        if limit <= 0:
            return []
        names = self.dump_ids(pid)
        removed = []
        for trace_id in names[: max(0, len(names) - limit)]:
            target = os.path.join(self.path_dir(pid), trace_id + DUMP_SUFFIX)
            if _silent_remove(target):
                removed.append(trace_id)
        if removed and not self.dump_ids(pid):
            # Last dump of the path is gone; nothing can reference its source.
            self.gc_sources(pid, keep=set())
        return removed

    def gc_paths(
        self,
        pids: Optional[List[str]] = None,
        max_age_days: Optional[float] = None,
    ) -> List[str]:
        """Delete whole paths nothing has hit for ``CONFIG.max_path_age_days``.

        :meth:`prune` bounds the dumps within a path but never the number of
        paths, and a path directory is never emptied -- so without this the
        store only grows. Most of that growth is not stale so much as
        unreachable: a path id hashes ``(filename, lineno)`` pairs, so a deploy
        that shifts a line strands every path through that file under an id no
        future exception can produce. Age separates those from failures that
        simply have not recurred yet, and needs no deploy hook to do it.

        Like :meth:`gc_sources`, a maintenance operation: deleting a path means
        listing and unlinking it, which has no place on the capture path.
        """
        limit = CONFIG.max_path_age_days if max_age_days is None else max_age_days
        if limit <= 0:
            return []
        cutoff = time.time() - limit * 86400.0
        removed = []
        for pid in self.path_ids() if pids is None else pids:
            last_seen = self.path_last_seen(pid)
            # No parseable dump: cannot tell the path's age, so leave it alone.
            if last_seen is None or last_seen >= cutoff:
                continue
            if self._remove_path(pid, cutoff):
                removed.append(pid)
        return removed

    def path_last_seen(self, pid: str) -> Optional[float]:
        """When this path last occurred, or ``None`` if that cannot be told.

        Read out of the file names: a trace id carries the capture time as
        fixed-width nanoseconds, so the age of a path costs one directory
        listing -- no ``stat`` calls, and certainly no loading of dumps.
        """
        for trace_id in reversed(self.dump_ids(pid)):
            parts = trace_id.split("-")
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1]) / 1e9
        return None

    def _remove_path(self, pid: str, cutoff: float) -> bool:
        """Delete one path's dumps, source blobs and metadata, then itself.

        Only the dumps listed here are deleted, and the directory is removed
        with ``rmdir`` rather than recursively: a peer that captured this
        failure again in the meantime leaves a dump behind, ``rmdir`` refuses,
        and the revived path survives with its new dump.
        """
        directory = self.path_dir(pid)
        trace_ids = self.dump_ids(pid)
        # Re-read the age from that same listing: the path may have been hit
        # between the survey and now, which makes it live again.
        if not trace_ids or (self.path_last_seen(pid) or 0.0) >= cutoff:
            return False
        for trace_id in trace_ids:
            _silent_remove(os.path.join(directory, trace_id + DUMP_SUFFIX))
        self.gc_sources(pid, keep=set())
        _silent_remove(os.path.join(directory, PATH_META))
        try:
            os.rmdir(directory)
            return True
        except OSError:
            return False

    def gc_sources(self, pid: str, keep: Optional[set] = None) -> List[str]:
        """Delete source blobs of ``pid`` that no surviving dump references.

        Not called after an ordinary prune: working out what is still
        referenced means loading every remaining dump of the path, which is far
        more work than the blobs are worth on the capture path. Blobs only
        accumulate when a captured file changes *without* moving any line in
        the traceback -- any other edit produces a new path id, and so a new
        directory -- so this is a maintenance operation, not a hot one.
        """
        blob_dir = os.path.join(self.path_dir(pid), SOURCE_DIR)
        if not os.path.isdir(blob_dir):
            return []
        if keep is None:
            keep = set()
            for trace_id in self.dump_ids(pid):
                try:
                    dump = load_dump(os.path.join(self.path_dir(pid), trace_id + DUMP_SUFFIX))
                except Exception:
                    return []  # Cannot prove a blob is unused; keep them all.
                keep.update(getattr(dump.sources, "manifest", {}).values())
        removed = []
        for name in sorted(os.listdir(blob_dir)):
            if not name.endswith(SOURCE_SUFFIX):
                continue
            if name[: -len(SOURCE_SUFFIX)] in keep:
                continue
            if _silent_remove(os.path.join(blob_dir, name)):
                removed.append(name)
        try:
            os.rmdir(blob_dir)  # Only succeeds when nothing is left.
        except OSError:
            pass
        return removed

    # -- reading -------------------------------------------------------------

    def path_ids(self) -> List[str]:
        try:
            return sorted(
                name for name in os.listdir(self.root)
                if os.path.isdir(os.path.join(self.root, name))
            )
        except OSError:
            return []

    def dump_ids(self, pid: str) -> List[str]:
        """Trace ids stored for one path, oldest first."""
        try:
            names = os.listdir(self.path_dir(pid))
        except OSError:
            return []
        return sorted(n[: -len(DUMP_SUFFIX)] for n in names if n.endswith(DUMP_SUFFIX))

    def path_meta(self, pid: str) -> Dict[str, Any]:
        try:
            with open(os.path.join(self.path_dir(pid), PATH_META), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def matches(self, trace_id: str) -> List[str]:
        """Trace ids equal to, or starting with, ``trace_id``."""
        pid = trace_path_id(trace_id)
        if os.path.exists(self.dump_file(trace_id)):
            return [trace_id]
        return [t for t in self.dump_ids(pid) if t.startswith(trace_id)]

    def find(self, trace_id: str) -> Optional[str]:
        """File for a trace id, or for a unique prefix of one."""
        candidates = self.matches(trace_id)
        if len(candidates) != 1:
            return None
        return os.path.join(self.path_dir(trace_path_id(trace_id)), candidates[0] + DUMP_SUFFIX)

    def latest(self, pid: Optional[str] = None) -> Optional[str]:
        """Most recent dump overall, or within one path."""
        pids = [pid] if pid else self.path_ids()
        newest: Optional[Tuple[str, str]] = None
        for candidate in pids:
            ids = self.dump_ids(candidate)
            if not ids:
                continue
            stamp = ids[-1].split("-")[1] if "-" in ids[-1] else ""
            if newest is None or stamp > newest[0]:
                newest = (stamp, os.path.join(self.path_dir(candidate), ids[-1] + DUMP_SUFFIX))
        return newest[1] if newest else None


def default_store() -> DumpStore:
    """Store described by the current :data:`CONFIG`."""
    return DumpStore()


def resolve_dump(target: Optional[str] = None, store: Optional[DumpStore] = None) -> str:
    """Turn a trace id, id prefix, path id, or file name into a dump file.

    With no target, the most recent dump in the store is used -- the common
    case right after a crash.
    """
    store = store or DumpStore()
    if target and (target.endswith(DUMP_SUFFIX) or os.path.isfile(target)):
        return target
    if not target:
        latest = store.latest()
        if latest is None:
            raise FileNotFoundError(f"no dumps in {store.root}")
        return latest
    if os.path.isdir(store.path_dir(target)) and target == trace_path_id(target):
        # A bare path id: open the newest dump recorded for that failure.
        latest = store.latest(target)
        if latest is None:
            raise FileNotFoundError(f"path {target} has no dumps in {store.root}")
        return latest

    candidates = store.matches(target)
    if len(candidates) > 1:
        shown = "\n  ".join(candidates[:10])
        raise ValueError(
            f"{len(candidates)} dumps start with {target!r}; use a longer id:\n  {shown}"
        )
    if candidates:
        return store.dump_file(candidates[0])
    raise FileNotFoundError(f"no dump matching {target!r} in {store.root}")
