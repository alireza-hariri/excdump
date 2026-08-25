"""Deciding how each captured value is stored.

The interesting question is not "can this be serialized" but "will it still be
there when the dump is opened somewhere else". :class:`_ValueFilter` answers it
per value; see its docstring for the rules.
"""

import pickle
import sys
import warnings
from types import ModuleType
from typing import Any, Dict, List, Optional

import dill

from .config import CONFIG, SERIALIZERS, _serializer_name
from .model import MissingRef


_MISSING = object()


class _ModuleRef:
    """A module captured by name rather than by content.

    Frames routinely hold imported modules in their globals. Plain pickle
    refuses them outright, and dill serializes a ``__main__``-reachable module
    *by value* -- one mid-sized module costs about 6 KB that way, in every dump
    that captures a frame importing it. A module is fully
    described by its name, and rebuilding it from that gives the inspector the
    real module rather than dill's reconstructed copy.
    """

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __reduce__(self):
        return (_load_module_ref, (self.name,))


def _load_module_ref(name: str) -> Any:
    """Re-import a module captured by name while a dump is being unpickled."""
    if name == "__main__":
        # Resolving this would hand back the *inspector's* __main__, which has
        # nothing to do with the module the dump captured.
        return MissingRef(name, "<module>")
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    try:
        import importlib

        return importlib.import_module(name)
    except Exception:
        return MissingRef(name, "<module>")


class _DillRef:
    """Placeholder for a value that travels in the dump's dill payloads.

    Every value dill has to handle goes into a small, independently loadable
    payload. The frames hold lightweight references, and
    :func:`_resolve_dill_refs` swaps in the real objects after the dump loads.
    Keeping payloads independent is important: framework objects often contain
    cycles or callbacks that dill can write but cannot reconstruct. One such
    value must not make every other function in the frame unreadable.
    """

    __slots__ = ("index",)

    def __init__(self, index: int):
        self.index = index

    def __repr__(self) -> str:
        return f"<pending dill value {self.index}>"


def _resolve_dill_refs(dump: Any) -> None:
    """Replace every :class:`_DillRef` in ``dump`` with its real value.

    Current dumps contain a pickle of individual dill payloads. Older dumps
    contain one dill stream holding a list of values, so both formats are
    accepted here. Loading each current payload separately prevents one
    un-reconstructable object (a common occurrence with web framework graphs)
    from poisoning otherwise perfectly usable functions and simple objects.
    """
    blob = getattr(dump, "dill_blob", None)
    if not blob:
        return
    try:
        decoded = dill.loads(blob)
    except Exception as error:
        values: Any = MissingRef("dill", f"blob ({type(error).__name__})")
    else:
        if (
            isinstance(decoded, tuple)
            and len(decoded) == 2
            and decoded[0] == "excdump-dill-values-v2"
            and isinstance(decoded[1], list)
        ):
            values = []
            for index, payload in enumerate(decoded[1]):
                try:
                    values.append(dill.loads(payload))
                except Exception as error:
                    values.append(
                        MissingRef("dill", f"value {index} ({type(error).__name__})")
                    )
        else:
            # Format used before payloads were isolated. It is intentionally
            # still supported for dumps already written by older releases.
            values = decoded

    for record in getattr(dump, "exceptions", None) or []:
        for frame in getattr(record, "frames", None) or []:
            for scope in (frame.locals, frame.globals):
                for key, value in list(scope.items()):
                    if type(value) is _DillRef:
                        if isinstance(values, list) and value.index < len(values):
                            scope[key] = values[value.index]
                        elif not isinstance(values, list):
                            # The legacy shared blob failed as a whole.
                            scope[key] = values
                        else:
                            scope[key] = MissingRef("dill", f"value {value.index}")


#: Pickle writes a global reference as ``module`` + ``qualname``, so a stream
#: mentioning ``__main__`` carries a reference that only resolves in the process
#: that wrote it -- the inspector's ``__main__`` is a different script.
_MAIN_REF = b"__main__"


def _dill_payload(value: Any) -> Optional[bytes]:
    """Smallest dill encoding of ``value``, or ``None`` if dill refused it.

    ``recurse=True`` narrows a captured function down to the globals it
    actually references. For a single function whose ``__globals__`` belong to
    some other module that is a large win -- 3 KB rather than 9 KB for a
    decorated wrapper. For a *batch* of functions defined in one module it is a
    large loss, because the plain form lets dill memoize that module's
    namespace once instead of expanding it for every function: the same five
    functions cost 9.8 KB recursed and 1.2 KB not.

    Neither setting wins in general, so both are tried and the smaller kept.
    Only values pickle already rejected get this far, so the doubled work is
    bounded by how much dill was needed in the first place. Each candidate is
    also loaded once before it is accepted: dill can write some framework
    graphs that it cannot reconstruct. The selected bytes are retained and
    loaded independently after capture; this is deliberate, since one bad
    framework object must not poison unrelated functions.
    """
    payloads = []
    for options in ({"recurse": True}, {}):
        try:
            # Trial runs are diagnostics: dill warns loudly about values it
            # cannot handle (pydantic models defined in __main__, say), and that
            # noise belongs in the dump, not in the application's log.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                payloads.append(
                    dill.dumps(value, protocol=pickle.HIGHEST_PROTOCOL, **options)
                )
        except Exception:
            continue
    for payload in sorted(payloads, key=len):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                dill.loads(payload)
        except Exception:
            continue
        return payload
    return None


def _pickle_payload(value: Any) -> Optional[bytes]:
    """Plain pickle bytes for ``value``, or ``None`` if pickle refused it."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        return None


class _ValueFilter:
    """Stores each captured value with the cheapest thing that can hold it.

    ``"auto"`` picks per value, on one question: will this still be *there*
    when the dump is opened somewhere else?

    * A module is stored as its name (:class:`_ModuleRef`). Pickle refuses
      modules outright and dill copies a ``__main__``-reachable one by value --
      about 6 KB for a mid-sized one -- when the name alone rebuilds the real
      module.
    * Anything plain pickle can store *and* that does not reference
      ``__main__`` is stored by pickle, which is far smaller than dill: a
      function in an imported module costs about 40 bytes by reference, and it
      resolves in the inspector because the module is importable there too.
    * Everything else goes through dill by value, as a :class:`_DillRef`
      blob: lambdas, local classes and closures, which pickle cannot take at
      all, and anything defined in ``__main__``, whose pickled reference would
      resolve against the *inspector's* ``__main__`` and come back as
      :class:`MissingRef`. Storing those costs a few hundred bytes each and is
      what keeps a script's own functions inspectable offline.
    * If dill's blob is bigger than :attr:`Config.max_dill_bytes`, the pickled
      form is kept anyway where one exists, and only a value neither can hold
      becomes a repr placeholder.

    Decisions are cached by identity: the same object usually appears in
    several frames (and again in every chained exception), so this runs the
    serializer over it once, and reusing one placeholder string per rejected
    object keeps the dump from carrying the same long repr many times over.
    """

    def __init__(self, serializer: Any = None):
        if serializer is not None and not isinstance(serializer, str):
            serializer = getattr(serializer, "__name__", None)  # a module
        self.name = _serializer_name(serializer)
        #: Only "auto" chooses per value; the strict modes run one serializer.
        self.auto = self.name == "auto"
        self.strict = SERIALIZERS[self.name]
        self.max_dill_bytes = CONFIG.max_dill_bytes if self.auto else 0
        #: Individual dill payloads, in _DillRef index order. They are wrapped
        #: in one small outer blob only to keep the dump model compact.
        self.dill_payloads: List[bytes] = []
        self.verdicts: Dict[int, Any] = {}
        self.texts: Dict[str, str] = {}
        self.alive: List[Any] = []  # keeps ids unique for the capture's lifetime

    def filter(self, data: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        for key, value in data.items():
            if key == "__builtins__":
                continue
            result[sys.intern(key) if type(key) is str else key] = self.value(value)
        return result

    def value(self, value: Any) -> Any:
        verdict = self.verdicts.get(id(value), _MISSING)
        if verdict is _MISSING:
            verdict = self._decide(value)
            self.alive.append(value)
            self.verdicts[id(value)] = verdict
        return value if verdict is None else verdict

    def _decide(self, value: Any) -> Any:
        """``None`` to store the value itself, else what replaces it."""
        return self._auto_decide(value) if self.auto else self._strict_decide(value)

    def _strict_decide(self, value: Any) -> Any:
        """One serializer, take it or leave it: the ``"dill"``/``"pickle"`` modes."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.strict.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            return None
        except Exception:
            return self._placeholder(value)

    def _auto_decide(self, value: Any) -> Any:
        if isinstance(value, ModuleType):
            name = getattr(value, "__name__", None)
            if name:
                return _ModuleRef(name)

        payload = _pickle_payload(value)
        if payload is not None and _MAIN_REF not in payload:
            return None  # Small, and it will resolve wherever the dump is read.

        # Either pickle refused it, or pickle would only store a pointer into a
        # __main__ that will not exist in the inspector. Keep the value itself.
        blob = _dill_payload(value)
        if blob is not None and (
            not self.max_dill_bytes or len(blob) <= self.max_dill_bytes
        ):
            # Retain the successful payload itself. Do not serialize all
            # values into one dill graph: a single cyclic framework object can
            # otherwise make unrelated functions impossible to load.
            self.dill_payloads.append(blob)
            return _DillRef(len(self.dill_payloads) - 1)
        if payload is not None:
            # dill is too expensive here; the pickled reference is better than
            # nothing, even though it may read back as MissingRef.
            return None
        return self._placeholder(value)

    def dill_blob(self) -> Optional[bytes]:
        """Return independently loadable dill payloads, or ``None``.

        The outer stream contains bytes, not the captured objects themselves.
        Consequently a value that dill cannot reconstruct cannot invalidate the
        payloads for all the other values in the dump.
        """
        if not self.dill_payloads:
            return None
        return dill.dumps(
            ("excdump-dill-values-v2", self.dill_payloads),
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    def _placeholder(self, value: Any) -> str:
        try:
            text = repr(value)
        except Exception:
            text = "<unrepresentable>"
        limit = CONFIG.max_repr_chars
        if len(text) > limit:
            text = f"{text[:limit]}... (+{len(text) - limit} chars)"
        text = f"<Unserializable {type(value).__name__}: {text}>"
        return self.texts.setdefault(text, text)
