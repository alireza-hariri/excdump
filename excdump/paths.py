"""Names: where a dump lives, and what identifies the failure it captured.

An *exception path* is the ``(filename, lineno)`` list of a traceback -- the
identity of "this exception happening here". Dumps are grouped by its hash, so
a hot failure loop cannot push rarer failures out of the store.
"""

import hashlib
import os
import time
import uuid
from types import FrameType
from typing import Any, Iterable, List, Optional, Tuple


# -- dump store --------------------------------------------------------------

#: Dump files are ``<trace_id>.dump`` inside ``<store_dir>/<path_id>/``.
DUMP_SUFFIX = ".dump"


PATH_META = "path.json"


#: Empty marker in the store root; its mtime is when a sweep last ran.
GC_STAMP = ".gc"


#: Sidecar holding the full text of every file a path's dumps captured.
SOURCE_DIR = "sources"


SOURCE_SUFFIX = ".json.gz"


#: Everything is written under this suffix and renamed into place, so a reader
#: never sees a half-written file.
TEMP_SUFFIX = ".part"


def relative_path(filename: str, root: Optional[str] = None) -> str:
    """Path as stored in dumps: relative to ``root`` when it lives under it."""
    if not filename or filename.startswith("<"):
        return filename
    root = root or os.getcwd()
    try:
        abspath = os.path.abspath(filename)
        relative = os.path.relpath(abspath, root)
    except (ValueError, OSError):
        return filename
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return abspath
    return relative


def _tb_frames(tb) -> List[Tuple[FrameType, int]]:
    """Traceback frames, outermost first."""
    frames = []
    while tb is not None:
        frames.append((tb.tb_frame, tb.tb_lineno))
        tb = tb.tb_next
    return frames


def exception_path(tb: Any, root: Optional[str] = None) -> List[Tuple[str, int]]:
    """``[(filename, lineno), ...]`` for a traceback, outermost first.

    This is the identity of a failure: the same bug hit a million times shares
    one path, so retention can be applied per path instead of globally.
    """
    return [
        (relative_path(frame.f_code.co_filename, root), lineno)
        for frame, lineno in _tb_frames(tb)
    ]


def path_id(path: Iterable[Tuple[str, int]]) -> str:
    """Stable short id for an exception path."""
    digest = hashlib.sha256("\n".join(f"{name}:{line}" for name, line in path).encode())
    return digest.hexdigest()[:16]


def _new_trace_id(pid: str) -> str:
    """Time-ordered, collision-resistant id that doubles as a file name.

    The fixed-width nanosecond stamp makes lexical order equal chronological
    order, which is what retention relies on to drop the oldest dumps.
    """
    return f"{pid}-{time.time_ns():020d}-{os.getpid():d}-{uuid.uuid4().hex[:8]}"


def trace_path_id(trace_id: str) -> str:
    """The path id embedded in a trace id."""
    return trace_id.split("-", 1)[0]


def _silent_remove(target: str) -> bool:
    """Delete a file, tolerating a peer process having deleted it first."""
    try:
        os.remove(target)
        return True
    except OSError:
        return False
