"""Process-wide capture settings and the serializer selection.

Everything here is read at capture time, so a deployment configures once --
through :func:`configure`, or ``EXCDUMP_*`` in the environment -- and the
rest of the package needs no arguments.
"""

import logging
import os
import pickle
from types import ModuleType
from typing import Callable, ClassVar, Dict, Literal, Optional, TypeVar, Union, cast

import dill
from pydantic import BaseModel, ConfigDict, Field


# -- global configuration ----------------------------------------------------

SerializerName = Literal["auto", "dill", "pickle"]


#: Module that writes the outer dump stream for each serializer setting.
#: ``"auto"`` writes plain pickle and reaches for dill only per value, so its
#: stream is a pickle stream (see :class:`_ValueFilter`).
SERIALIZERS: Dict[SerializerName, ModuleType] = {
    "auto": pickle, "dill": dill, "pickle": pickle,
}


#: Environment overrides, so a deployment can tune capture without code changes.
ENV_PREFIX = "EXCDUMP_"


T = TypeVar("T")


def _env(name: str, default: T, cast: Callable[[str], T] = str) -> T:
    """Read ``EXCDUMP_<name>``, falling back to ``default`` if unusable.

    A malformed environment variable must not stop the application from
    starting, so a bad value is ignored rather than raised.
    """
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(raw: str) -> bool:
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_serializer(raw: str) -> SerializerName:
    name = raw.strip().lower()
    if name not in SERIALIZERS:
        raise ValueError(name)
    return cast(SerializerName, name)


class Config(BaseModel):
    """Process-wide capture defaults, validated on every assignment.

    Every :func:`dump_exception` argument defaults to the matching field here,
    so application code can call ``dump_exception()`` with no arguments and
    still get the deployment's chosen depth, serializer, and retention.
    """

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    #: Directory holding the dump store. Dumps are grouped by exception path.
    store_dir: str = Field(
        default_factory=lambda: _env("DIR", os.path.join(os.getcwd(), ".exception_dumps")),
        min_length=1,
        description="Root of the dump store.",
    )
    #: How many dumps to keep for one exception path; the oldest are deleted.
    max_dumps_per_path: int = Field(
        default_factory=lambda: _env("MAX_PER_PATH", 200, int),
        ge=0,
        description="Per-path retention; 0 disables pruning.",
    )
    #: How long a whole exception path outlives its last occurrence. Retention
    #: bounds the dumps *within* a path but not the number of paths, and every
    #: deploy strands a generation of them: a path id hashes line numbers, so
    #: editing a file makes each traceback through it hash differently and the
    #: old ids unreachable. Age is what tells those apart from live failures --
    #: a path nobody has hit in weeks is a fixed bug or deleted code. Applied
    #: by ``gc``, and by capture itself at most once per ``gc_interval_seconds``.
    max_path_age_days: float = Field(
        default_factory=lambda: _env("MAX_PATH_AGE_DAYS", 14.0, float),
        ge=0,
        description="Age at which gc drops an untouched path; 0 keeps paths forever.",
    )
    #: How often capture sweeps dead paths itself, so a long-running service
    #: does not depend on someone remembering to run ``gc``. Time, not a share
    #: of captures: sweeping every thousandth exception would sweep constantly
    #: under a failure storm and never at all in a service that fails twice a
    #: week. Only the age rule runs here -- it costs one directory listing per
    #: path, where reclaiming source blobs means loading every dump and stays
    #: in the command.
    gc_interval_seconds: float = Field(
        default_factory=lambda: _env("GC_INTERVAL_SECONDS", 3600.0, float),
        ge=0,
        description="Seconds between automatic sweeps during capture; 0 disables them.",
    )
    #: Caller frames captured above the handling frame.
    n_depth_up: int = Field(default_factory=lambda: _env("DEPTH_UP", 5, int), ge=0)
    #: Traceback frames captured below the handling frame.
    n_depth_down: int = Field(default_factory=lambda: _env("DEPTH_DOWN", 5, int), ge=0)
    #: ``"auto"`` (pickle per value, dill only where pickle fails), ``"dill"``
    #: (captures more, much larger) or ``"pickle"`` (smallest, drops what
    #: pickle cannot take).
    serializer: SerializerName = Field(
        default_factory=lambda: _env("SERIALIZER", "auto", _env_serializer)
    )
    #: Lines of source kept above and below each captured line.
    source_radius: int = Field(default_factory=lambda: _env("SOURCE_RADIUS", 5, int), ge=0)
    #: Unserializable values are stored as a repr capped at this length.
    max_repr_chars: int = Field(default_factory=lambda: _env("MAX_REPR_CHARS", 2000, int), ge=0)
    #: Backstop on a single dill-serialized value. dill stores anything defined
    #: in ``__main__`` by value, so one closure can drag a module's whole code
    #: into the dump; past this many bytes the value becomes a repr placeholder
    #: instead. 0 removes the cap.
    max_dill_bytes: int = Field(
        default_factory=lambda: _env("MAX_DILL_BYTES", 65536, int), ge=0
    )
    #: How deep ``"auto"`` expands a value neither serializer can carry
    #: portably. Expansion keeps such a value readable -- an object becomes its
    #: attributes, a container its elements -- instead of a repr string or a
    #: MissingRef, but an attribute graph reaches the whole process if nothing
    #: stops it, so it is bounded. 0 disables expansion.
    max_expand_depth: int = Field(
        default_factory=lambda: _env("MAX_EXPAND_DEPTH", 3, int), ge=0
    )
    #: Elements kept per container while expanding; the rest become a count.
    #: Bounds what one large request body or query result adds to a dump.
    #: 0 keeps every element.
    max_expand_items: int = Field(
        default_factory=lambda: _env("MAX_EXPAND_ITEMS", 100, int), ge=0
    )
    #: Largest file whose full text is kept in the per-path source sidecar.
    #: Bigger files fall back to the line window inside the dump, which still
    #: shows the failing lines but cannot be scrolled. 0 stores every file.
    max_source_bytes: int = Field(
        default_factory=lambda: _env("MAX_SOURCE_BYTES", 1_000_000, int), ge=0
    )
    #: Set to False to turn capture into a no-op without removing the calls.
    enabled: bool = Field(default_factory=lambda: _env("ENABLED", True, _env_bool))
    #: Called with every trace id produced, for logging or alerting.
    on_dump: Optional[Callable[[str], None]] = None
    #: Print a line to stderr for each dump (useful in development).
    verbose: bool = Field(default_factory=lambda: _env("VERBOSE", False, _env_bool))


CONFIG = Config()


class Unset:
    """Sentinel for :func:`configure`: leave this option as it is.

    ``None`` cannot play that role, because it is the meaningful value that
    clears :attr:`Config.on_dump`.
    """

    _instance: ClassVar[Optional["Unset"]] = None

    def __new__(cls) -> "Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = Unset()


def configure(
    *,
    store_dir: Union[str, Unset] = UNSET,
    max_dumps_per_path: Union[int, Unset] = UNSET,
    max_path_age_days: Union[float, Unset] = UNSET,
    gc_interval_seconds: Union[float, Unset] = UNSET,
    n_depth_up: Union[int, Unset] = UNSET,
    n_depth_down: Union[int, Unset] = UNSET,
    serializer: Union[SerializerName, Unset] = UNSET,
    source_radius: Union[int, Unset] = UNSET,
    max_repr_chars: Union[int, Unset] = UNSET,
    max_expand_depth: Union[int, Unset] = UNSET,
    max_expand_items: Union[int, Unset] = UNSET,
    enabled: Union[bool, Unset] = UNSET,
    on_dump: Union[Callable[[str], None], None, Unset] = UNSET,
    verbose: Union[bool, Unset] = UNSET,
) -> Config:
    """Update :data:`CONFIG` in place and return it.

    ``configure(store_dir="/var/log/dumps", max_dumps_per_path=200)``

    Options left out keep their current value; every option passed is validated
    by :class:`Config`, so a typo or an out-of-range depth fails here rather
    than at capture time, when an exception is already in flight.
    """
    updates = {name: value for name, value in locals().items() if not isinstance(value, Unset)}
    for name, value in updates.items():
        setattr(CONFIG, name, value)
    return CONFIG


def set_serializer(name: SerializerName) -> None:
    """Select how new dumps serialize captured values.

    ``"auto"`` (the default) tries plain pickle for each value and falls back
    to dill only for the ones pickle rejects, which is both the smallest option
    that loses nothing pickle could have kept and much smaller than ``"dill"``
    -- see :class:`_ValueFilter` for why dill is expensive.

    ``"dill"`` runs everything through dill, capturing lambdas, local classes
    and ``__main__``-defined objects by value at a large size cost. ``"pickle"``
    is strict pickle: values dill could have handled become repr placeholders.
    Dumps written any of the three ways are loaded by :func:`load_dump`
    transparently.
    """
    _serializer_module(name)  # validates
    CONFIG.serializer = name


def get_serializer() -> SerializerName:
    """Name of the serializer currently used for new dumps."""
    return CONFIG.serializer


def _serializer_name(name: Optional[str] = None) -> SerializerName:
    """Validate a serializer name, defaulting to the configured one."""
    chosen = name or CONFIG.serializer
    if chosen not in SERIALIZERS:
        raise ValueError(f"unknown serializer {chosen!r}; use one of {sorted(SERIALIZERS)}")
    return cast(SerializerName, chosen)


def _serializer_module(name: Optional[str] = None) -> ModuleType:
    """The module writing the outer dump stream."""
    return SERIALIZERS[_serializer_name(name)]


logger = logging.getLogger("excdump")
