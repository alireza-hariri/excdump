"""The command line and the plain readline inspector.

``inspect`` opens a dump, ``list`` shows what the store holds, ``gc`` drops
paths nothing hits any more, and ``demo`` writes a dump so there is something
to look at.
"""

import sys
from typing import Dict, List, Optional, Tuple

from .capture import dump_exception
from .config import CONFIG, configure, set_serializer
from .model import ExceptionDump
from .session import DebuggerSession
from .store import DumpStore, resolve_dump
from .loading import load_dump


COMMANDS: List[Tuple[str, Tuple[str, ...], str]] = [
    ("/help", ("/h", "/?"), "Show this help"),
    ("/list", ("/l",), "Show source around the current line ( /list N for radius )"),
    ("/where", ("/w", "/bt"), "Show the captured frame stack"),
    ("/up", ("/u",), "Move to the calling frame ( /up N )"),
    ("/down", ("/d",), "Move to the called frame ( /down N )"),
    ("/frame", ("/f",), "Jump to frame by index: /frame 3"),
    ("/exceptions", ("/exc",), "List the captured exception chain"),
    ("/exception-up", ("/eup",), "Move to the older chained exception"),
    ("/exception-down", ("/edown",), "Move to the newer chained exception"),
    ("/exception", (), "Jump to a chained exception by index: /exception 0"),
    ("/traceback", ("/tb",), "Print the original formatted traceback"),
    ("/locals", ("/loc",), "Print locals of the current frame"),
    ("/globals", ("/glob",), "Print globals referenced by the current frame"),
    ("/print", ("/p",), "Evaluate an expression: /print cart['prices']"),
    ("/pp", (), "Pretty-print an expression"),
    ("/clear", (), "Clear the output pane"),
    ("/quit", ("/q", "/exit"), "Exit the debugger"),
]


ALIAS_MAP: Dict[str, str] = {}


for _name, _aliases, _ in COMMANDS:
    ALIAS_MAP[_name] = _name
    for _alias in _aliases:
        ALIAS_MAP[_alias] = _name


# Bare pdb-style shorthands that would never be useful as expressions.
BARE_ALIASES = {
    "u": "/up",
    "d": "/down",
    "w": "/where",
    "bt": "/where",
    "l": "/list",
    "q": "/quit",
    "exit": "/quit",
    "?": "/help",
}


def help_text() -> str:
    lines = ["Commands:"]
    for name, aliases, description in COMMANDS:
        shown = ", ".join((name,) + aliases)
        lines.append(f"  {shown:<34} {description}")
    lines.append("")
    lines.append("Anything that is not a /command is evaluated as Python in the current frame.")
    lines.append("Keys: Up/Down = command history, Alt-Up/Alt-Down = frame up/down,")
    lines.append("      Alt-Left/Alt-Right = exception up/down, PgUp/PgDn = scroll source,")
    lines.append("      Tab = complete, Ctrl-D = quit.")
    return "\n".join(lines)


def dispatch(session: DebuggerSession, line: str) -> Tuple[str, bool]:
    """Run one input line. Returns ``(output, should_quit)``."""
    line = line.strip()
    if not line:
        return "", False

    head, _, rest = line.partition(" ")
    rest = rest.strip()

    if not head.startswith("/"):
        mapped = BARE_ALIASES.get(head)
        if mapped is None:
            return session.eval_expr(line), False
        head = mapped

    command = ALIAS_MAP.get(head)
    if command is None:
        return f"*** Unknown command {head}. Try /help.", False

    def count() -> int:
        try:
            return max(1, int(rest))
        except ValueError:
            return 1

    if command == "/help":
        return help_text(), False
    if command == "/quit":
        return "", True
    if command == "/list":
        try:
            radius = int(rest) if rest else 5
        except ValueError:
            radius = 5
        return session.list_source(radius), False
    if command == "/where":
        return session.where(), False
    if command == "/up":
        return session.frame_up(count()), False
    if command == "/down":
        return session.frame_down(count()), False
    if command == "/frame":
        try:
            return session.goto_frame(int(rest)), False
        except ValueError:
            return "*** Usage: /frame <index>", False
    if command == "/exceptions":
        rows = session.exceptions_listing()
        lines = ["Exception chain (oldest first):"]
        for index, text, current in rows:
            lines.append(f"{'-> ' if current else '   '}[{index}] {text}")
        return "\n".join(lines), False
    if command == "/exception-up":
        return session.exception_up(), False
    if command == "/exception-down":
        return session.exception_down(), False
    if command == "/exception":
        try:
            return session.goto_exception(int(rest)), False
        except ValueError:
            return "*** Usage: /exception <index>", False
    if command == "/traceback":
        return session.record.formatted_tb.rstrip(), False
    if command == "/locals":
        return session.locals_text(), False
    if command == "/globals":
        return session.globals_text(), False
    if command == "/print":
        return (session.eval_expr(rest) if rest else "*** Usage: /print <expr>"), False
    if command == "/pp":
        return (session.eval_expr(rest, pretty=True) if rest else "*** Usage: /pp <expr>"), False
    if command == "/clear":
        return "\x00clear", False
    return f"*** Command {command} is not implemented.", False


def plain_loop(session: DebuggerSession) -> None:
    """Readline-based fallback used when the TUI cannot run (e.g. no tty)."""
    try:
        import readline  # noqa: F401  (enables history and line editing)
    except ImportError:
        pass

    print("=" * 70)
    print(f"Offline debugger: {session.record.title()}")
    if session.dump.trace_id:
        print(f"trace id: {session.dump.trace_id}")
    if session.dump.metadata:
        print(f"metadata: {session.dump.metadata}")
    print("Type /help for commands.")
    print("=" * 70)
    print(session.frame_header())
    print(session.list_source())

    while True:
        try:
            line = input("(exc-dbg) ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        output, quit_now = dispatch(session, line)
        if quit_now:
            return
        if output == "\x00clear":
            continue
        if output:
            print(output)


def load_and_debug(target: Optional[str] = None, plain: bool = False,
                   store: Optional[DumpStore] = None) -> None:
    """Open the inspector on a trace id, id prefix, path id, or dump file."""
    filepath = resolve_dump(target, store)
    session = DebuggerSession(load_dump(filepath))
    if plain or not sys.stdin.isatty() or not sys.stdout.isatty():
        plain_loop(session)
        return
    try:
        from .tui import run_tui
    except ImportError:
        plain_loop(session)
        return
    run_tui(session, filepath)


# Backwards-compatible name for the previous text-only inspector.
class OfflinePdb:
    def __init__(self, dump: ExceptionDump):
        self.session = DebuggerSession(dump)

    def cmdloop(self) -> None:
        plain_loop(self.session)


USAGE = """Usage:
  python -m excdump inspect [<trace-id> | <path-id> | <file>] [--plain]
  python -m excdump inspect --trace-id <trace-id>
  python -m excdump list [<path-id>]
  python -m excdump gc [<path-id>]
  python -m excdump demo

The trace id is the value returned by dump_exception() (and handed to the
on_dump callback), so a logged id goes straight into the inspector. A unique
id prefix, a path id (opens that path's newest dump), or no argument at all
(opens the store's newest dump) work too.

Options:
  --trace-id <id> Trace id to inspect, when a flag reads better than a positional
  --store <dir>   Dump store to use (default: $EXCDUMP_DIR or ./.exception_dumps)
  --pickle        Write dumps with strict pickle instead of the default "auto"
                  (per-value pickle, dill only where pickle fails; demo only)
  --plain         Force the readline inspector instead of the TUI

``gc`` drops whole exception paths nothing has hit for
``CONFIG.max_path_age_days`` (0 keeps them forever), and with each one
everything it held: dumps, captured source, and anything a process left
half-written. Retention bounds the dumps within a path but not the number of
paths, and a deploy strands a whole generation of them: a path id hashes line
numbers, so shifting a line puts every traceback through that file under a new
id and leaves the old one unreachable. Age tells those apart from failures that
just have not recurred yet.

Capture applies the same rule itself once every ``CONFIG.gc_interval_seconds``
(0 leaves it to this command), so a long-running service stays tidy without
anyone running anything. Running ``gc`` sweeps immediately rather than waiting
for the interval, and can be pointed at a single path.
"""


def _describe_path(store: DumpStore, pid: str) -> str:
    meta = store.path_meta(pid)
    ids = store.dump_ids(pid)
    where = ""
    if meta.get("path"):
        name, line = meta["path"][-1]
        where = f" at {name}:{line}"
    exc_type = meta.get("exc_type", "?")
    latest = ids[-1] if ids else "-"
    return f"{pid}  {len(ids):>5} dumps  {exc_type}{where}\n        latest: {latest}"


def list_command(store: DumpStore, pid: Optional[str] = None) -> int:
    if pid:
        ids = store.dump_ids(pid)
        if not ids:
            print(f"No dumps for path {pid} in {store.root}")
            return 1
        meta = store.path_meta(pid)
        for name, line in meta.get("path", []):
            print(f"  {name}:{line}")
        print(f"{len(ids)} dumps (oldest first, keeping {store.max_per_path}):")
        for trace_id in ids:
            print(f"  {trace_id}")
        return 0

    pids = store.path_ids()
    if not pids:
        print(f"No dumps in {store.root}")
        return 1
    print(f"{len(pids)} exception paths in {store.root}:")
    for candidate in pids:
        print("  " + _describe_path(store, candidate))
    return 0


def _demo() -> int:
    """Capture a chained exception so the inspector has something to walk."""
    def calculate_tax(amount, rate_fn):
        multiplier = rate_fn(amount)
        return amount / 0  # Intentional crash.

    def process_order(user_id, item_price):
        calc_rate = lambda val: 0.15 if val > 100 else 0.05
        order_data = {"user": user_id, "price": item_price}
        try:
            return calculate_tax(item_price, calc_rate)
        except ZeroDivisionError as error:
            raise RuntimeError(f"tax calculation failed for {user_id}") from error

    def handle_checkout():
        user = "user_42"
        cart_total = 250.0
        return process_order(user, cart_total)

    try:
        handle_checkout()
    except Exception:
        trace_id = dump_exception(n_depth_up=1, n_depth_down=1, metadata={"demo": True})

    print(f"trace id: {trace_id}")
    print(f"Run 'python -m excdump inspect {trace_id}' to debug.")
    return 0


def gc_command(store: DumpStore, pid: Optional[str] = None) -> int:
    """Drop exception paths nothing hits any more."""
    targets = [pid] if pid else store.path_ids()
    if not targets:
        print("No dumps found.")
        return 0
    if CONFIG.max_path_age_days <= 0:
        print("Nothing to do: max_path_age_days is 0, so paths are kept forever.")
        return 0
    expired = store.gc_paths(targets)
    for target in expired:
        print(f"{target}: dropped, last seen over {CONFIG.max_path_age_days:g} days ago")
    print(f"Reclaimed {len(expired)} of {len(targets)} path(s).")
    return 0


def main(argv: List[str]) -> int:
    args = argv[1:]
    if "--pickle" in args:
        set_serializer("pickle")
    if "--store" in args:
        index = args.index("--store")
        if index + 1 >= len(args):
            print(USAGE, file=sys.stderr)
            return 2
        configure(store_dir=args[index + 1])
        del args[index : index + 2]
    trace_id: Optional[str] = None
    if "--trace-id" in args:
        index = args.index("--trace-id")
        if index + 1 >= len(args):
            print(USAGE, file=sys.stderr)
            return 2
        trace_id = args[index + 1]
        del args[index : index + 2]
    plain = "--plain" in args
    positional = [a for a in args if not a.startswith("--")]
    command = positional[0] if positional else ("inspect" if trace_id else "demo")
    operand = trace_id or (positional[1] if len(positional) > 1 else None)
    store = DumpStore()

    if command == "inspect":
        try:
            load_and_debug(operand, plain=plain, store=store)
        except (FileNotFoundError, ValueError) as error:
            print(f"*** {error}", file=sys.stderr)
            return 1
        return 0
    if command == "list":
        return list_command(store, operand)
    if command == "gc":
        return gc_command(store, operand)
    if command == "demo":
        return _demo()

    print(USAGE, file=sys.stderr)
    return 2
