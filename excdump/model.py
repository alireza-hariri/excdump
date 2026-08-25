"""What a dump *is*: the frames, the exception chain, and the container.

These are the classes pickle names inside every dump file, so their module path
is part of the on-disk format: moving one of them makes every dump already on
disk unreadable.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

from .sources import SourceStore


class FrameSnapshot:
    """Serializable snapshot of a single execution frame.

    Source is not stored here: :attr:`file_id` points into the dump's shared
    :class:`SourceStore`. ``code_context``/``code_context_start`` remain only so
    dumps written by older versions keep working.
    """

    def __init__(
        self,
        filename: str,
        lineno: int,
        name: str,
        locals_dict: Dict[str, Any],
        globals_dict: Dict[str, Any],
        code_context: Optional[List[str]] = None,
        code_context_start: int = 1,
        file_id: Optional[str] = None,
        store: Optional[SourceStore] = None,
    ):
        self.filename = filename
        self.lineno = lineno
        self.name = name
        self.locals = locals_dict
        self.globals = globals_dict
        self.code_context = code_context or []
        self.code_context_start = code_context_start
        self.file_id = filename if file_id is None else file_id
        # One shared store per dump; the serializer writes the reference once.
        self.store = store

    def path(self) -> str:
        """Where this frame's file lives now, preferring the dump's root."""
        store = getattr(self, "store", None)
        if store is None:
            return self.filename
        resolved = store.resolve(getattr(self, "file_id", self.filename))
        return resolved if os.path.exists(resolved) else self.filename

    def source(self, radius: Optional[int] = None) -> Tuple[int, List[str]]:
        """``(start_lineno, lines)`` captured in the dump for this frame."""
        store = getattr(self, "store", None)
        if store is not None:
            start, lines = store.window(
                getattr(self, "file_id", self.filename), self.lineno, radius
            )
            if lines:
                return start, lines
        return getattr(self, "code_context_start", 1), list(self.code_context)


class ExceptionRecord:
    """One exception of a chain, with the frames captured around it.

    ``relation`` describes how this exception links to the *previous* (older)
    record in :attr:`ExceptionDump.exceptions`: ``"cause"`` for an explicit
    ``raise ... from ...`` and ``"context"`` for an implicit chain.
    """

    def __init__(
        self,
        exc_type: str,
        exc_value: str,
        formatted_tb: str,
        frames: List[FrameSnapshot],
        target_frame_index: int,
        relation: Optional[str] = None,
    ):
        self.exc_type = exc_type
        self.exc_value = exc_value
        self.formatted_tb = formatted_tb
        self.frames = frames
        self.target_frame_index = target_frame_index
        self.relation = relation

    def title(self) -> str:
        return f"{self.exc_type}: {self.exc_value}" if self.exc_value else self.exc_type


class ExceptionDump:
    """Container holding an exception chain and its serialized frames.

    :attr:`sources` holds every captured source file once; frames reference it.
    The attributes of the handled (newest) exception are mirrored on the dump
    itself so dumps stay readable by older tooling.
    """

    def __init__(
        self,
        exceptions: List[ExceptionRecord],
        sources: Optional[SourceStore] = None,
        trace_id: str = "",
        path: Optional[List[Tuple[str, int]]] = None,
        created_at: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
        dill_blob: Optional[bytes] = None,
    ):
        self.exceptions = exceptions
        self.sources = sources
        #: Independently loadable dill payloads for values plain pickle could
        #: not carry; frames point into them with :class:`_DillRef`.
        self.dill_blob = dill_blob
        #: Identifier returned by :func:`dump_exception`; also the file name.
        self.trace_id = trace_id
        #: ``[(filename, lineno), ...]`` of the handled traceback; the identity
        #: of "this exception happening here", used for grouping and retention.
        self.path = path or []
        self.created_at = created_at
        self.metadata = metadata or {}
        primary = exceptions[-1]
        self.exc_type = primary.exc_type
        self.exc_value = primary.exc_value
        self.formatted_tb = primary.formatted_tb
        self.frames = primary.frames
        self.target_frame_index = primary.target_frame_index


def _normalize_dump(dump: Any) -> ExceptionDump:
    """Accept dumps written by older versions that had no chain or store."""
    for name, default in (
        ("sources", None), ("trace_id", ""), ("path", []), ("created_at", 0.0),
        ("metadata", {}), ("dill_blob", None),
    ):
        if not hasattr(dump, name):
            setattr(dump, name, default)
    records = getattr(dump, "exceptions", None)
    if records:
        return dump
    record = ExceptionRecord(
        exc_type=getattr(dump, "exc_type", "Unknown"),
        exc_value=getattr(dump, "exc_value", ""),
        formatted_tb=getattr(dump, "formatted_tb", ""),
        frames=getattr(dump, "frames", []),
        target_frame_index=getattr(dump, "target_frame_index", 0),
    )
    dump.exceptions = [record]
    return dump


class MissingRef:
    """Stand-in for a global a dump referenced that this process cannot import.

    Frames commonly capture module-level names; when a dump is inspected
    somewhere the defining module is absent or has changed, that should degrade
    to a visible placeholder rather than making the whole dump unloadable.
    Pickle still writes an unavailable instance's state, so retain it: the class
    is gone, but its captured fields are often the most useful part of the value.
    """

    def __init__(self, module: str, name: str):
        self.module = module
        self.name = name
        self._state = {}

    def __call__(self, *args: Any, **kwargs: Any) -> "MissingRef":
        return self

    def __getattr__(self, name: str) -> Any:
        state = self.__dict__.get("_state", {})
        if name in state:
            return state[name]
        raise AttributeError(name)

    def __setstate__(self, state: Any) -> None:
        """Keep instance state even though the original class is unavailable."""
        if isinstance(state, dict):
            nested = state.get("__dict__")
            if isinstance(nested, dict):
                state = nested
            self._state.update(state)

    def __repr__(self) -> str:
        return f"<Unavailable {self.module}.{self.name}>"


class MissingModuleDict(dict):
    """Stand-in for the ``__dict__`` of a module this process cannot import.

    dill stores a function whose ``__globals__`` *is* a module's ``__dict__``
    as a reference to that dict, so a function from a module the inspector
    lacks arrives as ``somemodule.__dict__``. That is a value a reconstructor
    consumes -- ``FunctionType`` requires a real dict for its globals -- and
    not, like most unresolvable references, a class used to rebuild something.
    Standing in with a class raises *while unpickling* and loses the whole
    dump, so this stands in with a dict instead: the function is rebuilt and
    stays readable, and only calling it can fail on a name that is not there.
    """

    def __init__(self, module: str):
        super().__init__(__name__=module)
        self.module = module

    def __repr__(self) -> str:
        return f"<Unavailable {self.module}.__dict__>"


class ValueSnapshot:
    """A value kept as its type, repr and attributes rather than as itself.

    Reached when neither serializer can carry the object across processes: an
    instance of a ``__main__`` class (a pydantic model in the application's
    entry script, say) pickles only as a reference that resolves to nothing in
    the inspector, and dill refuses many framework objects outright. Storing
    the *shape* -- the attributes, each filtered in turn -- keeps the data the
    frame actually held readable, where the alternatives are
    :class:`MissingRef` or one long repr string.

    Attributes are reached normally (``snapshot.user.name``), so inspecting a
    snapshot reads like inspecting the original object.
    """

    __slots__ = ("type_name", "repr_text", "attrs")

    def __init__(self, type_name: str, repr_text: str, attrs: Dict[str, Any]):
        self.type_name = type_name
        self.repr_text = repr_text
        self.attrs = attrs

    def __getattr__(self, name: str) -> Any:
        # Only called for names the slots do not answer, i.e. the captured
        # attributes. Unpickling probes dunders before `attrs` is set, so a
        # missing slot has to read as a plain absent attribute.
        try:
            attrs = object.__getattribute__(self, "attrs")
        except AttributeError:
            raise AttributeError(name) from None
        try:
            return attrs[name]
        except KeyError:
            raise AttributeError(name) from None

    def __dir__(self) -> List[str]:
        return sorted(set(self.__slots__) | set(self.attrs))

    def __repr__(self) -> str:
        if self.repr_text:
            return f"<{self.type_name} snapshot: {self.repr_text}>"
        listed = ", ".join(self.attrs)
        return f"<{self.type_name} snapshot: {listed}>"
