"""Deciding how each captured value is stored.

The interesting question is not "can this be serialized" but "will it still be
there when the dump is opened somewhere else". :class:`_ValueFilter` answers it
per value; see its docstring for the rules.
"""

import pickle
import sys
import warnings
from types import FunctionType, MethodType, ModuleType
from typing import Any, Dict, List, Optional, Set, Tuple

import dill

from .config import CONFIG, SERIALIZERS, _serializer_name
from .model import MissingRef, ValueSnapshot


_MISSING = object()

#: Stands in for the entries an expansion dropped at
#: :attr:`Config.max_expand_items`.
_TRUNCATED = "<truncated>"


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


def _resolve_dill_refs(dump: Any, loads: Any = None) -> None:
    """Replace every :class:`_DillRef` in ``dump`` with its real value.

    Current dumps contain a pickle of individual dill payloads. Older dumps
    contain one dill stream holding a list of values, so both formats are
    accepted here. Loading each current payload separately prevents one
    un-reconstructable object (a common occurrence with web framework graphs)
    from poisoning otherwise perfectly usable functions and simple objects.

    ``loads`` reads one payload. :mod:`.loading` passes its tolerant unpickler,
    so a payload referencing something this process cannot import degrades
    inside the payload -- one absent name -- rather than costing the whole
    value; plain :func:`dill.loads` is the fallback for direct callers.
    """
    if loads is None:
        loads = dill.loads
    blob = getattr(dump, "dill_blob", None)
    if not blob:
        return
    try:
        decoded = loads(blob)
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
                    values.append(loads(payload))
                except Exception as error:
                    values.append(
                        MissingRef("dill", f"value {index} ({type(error).__name__})")
                    )
        else:
            # Format used before payloads were isolated. It is intentionally
            # still supported for dumps already written by older releases.
            values = decoded

    seen: Dict[int, Any] = {}
    for record in getattr(dump, "exceptions", None) or []:
        for frame in getattr(record, "frames", None) or []:
            for scope in (frame.locals, frame.globals):
                for key, value in list(scope.items()):
                    scope[key] = _replace_dill_refs(value, values, seen)


def _dill_value(ref: "_DillRef", values: Any) -> Any:
    """The loaded object a single :class:`_DillRef` stands for."""
    if not isinstance(values, list):
        # The legacy shared blob failed as a whole.
        return values
    if ref.index < len(values):
        return values[ref.index]
    return MissingRef("dill", f"value {ref.index}")


def _replace_dill_refs(value: Any, values: Any, seen: Dict[int, Any]) -> Any:
    """``value`` with every :class:`_DillRef` inside it swapped for its object.

    References do not only sit at the top of a scope: a value the serializer
    expanded (a dict kept element-wise, an object kept as a
    :class:`ValueSnapshot`) holds filtered values of its own, and a lambda
    nested two levels down is exactly the kind of thing dill was needed for.
    ``seen`` both memoizes shared sub-objects and stops a cycle in an expanded
    graph from recursing forever.
    """
    if type(value) is _DillRef:
        return _dill_value(value, values)
    kind = type(value)
    if kind not in (dict, list, tuple, set, frozenset, ValueSnapshot):
        return value
    key = id(value)
    if key in seen:
        return seen[key]
    seen[key] = value
    if kind is dict:
        for item_key, item in list(value.items()):
            value[item_key] = _replace_dill_refs(item, values, seen)
    elif kind is list:
        value[:] = [_replace_dill_refs(item, values, seen) for item in value]
    elif kind is ValueSnapshot:
        value.attrs = {
            name: _replace_dill_refs(item, values, seen)
            for name, item in value.attrs.items()
        }
    else:
        # Immutable containers have to be rebuilt rather than updated.
        rebuilt = kind(_replace_dill_refs(item, values, seen) for item in value)
        seen[key] = rebuilt
        return rebuilt
    return value


#: Pickle writes a global reference as ``module`` + ``qualname``, so a stream
#: mentioning ``__main__`` carries a reference that only resolves in the process
#: that wrote it -- the inspector's ``__main__`` is a different script.
_MAIN_REF = b"__main__"


def _by_value_function(value: Any) -> Any:
    """Make a function look non-locatable so dill writes its code.

    Dill deliberately stores an imported function by module reference, even
    with ``recurse=True``. That is efficient but useless in an offline dump
    after the application module has disappeared. A shallow function copy with
    a private globals dict and ``__main__`` module marker makes dill take the
    function by value while leaving the live application object untouched.
    """
    if not isinstance(value, FunctionType):
        return value
    globals_dict = dict(value.__globals__)
    globals_dict["__name__"] = "__main__"
    copied = FunctionType(
        value.__code__,
        globals_dict,
        value.__name__,
        value.__defaults__,
        value.__closure__,
    )
    copied.__kwdefaults__ = value.__kwdefaults__
    copied.__annotations__ = value.__annotations__
    copied.__dict__.update(value.__dict__)
    copied.__qualname__ = value.__qualname__
    copied.__module__ = "__main__"
    return copied


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
    value_for_dill = _by_value_function(value)
    for options in ({"recurse": True}, {}):
        try:
            # Trial runs are diagnostics: dill warns loudly about values it
            # cannot handle (pydantic models defined in __main__, say), and that
            # noise belongs in the dump, not in the application's log.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                payloads.append(
                    dill.dumps(value_for_dill, protocol=pickle.HIGHEST_PROTOCOL, **options)
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

    ``"snapshot"`` picks per value, on one question: will this still be *there*
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
    * A value neither can carry portably -- and one over
      :attr:`Config.max_dill_bytes` counts as such -- is kept by *shape*
      instead: a container keeps its elements, an object its attributes, each
      filtered by these same rules in turn (:meth:`_expand`). Only what has no
      shape either becomes a repr placeholder. A pickled reference into
      ``__main__`` is never kept as a consolation prize: it reads back as
      :class:`MissingRef`, and where the value is rebuilt *through* the missing
      class, unpickling raises and takes the entire dump with it.

    Decisions are cached by identity: the same object usually appears in
    several frames (and again in every chained exception), so this runs the
    serializer over it once, and reusing one placeholder string per rejected
    object keeps the dump from carrying the same long repr many times over.
    """

    def __init__(self, serializer: Any = None):
        if serializer is not None and not isinstance(serializer, str):
            serializer = getattr(serializer, "__name__", None)  # a module
        self.name = _serializer_name(serializer)
        #: Only "snapshot" chooses per value; the strict modes run one serializer.
        self.snapshot = self.name == "snapshot"
        self.strict = SERIALIZERS[self.name]
        self.max_dill_bytes = CONFIG.max_dill_bytes if self.snapshot else 0
        #: Individual dill payloads, in _DillRef index order. They are wrapped
        #: in one small outer blob only to keep the dump model compact.
        self.dill_payloads: List[bytes] = []
        self.verdicts: Dict[int, Any] = {}
        self.texts: Dict[str, str] = {}
        self.alive: List[Any] = []  # keeps ids unique for the capture's lifetime
        #: Values currently being expanded, so a cycle terminates.
        self._expanding: Set[int] = set()
        #: Verdicts for values reached inside an expansion, by (id, depth).
        self._nested_verdicts: Dict[Tuple[int, int], Any] = {}

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
        return self._snapshot_decide(value) if self.snapshot else self._strict_decide(value)

    def _strict_decide(self, value: Any) -> Any:
        """One serializer, take it or leave it: the ``"dill"``/``"pickle"`` modes."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.strict.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            return None
        except Exception:
            return self._placeholder(value)

    def _snapshot_decide(self, value: Any) -> Any:
        if isinstance(value, ModuleType):
            name = getattr(value, "__name__", None)
            if name:
                return _ModuleRef(name)

        # A successful pickle of an application object is only a reference to
        # its class. That reference is not portable: the inspector commonly
        # runs after a deploy, without the application's package on its path.
        # Keep the state by shape before accepting such a reference. This is
        # deliberately limited to stateful instances; modules, functions and
        # ordinary library values still use their normal serializer paths.
        expanded = self._expand_application_object(value)
        if expanded is not _MISSING:
            return expanded
        # Dill also preserves imported functions by reference unless their
        # module is made unavailable to its locator. Force functions through
        # the by-value payload path so snapshot mode does not create a dangling
        # reference merely because plain pickle accepted it.
        if isinstance(value, FunctionType):
            blob = _dill_payload(value)
            if blob is not None and (
                not self.max_dill_bytes or len(blob) <= self.max_dill_bytes
            ):
                self.dill_payloads.append(blob)
                return _DillRef(len(self.dill_payloads) - 1)

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

        # Neither serializer can carry this value across processes. Store its
        # shape instead: a container keeps its elements, an object keeps its
        # attributes, and each of those is filtered in turn, so only the parts
        # that genuinely cannot travel degrade.
        expanded = self._expand(value, 1)
        if expanded is not _MISSING:
            return expanded
        # `payload` may exist here, but it is a reference into a ``__main__``
        # that the inspector does not have. Storing it anyway is not merely
        # useless -- it reads back as MissingRef at best, and a reference
        # pickle rebuilds a value *through* (a pydantic model class, say)
        # raises while unpickling and takes the whole dump with it. A repr
        # says more and always loads.
        return self._placeholder(value)

    def _expand(self, value: Any, depth: int) -> Any:
        """Structural stand-in for ``value``, or ``_MISSING`` if it has none.

        Only reached for values neither pickle nor dill could store portably,
        and only down to :attr:`Config.max_expand_depth`: expansion trades dump
        size for readability, and a framework object's attribute graph reaches
        the whole process if nothing stops it. ``_expanding`` guards cycles,
        which are the norm in exactly the object graphs that get here (a
        request pointing at a session pointing back at the request).
        """
        if depth > CONFIG.max_expand_depth:
            return _MISSING
        key = id(value)
        if key in self._expanding:
            return self._placeholder(value)
        self._expanding.add(key)
        try:
            kind = type(value)
            if kind in (dict, list, tuple, set, frozenset):
                return self._expand_container(value, kind, depth)
            attrs = self._attributes(value)
            if attrs is None:
                return _MISSING
            return ValueSnapshot(
                kind.__name__,
                self._repr(value),
                {name: self._nested(item, depth) for name, item in attrs},
            )
        finally:
            self._expanding.discard(key)

    def _expand_container(self, value: Any, kind: type, depth: int) -> Any:
        """``value`` rebuilt with each element filtered on its own."""
        limit = CONFIG.max_expand_items
        if kind is dict:
            items = list(value.items())
            kept = items[:limit] if limit else items
            result = {
                self._nested(item_key, depth): self._nested(item, depth)
                for item_key, item in kept
            }
            if limit and len(items) > limit:
                result[f"<+{len(items) - limit} more entries>"] = _TRUNCATED
            return result
        elements = list(value)
        kept = elements[:limit] if limit else elements
        rebuilt = [self._nested(item, depth) for item in kept]
        if limit and len(elements) > limit:
            rebuilt.append(f"<+{len(elements) - limit} more items>")
        return kind(rebuilt)

    def _nested(self, value: Any, depth: int) -> Any:
        """Filter a value found *inside* an expansion.

        Cached on identity *and* depth, not identity alone as :meth:`value` is:
        what a member becomes depends on how deep it sits, so a shared object
        must not have its first sighting decide every later one. Caching at all
        matters because attribute graphs are rarely trees -- a request reached
        from ten places would otherwise be run through both serializers ten
        times over.
        """
        key = (id(value), depth)
        verdict = self._nested_verdicts.get(key, _MISSING)
        if verdict is not _MISSING:
            return verdict
        self.alive.append(value)
        verdict = self._nested_verdict(value, depth)
        self._nested_verdicts[key] = verdict
        return verdict

    def _nested_verdict(self, value: Any, depth: int) -> Any:
        """What :meth:`_nested` stores for ``value``, uncached."""
        if self.snapshot:
            expanded = self._expand_application_object(value, depth)
            if expanded is not _MISSING:
                return expanded
            if isinstance(value, FunctionType):
                blob = _dill_payload(value)
                if blob is not None and (
                    not self.max_dill_bytes or len(blob) <= self.max_dill_bytes
                ):
                    self.dill_payloads.append(blob)
                    return _DillRef(len(self.dill_payloads) - 1)
            if isinstance(value, ModuleType):
                name = getattr(value, "__name__", None)
                if name:
                    return _ModuleRef(name)
            payload = _pickle_payload(value)
            if payload is not None and _MAIN_REF not in payload:
                return value
            blob = _dill_payload(value)
            if blob is not None and (
                not self.max_dill_bytes or len(blob) <= self.max_dill_bytes
            ):
                self.dill_payloads.append(blob)
                return _DillRef(len(self.dill_payloads) - 1)
            expanded = self._expand(value, depth + 1)
            if expanded is not _MISSING:
                return expanded
            return self._placeholder(value)
        verdict = self._strict_decide(value)
        return value if verdict is None else verdict

    def _expand_application_object(self, value: Any, depth: int = 1) -> Any:
        """Snapshot a stateful user object instead of storing a class reference.

        Pickle and dill both represent a normally importable class instance by
        module and class name. That is compact, but it becomes ``MissingRef``
        after the application is redeployed or inspected on another machine.
        The instance state is the useful part of a crash dump, so preserve it
        structurally before either serializer gets to choose that reference.
        """
        if isinstance(value, (ModuleType, FunctionType, MethodType, type)):
            return _MISSING
        attrs = self._attributes(value)
        if attrs is None:
            return _MISSING
        return self._expand(value, depth)

    @staticmethod
    def _attributes(value: Any) -> Optional[List[Any]]:
        """``(name, value)`` pairs describing ``value``, or ``None`` if it has none.

        ``__dict__`` covers ordinary objects and pydantic models alike; slotted
        classes keep their state outside it, so those are read per slot.
        """
        instance_dict = getattr(value, "__dict__", None)
        if isinstance(instance_dict, dict) and instance_dict:
            return list(instance_dict.items())
        pairs = []
        for kind in type(value).__mro__:
            for name in getattr(kind, "__slots__", ()) or ():
                if isinstance(name, str) and hasattr(value, name):
                    pairs.append((name, getattr(value, name)))
        return pairs or None

    @staticmethod
    def _repr(value: Any) -> str:
        """Capped repr of ``value``, or ``""`` if it has no usable one."""
        try:
            text = repr(value)
        except Exception:
            return ""
        limit = CONFIG.max_repr_chars
        if len(text) > limit:
            text = f"{text[:limit]}... (+{len(text) - limit} chars)"
        return text

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
