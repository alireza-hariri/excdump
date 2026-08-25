"""Full-screen terminal UI for inspecting exception dumps.

Layout::

    exception title / chain position
    ┌───────────────────────────┬──────────────────┐
    │ source (auto-scrolled)    │ frame stack      │
    │                           ├──────────────────┤
    │                           │ locals           │
    ├───────────────────────────┴──────────────────┤
    │ transcript of commands and results           │
    ├──────────────────────────────────────────────┤
    │ (exc-dbg) _                                  │
    └ key hints ───────────────────────────────────┘
"""

from __future__ import annotations

import os
from typing import Callable, List

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.data_structures import Point
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    ScrollOffsets,
    VSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.filters import Condition

from .cli import COMMANDS, dispatch, help_text
from .session import DebuggerSession

STYLE = Style.from_dict(
    {
        "header": "bg:#1f2430 #d6deeb bold",
        "header.exc": "bg:#1f2430 #ff6b6b bold",
        "header.dim": "bg:#1f2430 #7f8c9b",
        "toolbar": "bg:#1f2430 #7f8c9b",
        "title": "#82aaff bold",
        "source": "",
        "source.current": "bg:#3a3f55 #ffffff bold",
        "gutter": "#5c6773",
        "stack": "",
        "stack.current": "#c3e88d bold",
        "stack.index": "#5c6773",
        "locals.key": "#82aaff",
        "locals.value": "#c9d1d9",
        "out.echo": "#7f8c9b",
        "out.error": "#ff6b6b",
        "out.info": "#c3e88d",
        "prompt": "#c3e88d bold",
        "separator": "#3a3f55",
        "completion-menu.completion": "bg:#2b3040 #d6deeb",
        "completion-menu.completion.current": "bg:#82aaff #10131a bold",
        "completion-menu.meta.completion": "bg:#2b3040 #7f8c9b",
        "completion-menu.meta.completion.current": "bg:#5c6773 #ffffff",
    }
)


class SourceLexer(Lexer):
    """Styles the frame's current line; everything else is plain."""

    def __init__(self, current_row: Callable[[], int]):
        self.current_row = current_row

    def lex_document(self, document: Document):
        current = self.current_row()

        def get_line(lineno: int):
            text = document.lines[lineno] if lineno < len(document.lines) else ""
            if lineno == current:
                return [("class:source.current", text)]
            gutter, _, rest = text.partition("│")
            if rest:
                return [("class:gutter", gutter + "│"), ("class:source", rest)]
            return [("class:source", text)]

        return get_line


class OutputLexer(Lexer):
    """Colours transcript lines by their leading marker."""

    def lex_document(self, document: Document):
        def get_line(lineno: int):
            text = document.lines[lineno] if lineno < len(document.lines) else ""
            if text.startswith("***"):
                style = "class:out.error"
            elif text.startswith("(exc-dbg)"):
                style = "class:out.echo"
            elif text.startswith("->"):
                style = "class:out.info"
            else:
                style = ""
            return [(style, text)]

        return get_line


class DebuggerCompleter(Completer):
    """Completes /commands at the start of a line, identifiers elsewhere."""

    def __init__(self, session: DebuggerSession):
        self.session = session

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        stripped = text.lstrip()

        if stripped.startswith("/") and " " not in stripped:
            for name, aliases, description in COMMANDS:
                for candidate in (name,) + aliases:
                    if candidate.startswith(stripped):
                        yield Completion(
                            candidate,
                            start_position=-len(stripped),
                            display=name,
                            display_meta=description,
                        )
                        break
            return

        word = document.get_word_before_cursor(WORD=False)
        if not word:
            return
        for name in self.session.names():
            if name.startswith(word):
                yield Completion(name, start_position=-len(word))


class DebuggerTUI:
    def __init__(self, session: DebuggerSession, filepath: str):
        self.session = session
        self.filepath = filepath
        self.current_row = 0

        self.source_buffer = Buffer(document=Document(""), name="source")
        self.output_buffer = Buffer(document=Document(""), name="output")
        self.input_buffer = Buffer(
            name="input",
            multiline=False,
            history=InMemoryHistory(),
            completer=DebuggerCompleter(session),
            complete_while_typing=True,
            accept_handler=self._accept,
        )

        self.layout = Layout(self._build_layout(), focused_element=self.input_window)
        self.app = Application(
            layout=self.layout,
            key_bindings=self._build_key_bindings(),
            style=STYLE,
            full_screen=True,
            mouse_support=True,
        )

        self._log(f"Loaded {os.path.basename(filepath)} — type /help for commands.")
        self.refresh()

    # -- layout --------------------------------------------------------------

    def _build_layout(self):
        self.source_window = Window(
            BufferControl(
                buffer=self.source_buffer,
                focusable=False,
                lexer=SourceLexer(lambda: self.current_row),
            ),
            wrap_lines=False,
            always_hide_cursor=True,
            scroll_offsets=ScrollOffsets(top=4, bottom=4),
        )
        self.output_window = Window(
            BufferControl(buffer=self.output_buffer, focusable=False, lexer=OutputLexer()),
            wrap_lines=True,
            always_hide_cursor=True,
            height=D(min=3, preferred=15, weight=1),
        )
        self.input_window = Window(
            BufferControl(buffer=self.input_buffer),
            height=1,
            get_line_prefix=lambda *_: [("class:prompt", "(exc-dbg) ")],
        )

        stack_window = Window(
            FormattedTextControl(
                self._stack_text,
                focusable=False,
                # A non-focusable control does not normally have a cursor,
                # so prompt-toolkit cannot keep its current row in view.  Use
                # an invisible cursor position as the scroll anchor instead.
                get_cursor_position=self._stack_cursor_position,
            ),
            wrap_lines=False,
            height=D(min=3),
        )
        locals_window = Window(
            FormattedTextControl(self._locals_text, focusable=False),
            wrap_lines=True,
        )

        side = HSplit(
            [
                self._pane_title("frames"),
                stack_window,
                ConditionalContainer(
                    HSplit(
                        [
                            Window(char="─", height=1, style="class:separator"),
                            self._pane_title("locals"),
                            locals_window,
                        ]
                    ),
                    filter=Condition(self._show_locals),
                ),
            ]
        )

        side_width = D(min=28, preferred=44, weight=1)
        body = VSplit(
            [
                HSplit(
                    [self._pane_title("source"), self.source_window],
                    width=D(weight=2),
                ),
                Window(char="│", width=1, style="class:separator"),
                HSplit([side], width=side_width),
            ],
            height=D(min=3, preferred=20, max=22),
        )

        root = HSplit(
            [
                Window(FormattedTextControl(self._header_text), height=1, style="class:header"),
                body,
                Window(char="─", height=1, style="class:separator"),
                self.output_window,
                Window(char="─", height=1, style="class:separator"),
                self.input_window,
                Window(FormattedTextControl(self._toolbar_text), height=1, style="class:toolbar"),
            ]
        )
        return FloatContainer(
            root,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=12, scroll_offset=1),
                )
            ],
        )

    def _show_locals(self) -> bool:
        """Show locals only when the terminal has room for a useful panel."""
        app = getattr(self, "app", None)
        if app is None:
            return True
        return app.output.get_size().rows >= 24

    @staticmethod
    def _pane_title(text: str) -> Window:
        return Window(
            FormattedTextControl([("class:title", f" {text} ")]),
            height=1,
            align=WindowAlign.LEFT,
        )

    # -- dynamic pane content ------------------------------------------------

    def _header_text(self):
        session = self.session
        record = session.record
        parts = [
            ("class:header.exc", f" {record.exc_type} "),
            ("class:header", record.exc_value or ""),
        ]
        total = len(session.records)
        if total > 1:
            parts.append(
                (
                    "class:header.dim",
                    f"   [exception {session.exc_index + 1}/{total}"
                    + (f", {record.relation}" if record.relation and session.exc_index else "")
                    + "]",
                )
            )
        return parts

    def _toolbar_text(self):
        return [
            (
                "class:toolbar",
                " ↑↓ history   ⇥ complete   M-/S-↑↓ frame   M-/S-←→ exception   "
                "PgUp/PgDn source   ^L clear   ^D quit ",
            )
        ]

    def _stack_text(self):
        session = self.session
        fragments = []
        if len(session.records) > 1:
            for index, text, current in session.exceptions_listing():
                style = "class:stack.current" if current else "class:stack"
                marker = "*" if current else " "
                fragments.append(("class:stack.index", f" {marker}exc {index} "))
                fragments.append((style, f"{text}\n"))
            fragments.append(("class:separator", " ─\n"))

        if not session.frames:
            fragments.append(("class:stack", " <no frames captured>\n"))
            return fragments

        for index, text, current in session.where_listing():
            style = "class:stack.current" if current else "class:stack"
            fragments.append(("class:stack.index", f" {'>' if current else ' '}[{index}] "))
            fragments.append((style, f"{text}\n"))
        return fragments

    def _stack_cursor_position(self) -> Point:
        """Return the row containing the selected frame.

        ``FormattedTextControl`` is otherwise rendered from the top whenever
        its content is taller than the frames pane.  Giving the window a
        cursor position makes prompt-toolkit apply its normal scroll logic,
        keeping the selected frame visible after frame/exception changes.
        """
        row = self.session.curindex
        if len(self.session.records) > 1:
            row += len(self.session.records) + 1  # exception rows and divider
        return Point(x=0, y=max(0, row))

    def _locals_text(self):
        frame = self.session.curframe
        if frame is None:
            return [("class:locals.value", " <no frame>")]
        if not frame.locals:
            return [("class:locals.value", " <empty>")]
        fragments = []
        for key, value in frame.locals.items():
            try:
                rendered = repr(value)
            except Exception as error:  # a broken __repr__ must not kill the UI
                rendered = f"<repr failed: {error}>"
            fragments.append(("class:locals.key", f" {key}"))
            fragments.append(("class:locals.value", f" = {rendered}\n"))
        return fragments

    # -- state sync ----------------------------------------------------------

    def refresh(self) -> None:
        lines, current = self.session.source_lines()
        if not lines:
            self._set_buffer(self.source_buffer, "  [source unavailable]", 0)
            self.current_row = -1
            return

        width = max(len(str(lines[-1][0])), 4)
        rendered: List[str] = []
        current_row = 0
        for row, (lineno, text) in enumerate(lines):
            if lineno == current:
                current_row = row
                marker = "→"
            else:
                marker = " "
            rendered.append(f"{marker}{lineno:>{width}} │ {text}")

        self.current_row = current_row
        text = "\n".join(rendered)
        cursor = Document(text).translate_row_col_to_index(current_row, 0)
        self._set_buffer(self.source_buffer, text, cursor)

    @staticmethod
    def _set_buffer(buffer: Buffer, text: str, cursor: int) -> None:
        buffer.set_document(Document(text, cursor), bypass_readonly=True)

    def _log(self, text: str) -> None:
        """Append lines to the transcript pane and keep it scrolled to the end."""
        if not text:
            return
        existing = self.output_buffer.text
        combined = f"{existing}\n{text}" if existing else text
        lines = combined.splitlines()[-500:]
        combined = "\n".join(lines)
        self._set_buffer(self.output_buffer, combined, len(combined))

    def _run(self, line: str, echo: bool = True) -> None:
        if echo:
            self._log(f"(exc-dbg) {line}")
        output, quit_now = dispatch(self.session, line)
        if quit_now:
            self.app.exit()
            return
        if output == "\x00clear":
            self._set_buffer(self.output_buffer, "", 0)
        else:
            self._log(output)
        self.refresh()

    def _accept(self, buffer: Buffer) -> bool:
        line = buffer.text.strip()
        if line:
            self._run(line)
        return False  # reset the input line

    # -- keys ----------------------------------------------------------------

    def _build_key_bindings(self) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("c-d")
        @keys.add("f10")
        def _(event):
            event.app.exit()

        @keys.add("c-c")
        def _(event):
            if self.input_buffer.text:
                self.input_buffer.reset()
            else:
                event.app.exit()

        @keys.add("c-l")
        def _(event):
            self._set_buffer(self.output_buffer, "", 0)

        @keys.add("escape", "up")
        @keys.add("s-up")
        def _(event):
            self._log(self.session.frame_up())
            self.refresh()

        @keys.add("escape", "down")
        @keys.add("s-down")
        def _(event):
            self._log(self.session.frame_down())
            self.refresh()

        @keys.add("escape", "left")
        @keys.add("s-left")
        def _(event):
            self._log(self.session.exception_up())
            self.refresh()

        @keys.add("escape", "right")
        @keys.add("s-right")
        def _(event):
            self._log(self.session.exception_down())
            self.refresh()

        @keys.add("pageup")
        def _(event):
            self._scroll_source(-15)

        @keys.add("pagedown")
        def _(event):
            self._scroll_source(15)

        @keys.add("f1")
        def _(event):
            self._log(help_text())

        return keys

    def _scroll_source(self, delta: int) -> None:
        document = self.source_buffer.document
        row = document.cursor_position_row + delta
        row = max(0, min(row, document.line_count - 1))
        self.source_buffer.cursor_position = document.translate_row_col_to_index(row, 0)

    def run(self) -> None:
        self.app.run()


def run_tui(session: DebuggerSession, filepath: str) -> None:
    DebuggerTUI(session, filepath).run()
