"""Compatibility facade over the :mod:`excdump` package.

The implementation moved into ``excdump/`` -- see that package's docstring for
what it does and how it is laid out. This module stays for two reasons.

Dumps are pickle streams, and pickle records the *module path* of every class it
stores. Dumps written before the split name ``exception_debugger.SourceStore``,
``exception_debugger.FrameSnapshot`` and friends, so every one of those names is
re-exported here and keeps resolving.

It also keeps ``python exception_debugger.py inspect <trace-id>`` working, which
matters for a tool that gets copied onto a host to read a crash. New code should
prefer ``import excdump`` and ``python -m excdump``.

Every name the single-file version exposed is re-exported below, private ones
included: dumps and downstream scripts referenced some of them by name.
"""

import sys

from excdump.config import (  # noqa: F401
    CONFIG,
    Config,
    ENV_PREFIX,
    SERIALIZERS,
    SerializerName,
    T,
    UNSET,
    Unset,
    _env,
    _env_bool,
    _env_serializer,
    _serializer_module,
    _serializer_name,
    configure,
    get_serializer,
    logger,
    set_serializer,
)
from excdump.paths import (  # noqa: F401
    DUMP_SUFFIX,
    PATH_META,
    SOURCE_DIR,
    SOURCE_SUFFIX,
    _new_trace_id,
    _silent_remove,
    _tb_frames,
    exception_path,
    path_id,
    relative_path,
    trace_path_id,
)
from excdump.model import (  # noqa: F401
    ExceptionDump,
    ExceptionRecord,
    FrameSnapshot,
    MissingRef,
    _normalize_dump,
)
from excdump.sources import (  # noqa: F401
    SourceFile,
    SourceStore,
)
from excdump.values import (  # noqa: F401
    _DillRef,
    _MAIN_REF,
    _MISSING,
    _ModuleRef,
    _ValueFilter,
    _dill_payload,
    _load_module_ref,
    _pickle_payload,
    _resolve_dill_refs,
)
from excdump.store import (  # noqa: F401
    DumpStore,
    default_store,
    resolve_dump,
)
from excdump.loading import (  # noqa: F401
    _tolerant_unpickler,
    load_dump,
)
from excdump.capture import (  # noqa: F401
    P,
    R,
    _chain_links,
    _notify,
    _snapshot_frames,
    dump_exception,
    dump_on_exception,
)
from excdump.session import (  # noqa: F401
    DebuggerSession,
    RELATION_TEXT,
)
from excdump.cli import (  # noqa: F401
    ALIAS_MAP,
    BARE_ALIASES,
    COMMANDS,
    OfflinePdb,
    USAGE,
    _demo,
    _describe_path,
    dispatch,
    gc_command,
    help_text,
    list_command,
    load_and_debug,
    main,
    plain_loop,
)

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
