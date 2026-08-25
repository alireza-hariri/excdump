"""Taking the snapshot: walking frames and the exception chain.

This is the only part that runs inside a failing application, so it stays
defensive -- a capture that raises would replace the user's exception with its
own.
"""

import inspect
import sys
import time
import traceback
from functools import wraps
from types import FrameType
from typing import Any, Callable, Dict, Iterable, List, Optional, ParamSpec, Tuple, TypeVar

from .config import CONFIG, logger, _serializer_module, _serializer_name
from .model import ExceptionDump, ExceptionRecord, FrameSnapshot
from .paths import _new_trace_id, _tb_frames, exception_path, path_id
from .sources import SourceStore
from .store import DumpStore
from .values import _ValueFilter


P = ParamSpec("P")


R = TypeVar("R")


def _snapshot_frames(
    frames: Iterable[Tuple[FrameType, int]],
    store: SourceStore,
    values: _ValueFilter,
) -> List[FrameSnapshot]:
    """Snapshot live frames while they are still reachable."""
    snapshots: List[FrameSnapshot] = []
    for frame, lineno in frames:
        code = frame.f_code
        file_id = store.capture(code.co_filename, lineno, code=code)

        # Expressions only need globals referenced by this code object. Saving
        # the whole module also pulls functions, imports, and decorator state
        # into what should be a small crash snapshot.
        referenced = {
            name: frame.f_globals[name] for name in code.co_names if name in frame.f_globals
        }

        snapshots.append(
            FrameSnapshot(
                filename=sys.intern(code.co_filename),
                lineno=lineno,
                name=sys.intern(code.co_name),
                locals_dict=values.filter(frame.f_locals),
                globals_dict=values.filter(referenced),
                file_id=file_id,
                store=store,
            )
        )
    return snapshots


def _live_up_frames(frame: Optional[FrameType], limit: int) -> List[Tuple[FrameType, int]]:
    """Callers reachable from ``frame`` on the live stack, oldest first."""
    frames: List[Tuple[FrameType, int]] = []
    while frame is not None and len(frames) < limit:
        frames.append((frame, frame.f_lineno))
        frame = frame.f_back
    frames.reverse()
    return frames


def _up_frames(
    tb_frames: List[Tuple[FrameType, int]],
    anchor_index: int,
    first_up_frame: Optional[FrameType],
    limit: int,
) -> List[Tuple[FrameType, int]]:
    """The ``limit`` callers just above the anchor, oldest first.

    The traceback is the authority wherever it reaches: each entry called the
    next, and it records the line each one was executing. The live stack is a
    different chain, and not a superset -- a coroutine frame at the outer edge
    of a task has no ``f_back`` at all. Walking ``f_back`` from an anchor below
    a task boundary therefore loses every caller the traceback *does* name: the
    frames holding the request and the context, replaced by asyncio's plumbing
    or by nothing. So ``f_back`` is used only above the traceback's outermost
    frame, which is the one region no traceback can describe.
    """
    if limit <= 0:
        return []
    if first_up_frame is not None:
        # A caller hiding its own frames (:func:`dump_on_exception` and its
        # wrapper) has told us where the chain resumes, and what it wants to
        # hide *is* the traceback's outermost frame. Only the live stack knows
        # what sits above that.
        return _live_up_frames(first_up_frame, limit)
    above = tb_frames[:anchor_index][-limit:]
    outstanding = limit - len(above)
    if outstanding:
        # Ran out of traceback: keep going up the live stack from its top.
        return _live_up_frames(tb_frames[0][0].f_back, outstanding) + above
    return above


def _chain_links(exc_value: BaseException) -> List[Tuple[BaseException, Optional[str]]]:
    """Walk a chain newest-first, pairing each exception with its older link."""
    links: List[Tuple[BaseException, Optional[str]]] = []
    seen: set = set()
    current: Optional[BaseException] = exc_value
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__cause__ is not None:
            older, relation = current.__cause__, "cause"
        elif current.__context__ is not None and not current.__suppress_context__:
            older, relation = current.__context__, "context"
        else:
            older, relation = None, None
        links.append((current, relation))
        current = older
    return links


def dump_exception(
    exc_info=None,
    *,
    n_depth_up: Optional[int] = None,
    n_depth_down: Optional[int] = None,
    serializer: Optional[str] = None,
    store: Optional["DumpStore"] = None,
    metadata: Optional[Dict[str, Any]] = None,
    _anchor_frame: Optional[FrameType] = None,
    _first_up_frame: Optional[FrameType] = None,
) -> Optional[str]:
    """Capture the exception being handled and return its trace id.

    Called with no arguments inside an ``except`` block, this captures
    :func:`sys.exc_info` using the deployment's :data:`CONFIG` defaults. There
    is no file name to choose: the dump is filed under its *exception path* --
    the ``(filename, lineno)`` list of the traceback -- and named after the
    returned trace id, which is what you log and later pass to ``inspect``.

    ``n_depth_up`` captures callers above the handling frame and
    ``n_depth_down`` captures traceback frames below it. The handling frame is
    always included and is the initial frame selected by the inspector.

    Chained exceptions (``raise ... from ...`` or exceptions raised while
    handling another) are captured too, each with its own traceback frames,
    capped at ``n_depth_up + n_depth_down + 1`` innermost frames.

    Returns ``None`` when capture is disabled via ``CONFIG.enabled``.

    The two private frame arguments are used by :func:`dump_on_exception` to
    hide its wrapper frame from captured application frames.
    """
    if isinstance(exc_info, str):
        raise TypeError(
            "dump_exception no longer takes a file path; dumps are named by trace id "
            "under CONFIG.store_dir (see configure(store_dir=...))"
        )
    if not CONFIG.enabled:
        return None

    n_depth_up = CONFIG.n_depth_up if n_depth_up is None else n_depth_up
    n_depth_down = CONFIG.n_depth_down if n_depth_down is None else n_depth_down
    if n_depth_up < 0 or n_depth_down < 0:
        raise ValueError("depth values must be non-negative")

    chosen = _serializer_name(serializer)
    module = _serializer_module(chosen)
    dump_store = store or DumpStore()
    sources = SourceStore()
    values = _ValueFilter(chosen)

    if exc_info is None:
        exc_info = sys.exc_info()

    exc_type, exc_value, tb = exc_info
    if tb is None:
        raise ValueError("No active exception traceback found.")

    caller_frame = inspect.currentframe().f_back
    anchor_frame = _anchor_frame or caller_frame

    # Traceback order is outermost to innermost. Locate the handling frame so
    # "up" and "down" are measured from that frame, not the exception site.
    tb_frames = _tb_frames(tb)
    anchor_index = next(
        (index for index, (frame, _) in enumerate(tb_frames) if frame is anchor_frame),
        None,
    )
    if anchor_index is None:
        # This can happen when dump_exception is called by a separate handler.
        # In that case, use the first traceback frame as the navigation pivot.
        anchor_index = 0
        anchor_frame = tb_frames[0][0]

    anchor_lineno = tb_frames[anchor_index][1]
    down_frames = tb_frames[anchor_index + 1 : anchor_index + 1 + n_depth_down]

    up_frames = _up_frames(tb_frames, anchor_index, _first_up_frame, n_depth_up)

    primary = ExceptionRecord(
        exc_type=str(exc_type.__name__ if exc_type else "Unknown"),
        exc_value=str(exc_value),
        formatted_tb="".join(traceback.format_exception(exc_type, exc_value, tb)),
        frames=_snapshot_frames(
            up_frames + [(anchor_frame, anchor_lineno)] + down_frames, sources, values
        ),
        target_frame_index=len(up_frames),
    )

    links = _chain_links(exc_value) if exc_value is not None else [(None, None)]
    primary.relation = links[0][1] if links else None

    chain_limit = n_depth_up + n_depth_down + 1
    records = [primary]
    for older, relation in links[1:]:
        older_frames = _tb_frames(older.__traceback__)[-chain_limit:]
        records.append(
            ExceptionRecord(
                exc_type=type(older).__name__,
                exc_value=str(older),
                formatted_tb="".join(
                    traceback.format_exception_only(type(older), older)
                ),
                frames=_snapshot_frames(older_frames, sources, values),
                target_frame_index=max(0, len(older_frames) - 1),
                relation=relation,
            )
        )

    records.reverse()  # oldest first, handled exception last

    path = exception_path(tb, sources.root)
    trace_id = _new_trace_id(path_id(path))
    dump_data = ExceptionDump(
        records,
        sources=sources,
        trace_id=trace_id,
        path=path,
        created_at=time.time(),
        metadata=dict(metadata) if metadata else None,
        # Written last: every frame has been filtered by now, so this is the
        # complete set of values dill had to take, serialized together.
        dill_blob=values.dill_blob(),
    )

    filepath = dump_store.write(dump_data, module)

    if CONFIG.verbose:
        print(f"[+] Exception context saved to: {filepath}", file=sys.stderr)
    logger.info("captured %s as %s (%s)", dump_data.exc_type, trace_id, filepath)
    _notify(CONFIG.on_dump, trace_id)
    return trace_id


def _notify(callback: Optional[Callable[[str], None]], trace_id: str) -> None:
    """Hand a trace id to a user callback without letting it break capture."""
    if callback is None:
        return
    try:
        callback(trace_id)
    except Exception:
        logger.exception("on_dump callback failed for %s", trace_id)


def dump_on_exception(
    func: Optional[Callable[P, R]] = None,
    *,
    on_dump: Optional[Callable[[str], None]] = None,
    n_depth_up: Optional[int] = None,
    n_depth_down: Optional[int] = None,
    serializer: Optional[str] = None,
    store: Optional[DumpStore] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Any:
    """Dump exceptions leaving a function, then re-raise them unchanged.

    Usable bare (``@dump_on_exception``) or with options
    (``@dump_on_exception(on_dump=report)``). ``on_dump`` receives the trace id
    of each dump, which is how a service ties a user-visible error to the dump
    it can inspect later; it runs in addition to ``CONFIG.on_dump``.

    The decorated function is the navigation pivot. Decorator implementation
    frames are omitted, so ``up`` reaches its real caller and ``down`` reaches
    functions called by it. Capture never changes the program's behaviour: the
    original exception propagates even if writing the dump fails.
    """
    if isinstance(func, str):
        raise TypeError(
            "dump_on_exception no longer takes a file path; dumps are named by trace id "
            "under CONFIG.store_dir (see configure(store_dir=...))"
        )

    def decorate(target: Callable[P, R]) -> Callable[P, R]:
        @wraps(target)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return target(*args, **kwargs)
            except Exception:
                exc_info = sys.exc_info()
                tb = exc_info[2]
                # The traceback starts at this wrapper; the next frame is the
                # decorated function and is the correct up/down pivot.
                decorated_tb = tb.tb_next if tb is not None else None
                if decorated_tb is None:
                    raise
                try:
                    trace_id = dump_exception(
                        exc_info,
                        n_depth_up=n_depth_up,
                        n_depth_down=n_depth_down,
                        serializer=serializer,
                        store=store,
                        metadata=metadata,
                        _anchor_frame=decorated_tb.tb_frame,
                        _first_up_frame=inspect.currentframe().f_back,
                    )
                except Exception:
                    # A debugging aid must never take down the application.
                    logger.exception("failed to capture exception from %s", target.__qualname__)
                else:
                    if trace_id is not None:
                        _notify(on_dump, trace_id)
                raise

        return wrapper

    return decorate if func is None else decorate(func)
