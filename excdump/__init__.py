"""Capture rich exception snapshots and inspect them offline.

The dump stores the whole exception chain (``__cause__`` / ``__context__``), so
the inspector can walk between chained exceptions the way modern pdb does with
its ``exceptions`` command, in addition to walking frames within one exception.

Dumps are kept small: source is stored once per file (as merged line windows
keyed by a path relative to the capture root, not per frame), each object is
serialized once no matter how many frames reference it, the whole file is
gzipped, and each value is stored with the cheapest serializer that can hold
it -- plain pickle for almost everything, dill only where pickle fails.
The full text of every captured file is written once per exception
path, beside the dumps, and referenced by content hash -- the inspector reads
that instead of the file on disk, so line numbers still line up after the code
has moved on. :func:`set_serializer` overrides that per-value choice with strict ``dill``
or strict ``pickle``.

In production, capture is configured once and then needs no arguments::

    configure(store_dir="/var/log/exception_dumps", max_dumps_per_path=1000,
              on_dump=lambda trace_id: log.error("dump %s", trace_id))

    try:
        ...
    except Exception:
        trace_id = dump_exception()      # returns the id to log

Dumps are filed by *exception path* -- the ``(filename, lineno)`` list of the
traceback -- and each path keeps only its most recent
``CONFIG.max_dumps_per_path`` dumps, so a hot failure loop cannot fill the disk
and cannot push other, rarer failures out of the store. What those dumps have
in common -- the text of the files they captured, and the dill stream carrying
the functions and classes pickle could not -- is stored once for the path and
referenced by hash, so repeats of one failure cost little more than the values
that actually differ.

The implementation is split by responsibility -- :mod:`~excdump.config`,
:mod:`~excdump.paths`, :mod:`~excdump.model`, :mod:`~excdump.sources`,
:mod:`~excdump.values`, :mod:`~excdump.capture`, :mod:`~excdump.store`,
:mod:`~excdump.loading`, :mod:`~excdump.session`, :mod:`~excdump.cli` --
and everything a caller needs is re-exported here.
"""

from .capture import dump_exception, dump_on_exception
from .cli import (
    COMMANDS,
    OfflinePdb,
    dispatch,
    gc_command,
    help_text,
    list_command,
    load_and_debug,
    main,
    plain_loop,
)
from .config import (
    CONFIG,
    SERIALIZERS,
    Config,
    SerializerName,
    Unset,
    UNSET,
    configure,
    get_serializer,
    logger,
    set_serializer,
)
from .loading import load_dump
from .model import (
    ExceptionDump,
    ExceptionRecord,
    FrameSnapshot,
    MissingRef,
)
from .paths import (
    DUMP_SUFFIX,
    PATH_META,
    SOURCE_DIR,
    SOURCE_SUFFIX,
    exception_path,
    path_id,
    relative_path,
    trace_path_id,
)
from .session import DebuggerSession
from .sources import SourceFile, SourceStore
from .store import DumpStore, default_store, resolve_dump
from .values import _DillRef, _ModuleRef, _ValueFilter

__all__ = [
    "CONFIG",
    "COMMANDS",
    "Config",
    "DebuggerSession",
    "DumpStore",
    "ExceptionDump",
    "ExceptionRecord",
    "FrameSnapshot",
    "MissingRef",
    "OfflinePdb",
    "SerializerName",
    "SourceFile",
    "SourceStore",
    "configure",
    "default_store",
    "dispatch",
    "dump_exception",
    "dump_on_exception",
    "exception_path",
    "gc_command",
    "get_serializer",
    "help_text",
    "list_command",
    "load_and_debug",
    "load_dump",
    "main",
    "path_id",
    "plain_loop",
    "relative_path",
    "resolve_dump",
    "set_serializer",
    "trace_path_id",
]
