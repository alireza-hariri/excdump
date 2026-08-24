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
from .model import ExceptionDump
from .paths import (
    DUMP_SUFFIX,
    GC_STAMP,
    PATH_META,
    SOURCE_DIR,
    SOURCE_SUFFIX,
    TEMP_SUFFIX,
    _silent_remove,
    trace_path_id,
)


#: A temporary older than this was left behind by a process that died between
#: writing a file and renaming it into place. A rename takes microseconds, so
#: nothing legitimate is ever this old.
STALE_TEMPORARY_SECONDS = 3600.0


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
        temporary = target + TEMP_SUFFIX
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
        self.maybe_gc()
        return target

    def maybe_gc(self, interval: Optional[float] = None) -> List[str]:
        """Sweep dead paths, but at most once per ``CONFIG.gc_interval_seconds``.

        Without this a store is only ever tidied by someone remembering to run
        ``gc``, which on a long-running service means never. The rate is set in
        time rather than as a share of captures, because capture rate is the
        wrong clock: a failure storm would sweep thousands of times an hour and
        a service that fails twice a week would never sweep at all.

        The interval is held in the mtime of an empty marker in the store root,
        and claimed by touching it *before* sweeping, so a peer capturing
        meanwhile sees a fresh stamp and skips. Two processes racing that
        window both sweep, which is harmless -- the sweep tolerates a peer
        having deleted a file first. Only the age rule runs; reclaiming source
        blobs means loading every dump and stays in the command.

        Never raises: this runs with an exception already in flight, and the
        dump is on disk by the time it is called. A store that cannot be swept
        is not a reason to lose the capture.
        """
        seconds = CONFIG.gc_interval_seconds if interval is None else interval
        if seconds <= 0 or CONFIG.max_path_age_days <= 0:
            return []
        try:
            stamp = os.path.join(self.root, GC_STAMP)
            try:
                due = time.time() - os.path.getmtime(stamp) >= seconds
            except OSError:
                due = True  # No marker yet: this is the store's first sweep.
            if not due:
                return []
            with open(stamp, "a"):
                os.utime(stamp, None)
            return self.gc_paths()
        except Exception:
            return []

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
            temporary = f"{target}.{os.getpid()}{TEMP_SUFFIX}"
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
        temporary = f"{meta_file}.{os.getpid()}{TEMP_SUFFIX}"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(temporary, meta_file)
        except OSError:
            _silent_remove(temporary)

    def prune(self, pid: str, max_per_path: Optional[int] = None) -> List[str]:
        """Delete the oldest dumps of one path beyond the retention limit.

        Source blobs the pruned dumps referenced are left alone. Sorting out
        which are still wanted means loading every dump that survived, and they
        go anyway when the path itself is reclaimed by age.
        """
        limit = self.max_per_path if max_per_path is None else max_per_path
        if limit <= 0:
            return []
        names = self.dump_ids(pid)
        removed = []
        for trace_id in names[: max(0, len(names) - limit)]:
            target = os.path.join(self.path_dir(pid), trace_id + DUMP_SUFFIX)
            if _silent_remove(target):
                removed.append(trace_id)
        return removed

    def gc_paths(
        self,
        pids: Optional[List[str]] = None,
        max_age_days: Optional[float] = None,
    ) -> List[str]:
        """Delete whole paths nothing has hit for ``CONFIG.max_path_age_days``.

        :meth:`prune` bounds the dumps within a path but never the number of
        paths -- so without this the store only grows. Most of that growth is
        not stale so much as unreachable: a path id hashes ``(filename,
        lineno)`` pairs, so a deploy that shifts a line strands every path
        through that file under an id no future exception can produce. Age
        separates those from failures that simply have not recurred yet, and
        needs no deploy hook to do it.

        This is where everything in the store is reclaimed. Nothing inside a
        path is collected on its own: working out which source blobs a dump
        still wants means loading it, and the whole directory goes together
        when the failure stops happening. Paths that stay only have their
        abandoned temporaries swept.
        """
        limit = CONFIG.max_path_age_days if max_age_days is None else max_age_days
        if limit <= 0:
            return []
        cutoff = time.time() - limit * 86400.0
        removed = []
        for pid in self.path_ids() if pids is None else pids:
            last_seen = self.path_last_seen(pid)
            if last_seen is None:
                continue  # Unreadable directory; nothing to judge it on.
            if last_seen < cutoff and self._remove_path(pid, cutoff):
                removed.append(pid)
            else:
                self._drop_stale_temporaries(pid)
        return removed

    def path_last_seen(self, pid: str) -> Optional[float]:
        """When this path last occurred, or ``None`` if it cannot be read.

        Taken from the file names: a trace id carries its capture time as
        fixed-width nanoseconds, so the age of a path costs one directory
        listing -- no ``stat`` calls, and certainly no loading of dumps.

        A path holding no readable dump falls back to the mtime of the
        directory itself. That is what collects one emptied by hand, and one
        left holding nothing but a temporary from a process killed mid-write --
        both of which are invisible to the listing above and so, judged on
        dumps alone, would be skipped forever. It cannot strand a directory a
        peer is creating right now, whose mtime is now.
        """
        for trace_id in reversed(self.dump_ids(pid)):
            parts = trace_id.split("-")
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1]) / 1e9
        try:
            return os.path.getmtime(self.path_dir(pid))
        except OSError:
            return None

    def _remove_path(self, pid: str, cutoff: float) -> bool:
        """Delete everything in one path directory, then the directory itself.

        Everything: dumps, source blobs, metadata, and any temporary left
        behind by a process that died mid-write. With the path goes the only
        thing that referred to any of it.

        Only the entries listed here are deleted, and the directory is removed
        with ``rmdir`` rather than recursively: a peer that captured this
        failure again in the meantime leaves a dump behind, ``rmdir`` refuses,
        and the revived path survives with its new dump.
        """
        directory = self.path_dir(pid)
        # Re-read the age: the path may have been hit since the survey, which
        # makes it live again.
        if (self.path_last_seen(pid) or 0.0) >= cutoff:
            return False
        blob_dir = os.path.join(directory, SOURCE_DIR)
        # The sidecar is emptied first: it is itself an entry of the path
        # directory, and only an empty one can be unlinked with the rest.
        for subdir in (blob_dir, directory):
            for name in self._listdir(subdir):
                _silent_remove(os.path.join(subdir, name))
        try:
            os.rmdir(blob_dir)
        except OSError:
            pass  # Absent, or a peer wrote into it -- the verdict is below.
        try:
            os.rmdir(directory)
            return True
        except OSError:
            return False

    def _drop_stale_temporaries(self, pid: str) -> List[str]:
        """Delete half-written files a process died before renaming into place.

        :meth:`write` unlinks its own temporary when it fails, but ``SIGKILL``
        and power loss run no handler. What they leave matches none of the
        suffixes anything here lists, so without this nothing would ever
        collect it -- and a leftover in a path due for removal would keep
        ``rmdir`` failing forever.
        """
        directory = self.path_dir(pid)
        horizon = time.time() - STALE_TEMPORARY_SECONDS
        removed = []
        for subdir in (directory, os.path.join(directory, SOURCE_DIR)):
            for name in self._listdir(subdir):
                if not name.endswith(TEMP_SUFFIX):
                    continue
                target = os.path.join(subdir, name)
                try:
                    if os.path.getmtime(target) >= horizon:
                        continue  # Someone may still be writing it.
                except OSError:
                    continue
                if _silent_remove(target):
                    removed.append(name)
        return removed

    # -- reading -------------------------------------------------------------

    def _listdir(self, directory: str) -> List[str]:
        """Sorted entries of a directory, empty if it is gone or unreadable."""
        try:
            return sorted(os.listdir(directory))
        except OSError:
            return []

    def path_ids(self) -> List[str]:
        return [
            name for name in self._listdir(self.root)
            if os.path.isdir(os.path.join(self.root, name))
        ]

    def dump_ids(self, pid: str) -> List[str]:
        """Trace ids stored for one path, oldest first."""
        return [
            name[: -len(DUMP_SUFFIX)]
            for name in self._listdir(self.path_dir(pid))
            if name.endswith(DUMP_SUFFIX)
        ]

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
