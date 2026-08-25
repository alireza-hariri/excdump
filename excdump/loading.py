"""Reading a dump back, tolerantly.

A dump is a snapshot of a process that no longer exists, so loading one must
degrade rather than fail: a class that cannot be imported here becomes a
visible placeholder instead of making the whole file unreadable.
"""

import gzip
import io
import os
import pickle
from typing import Any

import dill

from .model import ExceptionDump, MissingModuleDict, MissingRef, _normalize_dump
from .values import _resolve_dill_refs


def _tolerant_unpickler(module: Any) -> Any:
    class Unpickler(module.Unpickler):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self._missing_classes = {}

        def find_class(self, module_name: str, name: str) -> Any:
            try:
                return super().find_class(module_name, name)
            except Exception:
                if name == "__dict__":
                    # A module's own dict, referenced by a dill-stored function
                    # as its globals. Only a dict can play that role.
                    return MissingModuleDict(module_name)
                # A missing global can be either a value or the class used by
                # NEWOBJ/NEWOBJ_EX to rebuild a value.  Returning a MissingRef
                # instance works for the former, but pickle requires the
                # latter to be a type and otherwise raises before we can
                # degrade the value.  Return a short-lived type whose __new__
                # turns the reconstructed object into the usual placeholder.
                key = (module_name, name)
                missing = self._missing_classes.get(key)
                if missing is None:
                    def missing_new(cls: Any, *args: Any, **kwargs: Any) -> MissingRef:
                        return MissingRef(module_name, name)

                    missing = type(
                        "MissingRef",
                        (MissingRef,),
                        {"__new__": missing_new},
                    )
                    self._missing_classes[key] = missing
                return missing

    return Unpickler


def _tolerant_loads(payload: bytes) -> Any:
    """Load one dill payload, degrading names this process cannot import."""
    return _tolerant_unpickler(dill)(io.BytesIO(payload)).load()


def load_dump(filepath: str) -> ExceptionDump:
    """Load a dump written by either serializer (and older uncompressed ones).

    dill reads plain pickle streams, so it is tried first and pickle is the
    fallback for environments where dill is unavailable or unhappy.
    """
    with open(filepath, "rb") as raw:
        compressed = raw.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else open
    errors = []
    for module in (dill, pickle):
        try:
            with opener(filepath, "rb") as f:
                dump = _normalize_dump(_tolerant_unpickler(module)(f).load())
        except Exception as error:
            errors.append(f"{module.__name__}: {error}")
            continue
        _resolve_dill_refs(dump, _tolerant_loads)
        # The dump names its source by hash; the blobs sit next to it.
        sources = getattr(dump, "sources", None)
        if sources is not None and hasattr(sources, "attach"):
            sources.attach(os.path.dirname(os.path.abspath(filepath)))
        return dump
    raise ValueError(f"could not load dump {filepath} ({'; '.join(errors)})")
