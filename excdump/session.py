"""Navigating a loaded dump: frames, the exception chain, and expressions.

The state a debugger UI needs, with no UI in it -- both the plain readline loop
and the full-screen TUI drive this same object.
"""

import linecache
import os
import pprint
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG
from .model import ExceptionDump, ExceptionRecord, FrameSnapshot, _normalize_dump


RELATION_TEXT = {
    "cause": "raised directly from the previous exception",
    "context": "raised while handling the previous exception",
}


class DebuggerSession:
    """Navigation state over a dump: which exception, and which frame in it.

    All methods return text instead of printing, so both the TUI and the plain
    fallback REPL can render the same results.
    """

    def __init__(self, dump: ExceptionDump):
        self.dump = _normalize_dump(dump)
        self.records: List[ExceptionRecord] = self.dump.exceptions
        self.exc_index = len(self.records) - 1
        self.curindex = 0
        self._reset_frame()

    # -- state ---------------------------------------------------------------

    def _reset_frame(self) -> None:
        record = self.record
        self.curindex = min(record.target_frame_index, max(0, len(record.frames) - 1))

    @property
    def record(self) -> ExceptionRecord:
        return self.records[self.exc_index]

    @property
    def frames(self) -> List[FrameSnapshot]:
        return self.record.frames

    @property
    def curframe(self) -> Optional[FrameSnapshot]:
        if not self.frames:
            return None
        return self.frames[min(self.curindex, len(self.frames) - 1)]

    # -- frame navigation ----------------------------------------------------

    def frame_up(self, count: int = 1) -> str:
        if not self.frames:
            return "*** No frames captured for this exception."
        if self.curindex == 0:
            return "*** Oldest captured frame reached."
        self.curindex = max(0, self.curindex - count)
        return self.frame_header()

    def frame_down(self, count: int = 1) -> str:
        if not self.frames:
            return "*** No frames captured for this exception."
        last = len(self.frames) - 1
        if self.curindex == last:
            return "*** Newest frame reached."
        self.curindex = min(last, self.curindex + count)
        return self.frame_header()

    def goto_frame(self, index: int) -> str:
        if not self.frames:
            return "*** No frames captured for this exception."
        if not 0 <= index < len(self.frames):
            return f"*** Frame index out of range (0..{len(self.frames) - 1})."
        self.curindex = index
        return self.frame_header()

    # -- exception navigation ------------------------------------------------

    def exception_up(self) -> str:
        """Move to the older (chained) exception, like pdb's ``exceptions``."""
        if self.exc_index == 0:
            return "*** Oldest exception in the chain reached."
        self.exc_index -= 1
        self._reset_frame()
        return self.exception_header()

    def exception_down(self) -> str:
        """Move to the newer exception in the chain."""
        if self.exc_index >= len(self.records) - 1:
            return "*** Newest exception in the chain reached."
        self.exc_index += 1
        self._reset_frame()
        return self.exception_header()

    def goto_exception(self, index: int) -> str:
        if not 0 <= index < len(self.records):
            return f"*** Exception index out of range (0..{len(self.records) - 1})."
        self.exc_index = index
        self._reset_frame()
        return self.exception_header()

    def exception_header(self) -> str:
        record = self.record
        suffix = ""
        if record.relation and self.exc_index > 0:
            suffix = f" ({RELATION_TEXT[record.relation]})"
        return (
            f"-> Exception [{self.exc_index}/{len(self.records) - 1}] "
            f"{record.title()}{suffix}\n{self.frame_header()}"
        )

    def exceptions_listing(self) -> List[Tuple[int, str, bool]]:
        """``(index, text, is_current)`` for every exception in the chain."""
        rows = []
        for index, record in enumerate(self.records):
            text = record.title()
            if record.relation and index > 0:
                text += f"  [{record.relation}]"
            rows.append((index, text, index == self.exc_index))
        return rows

    # -- rendering -----------------------------------------------------------

    def frame_header(self) -> str:
        frame = self.curframe
        if frame is None:
            return "*** No frames captured for this exception."
        return (
            f"-> Frame [{self.curindex}/{len(self.frames) - 1}] in {frame.name}() "
            f"at {frame.filename}:{frame.lineno}"
        )

    def source_lines(self) -> Tuple[List[Tuple[int, str]], int]:
        """``([(lineno, text), ...], current_lineno)`` for the current frame.

        What the dump captured always beats the file on disk. A frame's line
        number only means anything against the source that was live when the
        exception happened, and by the time anyone opens a dump that file has
        usually been edited -- reading it back would point the arrow at a line
        that had nothing to do with the failure.

        In order: the full text from the path's source sidecar (the whole file,
        so it can be scrolled), then the window carried inside the dump, then --
        only for dumps that captured no source at all -- the file on disk.
        """
        frame = self.curframe
        if frame is None:
            return [], 0

        store = getattr(frame, "store", None)
        file_id = getattr(frame, "file_id", None)
        if store is not None and file_id is not None:
            lines = store.full_lines(file_id)
            if lines:
                return [(n + 1, line) for n, line in enumerate(lines)], frame.lineno

        if hasattr(frame, "source"):
            start, window = frame.source(radius=max(CONFIG.source_radius, 50))
        else:  # dump from an older version
            start, window = getattr(frame, "code_context_start", 1), frame.code_context
        if window:
            return (
                [(start + offset, line.rstrip("\n")) for offset, line in enumerate(window)],
                frame.lineno,
            )

        path = frame.path() if hasattr(frame, "path") else frame.filename
        if os.path.exists(path):
            lines = linecache.getlines(path)
            if lines:
                return [(n + 1, line.rstrip("\n")) for n, line in enumerate(lines)], frame.lineno
        return [], frame.lineno

    def list_source(self, radius: int = 5) -> str:
        lines, current = self.source_lines()
        if not lines:
            return "   [Source code unavailable]"
        out = []
        for lineno, text in lines:
            if abs(lineno - current) > radius:
                continue
            prefix = "-> " if lineno == current else "   "
            out.append(f"{prefix}{lineno:4d}  {text}")
        return "\n".join(out)

    def where_listing(self) -> List[Tuple[int, str, bool]]:
        """``(index, text, is_current)`` for each frame, outermost first."""
        rows = []
        for index, frame in enumerate(self.frames):
            text = f"{frame.name}() at {os.path.basename(frame.filename)}:{frame.lineno}"
            rows.append((index, text, index == self.curindex))
        return rows

    def where(self) -> str:
        lines = ["Traceback (most recent call last):"]
        for index, frame in enumerate(self.frames):
            prefix = "-> " if index == self.curindex else "   "
            lines.append(f"{prefix}[{index}] {frame.filename}:{frame.lineno} in {frame.name}()")
        return "\n".join(lines)

    def scope(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        frame = self.curframe
        if frame is None:
            return {}, {}
        scope_globals = dict(frame.globals)
        scope_globals["__builtins__"] = __builtins__
        return scope_globals, frame.locals

    def names(self) -> List[str]:
        """Identifiers available for completion in the current frame."""
        frame = self.curframe
        if frame is None:
            return []
        return sorted(set(frame.locals) | set(frame.globals))

    def render_mapping(self, mapping: Dict[str, Any], title: str) -> str:
        if not mapping:
            return f"{title}: <empty>"
        lines = [f"{title}:"]
        for key, value in mapping.items():
            lines.append(f"  {key} = {value!r}")
        return "\n".join(lines)

    def locals_text(self) -> str:
        frame = self.curframe
        if frame is None:
            return "*** No frames captured for this exception."
        return self.render_mapping(frame.locals, f"Locals in {frame.name}()")

    def globals_text(self) -> str:
        frame = self.curframe
        if frame is None:
            return "*** No frames captured for this exception."
        return self.render_mapping(frame.globals, f"Globals referenced in {frame.name}()")

    def eval_expr(self, expr: str, pretty: bool = False) -> str:
        """Evaluate an expression (or statement) in the current frame's scope."""
        frame = self.curframe
        if frame is None:
            return "*** No frames captured for this exception."
        scope_globals, scope_locals = self.scope()
        try:
            result = eval(expr, scope_globals, scope_locals)
        except SyntaxError:
            try:
                exec(expr, scope_globals, scope_locals)
                return ""
            except Exception as error:
                return f"*** {type(error).__name__}: {error}"
        except Exception as error:
            return f"*** {type(error).__name__}: {error}"
        return pprint.pformat(result) if pretty else repr(result)
