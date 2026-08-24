"""Reading a dump back, tolerantly.

A dump is a snapshot of a process that no longer exists, so loading one must
degrade rather than fail: a class that cannot be imported here becomes a
visible placeholder instead of making the whole file unreadable.
"""

import gzip
import os
import pickle
from typing import Any

import dill

from .model import ExceptionDump, MissingRef, _normalize_dump
from .values import _resolve_dill_refs


def _tolerant_unpickler(module: Any) -> Any:
    class Unpickler(module.Unpickler):
        def find_class(self, module_name: str, name: str) -> Any:
            try:
                return super().find_class(module_name, name)
            except Exception:
                return MissingRef(module_name, name)

    return Unpickler


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
        _resolve_dill_refs(dump)
        # The dump names its source by hash; the blobs sit next to it.
        sources = getattr(dump, "sources", None)
        if sources is not None and hasattr(sources, "attach"):
            sources.attach(os.path.dirname(os.path.abspath(filepath)))
        return dump
    raise ValueError(f"could not load dump {filepath} ({'; '.join(errors)})")
