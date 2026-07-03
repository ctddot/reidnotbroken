"""Full-screen chat TUI: a real split-pane layout, not an inline redraw hack.

A `prompt_toolkit` full-screen `Application` owns the whole terminal (like
`vim`/`htop`, alternate screen — native scrollback is untouched and restored
on exit). Layout: a scrollable output pane on top, and a footer permanently
pinned to the last rows — spinner row, input box, status line. Because
`prompt_toolkit` owns the screen entirely, it handles cursor tracking and
resize itself; nothing fights it (unlike the reverted VT100 scroll-region
approach, which manually repositioned the cursor behind Rich/prompt_toolkit's
backs and corrupted rendering).

Rendering reuse: rather than reimplementing Rich's markdown/table/panel
rendering in prompt_toolkit's own formatting, `render.console` (the
module-level Rich `Console` almost everything in `ui/render.py` and
`ui/commands.py` already prints through) is temporarily swapped for one
backed by an in-memory buffer. Every existing `render.print_*` call and
`ui.commands.handle` keep working completely unmodified; their ANSI output is
drained and appended into the output pane's fragment list.
"""
from __future__ import annotations

import asyncio
import functools
import io
import random
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, has_completions, is_done
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.text import Text

from reidcli.deepreid import format_markdown, run_deepreid, save_deepreid_result
from reidcli.diagnostics.logger import get_logger
from reidcli.runtime.orchestrator import Orchestrator
from reidcli.ui import render
from reidcli.ui.commands import _EFFORT_LEVELS, SLASH_COMMANDS, WORKFLOW_SUBCOMMANDS
from reidcli.ui.commands import handle as handle_command
from reidcli.ui.render import _GERUNDS, _STAR_FRAMES, _bullet_grid
from reidcli.ui.theme import (
    APP_NAME,
    BULLET,
    DANGER,
    DIM,
    PRIMARY,
    SPARKLE,
    SUCCESS,
    TREE,
    WARN,
    context_window_for,
    fmt_tokens,
    short_path,
)

log = get_logger("reidcli.ui")

# A paste collapses to a placeholder when it's multi-line (a single-line
# input box can't display embedded newlines sanely) or long enough to make
# the box unreadable. Same idea as Claude Code's own input box.
_PASTE_COLLAPSE_CHARS = 300

# Typing one of these at the very start of the box turns it green and routes
# the submission through the real Researcher->Planner->Critic DeepReid
# pipeline (deepreid/pipeline.py) instead of a normal turn — the trigger word
# itself is stripped before the task is handed to the pipeline.
_DEEPREID_TRIGGERS = ("deepread", "deep read", "deepreid", "deep reid")

# Box border/caret color. Normal is a flat color; DeepReid cycles through
# these shades over time (see _box_color/_deepread_pulse_active) for an
# actual pulse, not just a static color swap.
_BOX_COLOR_NORMAL = "#ff5f5f"
_DEEPREID_PULSE_SHADES = ("#5fd75f", "#7fe77f", "#9ff09f", "#7fe77f")

_MODE_COLOR = {
    "strict": "#ff5555",
    "balanced": "#ffd75f",
    "autonomous": "#5fd75f",
    "custom": "#d75fd7",
}


class _ConsoleCapture:
    """A Rich Console backed by an in-memory buffer, so existing render.py /
    commands.py code keeps writing ANSI-styled output unmodified — it just
    lands in a buffer we drain instead of stdout."""

    def __init__(self) -> None:
        cols, _rows = shutil.get_terminal_size(fallback=(100, 30))
        self._buf = io.StringIO()
        self.console = Console(
            file=self._buf,
            width=max(40, cols - 2),
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
            soft_wrap=False,
        )
        self._pos = 0

    def drain(self) -> str:
        text = self._buf.getvalue()
        new = text[self._pos :]
        self._pos = len(text)
        return new


class _Block:
    """One unit of output pane content.

    Most blocks are static (fixed fragments). Thinking and tool-call blocks
    are collapsible: two ANSI variants are rendered once (at turn-completion
    time, never replayed) and the pane picks whichever the global Ctrl+O
    toggle currently wants at assembly time — no re-rendering, no re-running
    any side-effecting code (slash commands, etc.) on toggle.
    """

    __slots__ = ("fragments", "collapsed", "expanded")

    def __init__(self, fragments=None, collapsed=None, expanded=None) -> None:  # type: ignore[no-untyped-def]
        self.fragments = fragments
        self.collapsed = collapsed
        self.expanded = expanded

    @property
    def is_collapsible(self) -> bool:
        return self.collapsed is not None


class _OutputPane:
    """Accumulated blocks for the scrollable output window.

    Tail-to-bottom auto-follow uses prompt_toolkit's documented
    `[SetCursorPosition]` sentinel mechanism (see widgets.base.Label): the
    renderer scrolls to keep whichever fragment carries that marker visible.
    Scrolling manually (PageUp/PageDown, mouse wheel) works by *relocating*
    that marker to the target line rather than fighting the renderer's
    per-frame scroll recomputation — `Window._scroll_up/_down` (used by the
    default page-navigation bindings and the default mouse wheel handler)
    only adjust `vertical_scroll` directly, which gets silently overwritten
    right back by that same per-frame recomputation as long as the marker
    stays fixed at the bottom; relocating the marker is what actually moves
    the view. While `pinned` is True (the default, and whenever scrolled back
    down to the last line) the marker tracks the newest line automatically —
    "locked to bottom" exactly as before. Scrolling up unpins so new output
    appends below the fold without disturbing what's being read.
    """

    def __init__(self) -> None:
        self._blocks: list[_Block] = []
        self.expanded = False  # global Ctrl+O toggle for collapsible blocks
        self._line_cache: list | None = None
        self._line_cache_expanded = self.expanded
        self.pinned = True
        self._cursor_line = 0  # only meaningful while not pinned

    def append_static(self, ansi_text: str) -> None:
        if not ansi_text:
            return
        self._blocks.append(_Block(fragments=to_formatted_text(ANSI(ansi_text))))
        self._line_cache = None

    def append_collapsible(self, collapsed_ansi: str, expanded_ansi: str) -> None:
        self._blocks.append(
            _Block(
                collapsed=to_formatted_text(ANSI(collapsed_ansi)),
                expanded=to_formatted_text(ANSI(expanded_ansi)),
            )
        )
        self._line_cache = None

    def toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self._line_cache = None

    def reset(self) -> None:
        self._blocks = []
        self._line_cache = None
        self.pinned = True
        self._cursor_line = 0

    def _all_fragments(self):  # type: ignore[no-untyped-def]
        out: list = []
        for block in self._blocks:
            if block.is_collapsible:
                out.extend(block.expanded if self.expanded else block.collapsed)
            else:
                out.extend(block.fragments)
        return out

    def _lines(self) -> list:
        if self._line_cache is None or self._line_cache_expanded != self.expanded:
            self._line_cache = list(split_lines(self._all_fragments()))
            self._line_cache_expanded = self.expanded
        return self._line_cache

    def scroll_up(self, lines: int = 3) -> None:
        total = max(1, len(self._lines()))
        base = (total - 1) if self.pinned else self._cursor_line
        self._cursor_line = max(0, base - lines)
        self.pinned = False

    def scroll_down(self, lines: int = 3) -> None:
        if self.pinned:
            return
        total = max(1, len(self._lines()))
        self._cursor_line = min(total - 1, self._cursor_line + lines)
        if self._cursor_line >= total - 1:
            self.pinned = True

    def scroll_top(self) -> None:
        self._cursor_line = 0
        self.pinned = False

    def scroll_bottom(self) -> None:
        self._cursor_line = 0
        self.pinned = True

    def get_fragments(self):  # type: ignore[no-untyped-def]
        lines = self._lines()
        total = len(lines)
        if total == 0:
            return [("[SetCursorPosition]", "")]

        target = (total - 1) if self.pinned else min(self._cursor_line, total - 1)
        out: list = []
        for i, line in enumerate(lines):
            if i == target:
                out.append(("[SetCursorPosition]", ""))
            out.extend(line)
            if i != total - 1:
                out.append(("", "\n"))
        return out


class _ScrollableOutputControl(FormattedTextControl):
    """FormattedTextControl that routes mouse wheel scroll to callbacks.

    The default `Window._mouse_handler` fallback for scroll events just
    nudges `vertical_scroll` by +-1, which is exactly what gets fought and
    reverted by the cursor-follow recomputation described on `_OutputPane`.
    Intercepting here lets scroll wheel drive the same marker-relocation
    logic as the PageUp/PageDown key bindings.
    """

    def __init__(self, get_fragments, on_scroll_up, on_scroll_down, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(get_fragments, **kwargs)
        self._on_scroll_up = on_scroll_up
        self._on_scroll_down = on_scroll_down

    def mouse_handler(self, mouse_event: MouseEvent):  # type: ignore[no-untyped-def]
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._on_scroll_up()
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._on_scroll_down()
            return None
        return super().mouse_handler(mouse_event)


class SlashCommandCompleter(Completer):
    """Completion menu for the input box: typing "/" lists every command
    from `ui.commands.SLASH_COMMANDS` (the same source `/help` renders from,
    so the two can't drift apart); typing "/workflow " lists its
    subcommands from `WORKFLOW_SUBCOMMANDS`. Returns nothing for anything
    else, so it's invisible while typing a normal prompt.
    """

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator

    def _word_completion(self, word: str, values: list[str]):  # type: ignore[no-untyped-def]
        for value in values:
            if value.startswith(word):
                yield Completion(value, start_position=-len(word))

    def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        if text.startswith("/workflow "):
            prefix = text[len("/workflow ") :]
            if " " in prefix:
                return
            for name, args, desc in WORKFLOW_SUBCOMMANDS:
                if name.startswith(prefix):
                    display = f"{name} {args}".rstrip()
                    yield Completion(name, start_position=-len(prefix), display=display, display_meta=desc)
            return

        arg_completions = {
            "/mode ": ["strict", "balanced", "autonomous", "custom"],
            "/effort ": list(_EFFORT_LEVELS),
            "/nyx ": ["on", "off"],
            "/plan ": ["on", "off"],
            "/web ": ["on", "off"],
            "/theme ": ["reid", "dark", "high-contrast"],
            "/tools enable ": [d.name for d in self.orchestrator.tools.definitions()],
            "/tools disable ": [d.name for d in self.orchestrator.tools.definitions()],
            "/use ": self.orchestrator.providers.names() if self.orchestrator.providers is not None else [],
            "/resume ": [s.id for s in self.orchestrator.session_store.list()],
            "/session delete ": [s.id for s in self.orchestrator.session_store.list()],
            "/prompt ": [wf.name for wf in self.orchestrator.workflow_store.list()],
            "/workflow run ": [wf.name for wf in self.orchestrator.workflow_store.list()],
            "/workflow show ": [wf.name for wf in self.orchestrator.workflow_store.list()],
            "/workflow delete ": [wf.name for wf in self.orchestrator.workflow_store.list()],
        }
        for prefix, values in arg_completions.items():
            if text.startswith(prefix):
                word = text[len(prefix) :]
                if " " in word:
                    return
                yield from self._word_completion(word, values)
                return

        word = text[1:]
        if " " in word:
            return
        for cmd, args, desc, _group in SLASH_COMMANDS:
            token = cmd[1:]
            if token.startswith(word):
                display = f"{cmd} {args}".rstrip()
                yield Completion(f"/{token}", start_position=-len(text), display=display, display_meta=desc)


class _DarkCompletionsControl(UIControl):
    """Completion menu with explicit dark fragments.

    prompt_toolkit's stock menu is theme-driven. Some Windows terminals kept
    rendering its default gray block, so this control paints every cell itself.
    """

    def has_focus(self) -> bool:
        return False

    def preferred_width(self, max_available_width: int) -> int | None:
        state = get_app().current_buffer.complete_state
        if state is None:
            return 0
        left = max(10, max(get_cwidth(c.display_text) for c in state.completions) + 1)
        right = max((get_cwidth(c.display_meta_text) for c in state.completions), default=0) + 1
        return min(max_available_width, left + right)

    def preferred_height(self, width: int, max_available_height: int, wrap_lines: bool, get_line_prefix) -> int | None:  # type: ignore[no-untyped-def]
        state = get_app().current_buffer.complete_state
        return min(max_available_height, len(state.completions)) if state else 0

    @staticmethod
    def _fit(text: str, width: int) -> str:
        if width <= 0:
            return ""
        if get_cwidth(text) > width:
            while text and get_cwidth(text + "...") > width:
                text = text[:-1]
            return text + "..."
        return text + (" " * max(0, width - get_cwidth(text)))

    def create_content(self, width: int, height: int) -> UIContent:
        state = get_app().current_buffer.complete_state
        if state is None:
            return UIContent()
        completions = state.completions
        index = state.complete_index or 0
        left_width = max(10, min(width, max(get_cwidth(c.display_text) for c in completions) + 1))
        meta_width = max(0, width - left_width)

        def get_line(i: int):
            completion = completions[i]
            current = i == index
            bg = "#ff5f5f bold" if current else "#ff5f5f"
            meta = "#ff8a8a" if current else "#ff5f5f"
            left = self._fit(" " + completion.display_text, left_width)
            right = self._fit(" " + completion.display_meta_text, meta_width)
            return [(bg, left), (meta, right)]

        return UIContent(get_line=get_line, cursor_position=Point(x=0, y=index), line_count=len(completions))


class _DarkCompletionsMenu(ConditionalContainer):
    def __init__(self) -> None:
        super().__init__(
            content=Window(
                content=_DarkCompletionsControl(),
                width=Dimension(min=10),
                height=Dimension(min=1, max=10),
                dont_extend_width=True,
                style="",
                z_index=10**8,
            ),
            filter=has_completions & ~is_done,
        )


class ChatApp:
    """Owns the full-screen layout, input handling, and turn dispatch."""

    def __init__(self, orchestrator: Orchestrator, initial_prompt: str | None = None) -> None:
        self.orchestrator = orchestrator
        self.capture = _ConsoleCapture()
        self.output = _OutputPane()
        self._history = InMemoryHistory()
        self._thinking = {"flag": False, "start": 0.0, "gerund": "", "last_swap": 0.0}
        self._cancel_event: threading.Event | None = None
        self._cancel_confirm_until = 0.0
        self._approving: dict = {"flag": False, "prompt": "", "result": False, "event": None}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._initial_prompt = (initial_prompt or "").strip()
        self._pastes: dict[str, str] = {}
        self._paste_counter = 0
        self._deepreid_running = False

        self._buf = Buffer(
            history=self._history,
            multiline=False,
            read_only=Condition(lambda: self._approving["flag"]),
            completer=SlashCommandCompleter(orchestrator),
            complete_while_typing=True,
        )
        self.app: Application = Application(
            layout=self._build_layout(),
            key_bindings=self._build_key_bindings(),
            full_screen=True,
            mouse_support=True,
            style=Style.from_dict(
                {
                    "completion-menu": "#ff5f5f",
                    "completion-menu.completion": "#ff5f5f",
                    "completion-menu.completion.current": "#ff5f5f bold",
                    "completion-menu.meta": "#ff5f5f",
                    "completion-menu.meta.completion.current": "#ff8a8a",
                    "scrollbar.background": "bg:#141414",
                    "scrollbar.button": "bg:#5f5f5f",
                }
            ),
        )

    # --- setup -----------------------------------------------------------

    def start(self) -> None:
        if self.orchestrator.state is None:
            self.orchestrator.start_session(title="interactive")
        self._append_output(render.banner)

    async def main(self) -> int:
        self._loop = asyncio.get_running_loop()
        self.app.create_background_task(self._spinner_ticker())
        if self._initial_prompt:
            # Run as a background task rather than awaiting inline, so the app
            # starts rendering (banner, empty input box) immediately instead
            # of appearing to hang until the injected prompt's turn finishes.
            self.app.create_background_task(self._submit_text(self._initial_prompt))
        result = await self.app.run_async()
        return result or 0

    async def _spinner_ticker(self) -> None:
        while True:
            await asyncio.sleep(0.125)
            # Prune finished subagent rows past their linger window so the panel
            # actually shrinks when children complete.
            try:
                self.orchestrator.subagents.prune_finished()
            except AttributeError:
                pass
            if (
                self._thinking["flag"]
                or self._approving["flag"]
                or self._deepread_pulse_active()
                or self._subagent_rows_visible()
            ):
                self.app.invalidate()

    # --- subagent panel --------------------------------------------------

    _SUBAGENT_PANEL_MAX = 5

    def _subagent_rows_visible(self) -> bool:
        """True when there's anything to render in the panel (running or lingering)."""
        try:
            return bool(self.orchestrator.subagents.visible_rows())
        except AttributeError:
            return False

    def _subagent_fragments(self):  # type: ignore[no-untyped-def]
        try:
            return self._build_subagent_fragments()
        except Exception:  # noqa: BLE001 - cosmetic; never break the render loop
            log.exception("subagent panel render failed")
            return [("#9e9e9e", "  subagents: (render error)")]

    def _build_subagent_fragments(self):  # type: ignore[no-untyped-def]
        rows = self.orchestrator.subagents.visible_rows()
        if not rows:
            return [("", "")]
        # Cap displayed rows; overflow gets a "+N more" line.
        shown = rows[: self._SUBAGENT_PANEL_MAX]
        overflow = len(rows) - len(shown)

        status_glyph = {
            "running": ("#ffd75f", "◐"),
            "done": ("#5fd75f", "●"),
            "error": ("#ff5f5f", "●"),
        }
        frags: list = []
        for i, row in enumerate(shown):
            color, glyph = status_glyph.get(row.status, ("#9e9e9e", "○"))
            elapsed = int(row.elapsed_seconds)
            name = row.name[:16].ljust(16)
            status_text = row.status.ljust(7)
            action = (row.error or row.last_action or "").strip()
            action = action[:60]
            frags += [
                (color, f"  {glyph} "),
                ("#ffffff bold", name),
                (" "),
                (f"{color}", status_text),
                ("#9e9e9e", f" {elapsed}s"),
            ]
            if action:
                frags += [("#6c6c6c", "  · "), ("#9e9e9e", action)]
            if i != len(shown) - 1 or overflow > 0:
                frags.append(("", "\n"))
        if overflow > 0:
            frags.append(("#9e9e9e", f"  … +{overflow} more subagent(s)"))
        return frags

    def _deepread_pulse_active(self) -> bool:
        """Whether the box border should be pulsing right now — either the
        trigger word is currently typed (not yet submitted) or the pipeline
        is actively running. Without this, `_box_color()` would only ever be
        re-evaluated on buffer-edit events, so it'd show one static shade
        instead of animating while just sitting there."""
        return bool(self._deepread_prefix_len()) or self._deepreid_running

    # --- rendering bridge --------------------------------------------------

    def _append_output(self, fn: Callable[[], None]) -> None:
        fn()
        self.output.append_static(self.capture.drain())
        if self.app.is_running:
            self.app.invalidate()

    def _render_thinking_variants(self, text: str, seconds: int) -> tuple[str, str]:
        """Render both display variants of the chain-of-thought block once.

        Only called when the model actually produced reasoning (see
        `_emit_turn_result`) — an empty turn no longer renders a filler
        block. Collapsed: a single grayed-out "Thought for Ns" header
        matching the spinner's elapsed-time readout. Expanded: the same
        header plus the full thinking text beneath it. Neither variant is
        ever re-rendered — Ctrl+O just picks which was already captured.
        """
        header = Text(f"  {SPARKLE} Thought for {seconds}s", style=DIM)

        render.console.print(header)
        collapsed = self.capture.drain()

        render.console.print(header)
        render.print_thinking(text)
        expanded = self.capture.drain()
        return collapsed, expanded

    def _render_tool_call_variants(self, entry: dict) -> tuple[str, str]:
        """Render both display variants of one tool-call log entry.

        Collapsed: header line (name + args) with an inline ok/error status.
        Expanded: today's two-line layout — header, then a tree-connector
        result line beneath it.
        """
        name = entry["name"]
        ok = entry["ok"]
        error = entry.get("error", "")
        args = entry.get("args", {})
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
        header = Text.assemble((name, "bold"), ("(", DIM), (args_str, DIM), (")", DIM))

        status = Text(" ok", style=SUCCESS) if ok else Text(" error", style=DANGER)
        collapsed_line = Text.assemble(header, status, ("  (ctrl+o)", DIM))
        render.console.print(_bullet_grid(Text(BULLET, style=PRIMARY), collapsed_line))
        collapsed = self.capture.drain()

        render.console.print(_bullet_grid(Text(BULLET, style=PRIMARY), header))
        result = Text("ok", style=SUCCESS) if ok else Text(f"Error: {error}", style=DANGER)
        render.console.print(Text.assemble(("  ", ""), (TREE, DIM), ("  ", ""), result))
        expanded = self.capture.drain()

        return collapsed, expanded

    def _emit_turn_result(self, result: dict, thinking_seconds: int) -> None:
        # Only render the thinking block when the model actually produced
        # reasoning. Empty <think> content gets suppressed entirely rather
        # than leaving a "(model returned no reasoning for this turn)" line
        # floating above the answer, which was noisy and confusing when the
        # provider skipped CoT (short answers, safety refusals, low-effort
        # runs).
        thinking_text = (result.get("thinking") or "").strip()
        if thinking_text:
            thinking_variants = self._render_thinking_variants(thinking_text, thinking_seconds)
            self.output.append_collapsible(*thinking_variants)

        for entry in result.get("tools", []):
            self.output.append_collapsible(*self._render_tool_call_variants(entry))

        render.print_assistant(result["text"])
        self.output.append_static(self.capture.drain())

        if self.app.is_running:
            self.app.invalidate()

    # --- scrolling (mouse wheel only) ---------------------------------------

    def _scroll_up(self) -> None:
        self.output.scroll_up(3)
        self.app.invalidate()

    def _scroll_down(self) -> None:
        self.output.scroll_down(3)
        self.app.invalidate()

    def _scroll_page_up(self) -> None:
        self.output.scroll_up(15)
        self.app.invalidate()

    def _scroll_page_down(self) -> None:
        self.output.scroll_down(15)
        self.app.invalidate()

    def _scroll_top(self) -> None:
        self.output.scroll_top()
        self.app.invalidate()

    def _scroll_bottom(self) -> None:
        self.output.scroll_bottom()
        self.app.invalidate()

    # --- status / spinner content -----------------------------------------

    def _estimate_tokens(self) -> int:
        st = self.orchestrator.state
        if st is None:
            return 0
        # Prefer real usage from the provider's last response over a guess —
        # StubProvider (and any provider that doesn't report usage) leaves
        # these at 0, so the char-based estimate below is still the fallback
        # for it, but real providers (e.g. Anthropic) report actual token
        # counts and that's what's shown once at least one turn has run.
        real = st.last_usage_prompt_tokens + st.last_usage_completion_tokens
        if real > 0:
            return real
        try:
            chars = sum(len(m.content or "") for m in list(st.messages))
        except (RuntimeError, AttributeError):
            return 0
        return max(1, chars // 4)

    def _status(self) -> dict:
        st = self.orchestrator.state
        if st is None:
            return {
                "mode": "—", "model": "—", "effort": "—",
                "tokens_used": 0, "context_window": 0,
                "workspace": "—", "tasks": 0,
            }
        return {
            "mode": st.effective_mode.value,
            "model": st.session.model,
            "effort": st.session.reasoning_effort,
            "tokens_used": self._estimate_tokens(),
            "context_window": context_window_for(st.session.model),
            "workspace": str(st.session.workspace),
            "tasks": self.orchestrator.task_count(),
        }

    def _status_fragments(self):  # type: ignore[no-untyped-def]
        # Called on every redraw by prompt_toolkit's core render loop, with no
        # error boundary above it — an uncaught exception here kills the whole
        # app (that's what happened when tasks.json read failed). Never let a
        # status-computation bug take the TUI down.
        try:
            return self._build_status_fragments()
        except Exception:  # noqa: BLE001 - cosmetic; never break the render loop
            log.exception("status bar render failed")
            return [("#9e9e9e", "  status unavailable")]

    def _build_status_fragments(self):  # type: ignore[no-untyped-def]
        status = self._status()
        window = status.get("context_window", 0)
        used = status.get("tokens_used", 0)
        pct = f"{(used / window * 100):.0f}%" if window else "—"
        usage = f"{fmt_tokens(used)}/{fmt_tokens(window)} ({pct})" if window else fmt_tokens(used)
        mode = status.get("mode", "—")
        mode_color = _MODE_COLOR.get(mode, "#9e9e9e")
        sep = ("#6c6c6c", "  ·  ")
        frags = [
            ("#ff5f5f bold", f"  {APP_NAME}"), sep,
            (f"{mode_color} bold", mode), sep,
            ("#9e9e9e", status.get("model", "—")), sep,
            ("#9e9e9e", f"effort:{status.get('effort', '—')}"), sep,
            ("#9e9e9e", usage), sep,
            ("#9e9e9e", short_path(status.get("workspace", "—"))), sep,
            ("#9e9e9e", f"{status.get('tasks', 0)} tasks"),
        ]
        if not self.output.pinned:
            frags += [sep, ("#ffd75f bold", "scrolled ↑ (scroll down to return)")]
        return frags

    def _spinner_fragments(self):  # type: ignore[no-untyped-def]
        # Same rationale as _status_fragments: this runs every redraw with no
        # error boundary above it in prompt_toolkit's render loop.
        try:
            return self._build_spinner_fragments()
        except Exception:  # noqa: BLE001 - cosmetic; never break the render loop
            log.exception("spinner render failed")
            return [("#9e9e9e", "  …")]

    def _build_spinner_fragments(self):  # type: ignore[no-untyped-def]
        if self._approving["flag"]:
            prompt_text = self._approving.get("prompt", "")
            return [("#ffd75f bold", f"  {prompt_text}  allow? [y/N]")]
        if not self._thinking["flag"]:
            return [("", "")]
        if self._cancel_event is not None and self._cancel_event.is_set():
            return [("#ffd75f bold", "  ◐ stopping… "), ("#9e9e9e", "(esc pressed, finishing current step)")]
        if time.monotonic() < self._cancel_confirm_until:
            return [("#ffd75f bold", "  Esc again to interrupt "), ("#9e9e9e", "(continues if not confirmed)")]
        now = time.monotonic()
        if now - self._thinking["last_swap"] > 8.0:
            self._thinking["gerund"] = random.choice(_GERUNDS)
            self._thinking["last_swap"] = now
        elapsed = int(now - self._thinking["start"])
        star = _STAR_FRAMES[int(now * 6) % len(_STAR_FRAMES)]
        frags = [
            ("#ff5f5f", f"  {star} "),
            ("#ff5f5f", f"{self._thinking['gerund']}… "),
            ("#9e9e9e", f"({elapsed}s"),
            ("#9e9e9e", f" · ↑ {fmt_tokens(self._estimate_tokens())} tokens"),
            ("#9e9e9e", ")"),
        ]
        return frags

    # --- layout --------------------------------------------------------

    def _build_layout(self) -> Layout:
        output_window = Window(
            content=_ScrollableOutputControl(
                self.output.get_fragments,
                on_scroll_up=self._scroll_up,
                on_scroll_down=self._scroll_down,
                focusable=False,
            ),
            wrap_lines=True,
        )
        spinner_window = Window(content=FormattedTextControl(self._spinner_fragments), height=1)

        # Box border/caret color is a callable, not a static style, so it
        # re-evaluates every render — that's what makes it turn green live as
        # soon as the buffer starts with a DeepReid trigger word.
        def corner(ch: str) -> Window:
            return Window(FormattedTextControl(lambda: [(self._box_color(), ch)]), width=1, height=1)

        def hline() -> Window:
            return Window(char="─", style=self._box_color, height=1)

        input_window = Window(BufferControl(buffer=self._buf), wrap_lines=False, height=1)

        input_box = HSplit(
            [
                VSplit([corner("╭"), hline(), corner("╮")], height=1),
                VSplit(
                    [
                        Window(FormattedTextControl(lambda: [(self._box_color(), "│")]), width=1, height=1),
                        Window(FormattedTextControl(lambda: [(f"{self._box_color()} bold", " You"), ("#8a8a8a", " > ")]), width=7, height=1),
                        input_window,
                        Window(FormattedTextControl(lambda: [(self._box_color(), "│")]), width=1, height=1),
                    ],
                    height=1,
                ),
                VSplit([corner("╰"), hline(), corner("╯")], height=1),
            ]
        )
        status_window = Window(content=FormattedTextControl(self._status_fragments), height=1)

        # Subagent panel: appears directly under the input box (pushing it up
        # visually because HSplit re-layouts) whenever there are running or
        # recently-finished subagents. Sits above the status line so the
        # footer's app/mode/model/tokens readout stays the last row.
        subagent_panel = ConditionalContainer(
            content=Window(content=FormattedTextControl(self._subagent_fragments)),
            filter=Condition(self._subagent_rows_visible),
        )

        root = HSplit([output_window, spinner_window, input_box, subagent_panel, status_window])
        # Floats above the cursor position of the focused control (the input
        # buffer) — this is what makes the "/" completion menu pop up right
        # above/below the box as you type, instead of requiring /help.
        floated = FloatContainer(
            content=root,
            floats=[Float(xcursor=True, ycursor=True, content=_DarkCompletionsMenu())],
        )
        return Layout(floated, focused_element=input_window)

    # --- input handling --------------------------------------------------

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        is_thinking = Condition(lambda: self._thinking["flag"])
        is_approving = Condition(lambda: self._approving["flag"])
        # Left/Right only take over effort-cycling when the box is empty, so
        # they still move the cursor to fix a typo once you're typing —
        # unlike Up/Down (history), which are unconditional since a
        # single-line buffer has no other use for them.
        is_buffer_empty = Condition(lambda: not self._buf.text)

        @kb.add("enter", filter=~is_thinking & ~is_approving)
        async def _submit(event) -> None:  # type: ignore[no-untyped-def]
            buf = event.current_buffer
            if buf.complete_state is not None:
                # A completion menu is open — Enter accepts the highlighted
                # entry (or just closes the menu if nothing's highlighted
                # yet), matching every other tool's "/" menu. It does not
                # submit; that needs a second Enter once the text is filled in.
                completion = buf.complete_state.current_completion
                if completion is not None:
                    buf.apply_completion(completion)
                else:
                    buf.cancel_completion()
                return
            await self._on_submit()

        @kb.add("y", filter=is_approving)
        @kb.add("Y", filter=is_approving)
        def _approve_yes(event) -> None:  # type: ignore[no-untyped-def]
            self._resolve_approval(True)

        @kb.add("n", filter=is_approving)
        @kb.add("N", filter=is_approving)
        @kb.add("enter", filter=is_approving)
        def _approve_no(event) -> None:  # type: ignore[no-untyped-def]
            self._resolve_approval(False)

        @kb.add("escape", filter=is_thinking)
        def _cancel_turn(event) -> None:  # type: ignore[no-untyped-def]
            # Stops the in-flight response, not the session — the running turn
            # ends at its next safe point (see Agent.run_turn's `cancel` polling)
            # instead of the whole app exiting, matching Claude Code's Escape.
            now = time.monotonic()
            if now < self._cancel_confirm_until:
                if self._cancel_event is not None:
                    self._cancel_event.set()
                self._cancel_confirm_until = 0.0
            else:
                self._cancel_confirm_until = now + 2.0
            self.app.invalidate()

        @kb.add("c-c")
        def _clear_line(event) -> None:  # type: ignore[no-untyped-def]
            if self._buf.text:
                self._buf.reset()

        @kb.add("c-d")
        def _exit(event) -> None:  # type: ignore[no-untyped-def]
            self.app.exit(result=0)

        @kb.add("c-o")
        def _toggle_collapse(event) -> None:  # type: ignore[no-untyped-def]
            self.output.toggle_expanded()
            self.app.invalidate()

        @kb.add("pageup")
        def _page_up(event) -> None:  # type: ignore[no-untyped-def]
            self._scroll_page_up()

        @kb.add("pagedown")
        def _page_down(event) -> None:  # type: ignore[no-untyped-def]
            self._scroll_page_down()

        @kb.add("home", filter=is_buffer_empty & ~is_thinking & ~is_approving)
        def _top(event) -> None:  # type: ignore[no-untyped-def]
            self._scroll_top()

        @kb.add("end", filter=is_buffer_empty & ~is_thinking & ~is_approving)
        def _bottom(event) -> None:  # type: ignore[no-untyped-def]
            self._scroll_bottom()

        @kb.add(Keys.BracketedPaste, filter=~is_approving)
        def _paste(event) -> None:  # type: ignore[no-untyped-def]
            data = event.data
            if "\n" in data or len(data) > _PASTE_COLLAPSE_CHARS:
                event.current_buffer.insert_text(self._collapse_paste(data))
            else:
                event.current_buffer.insert_text(data)

        @kb.add("left", filter=is_buffer_empty & ~is_thinking & ~is_approving)
        def _effort_prev(event) -> None:  # type: ignore[no-untyped-def]
            self._cycle_effort(-1)

        @kb.add("right", filter=is_buffer_empty & ~is_thinking & ~is_approving)
        def _effort_next(event) -> None:  # type: ignore[no-untyped-def]
            self._cycle_effort(1)

        return kb

    def _cycle_effort(self, delta: int) -> None:
        if self.orchestrator.state is None:
            return
        session = self.orchestrator.state.session
        try:
            idx = _EFFORT_LEVELS.index(session.reasoning_effort)
        except ValueError:
            idx = 0
        session.reasoning_effort = _EFFORT_LEVELS[(idx + delta) % len(_EFFORT_LEVELS)]
        self.orchestrator.session_store.update(session)
        self.app.invalidate()

    def _deepread_prefix_len(self) -> int:
        """Length of a DeepReid trigger word at the start of the buffer, or 0
        if there isn't one — 0 also means "not triggered", so this doubles as
        the truthiness check. Requires a word boundary right after the
        trigger (end-of-text or whitespace) so "deepreading..." doesn't
        false-positive on "deepread"."""
        text = self._buf.text.lstrip()
        lead = len(self._buf.text) - len(text)
        lowered = text.lower()
        for trigger in _DEEPREID_TRIGGERS:
            if lowered.startswith(trigger):
                rest = text[len(trigger) :]
                if not rest or rest[0].isspace():
                    return lead + len(trigger)
        return 0

    def _box_color(self) -> str:
        # Faster pulse while the pipeline is actually working, gentler pulse
        # while just sitting there with the trigger typed but not submitted.
        if self._deepreid_running:
            idx = int(time.monotonic() * 6) % len(_DEEPREID_PULSE_SHADES)
            return _DEEPREID_PULSE_SHADES[idx]
        if self._deepread_prefix_len():
            idx = int(time.monotonic() * 2) % len(_DEEPREID_PULSE_SHADES)
            return _DEEPREID_PULSE_SHADES[idx]
        return _BOX_COLOR_NORMAL

    def _collapse_paste(self, data: str) -> str:
        """Store a large/multi-line paste and return a short placeholder for
        the input box — same idea as Claude Code's own `[Pasted text]`
        collapse. The full text is substituted back in at submit time."""
        self._paste_counter += 1
        lines = data.count("\n") + 1
        label = f"[Pasted text #{self._paste_counter} +{lines} lines]" if lines > 1 else f"[Pasted text #{self._paste_counter} +{len(data)} chars]"
        self._pastes[label] = data
        return label

    def _expand_pastes(self, text: str) -> str:
        for label, data in self._pastes.items():
            text = text.replace(label, data)
        return text

    async def _on_submit(self) -> None:
        text = self._buf.text
        if not text.strip():
            return
        prefix_len = self._deepread_prefix_len()
        self._buf.reset()
        if prefix_len:
            task = self._expand_pastes(text[prefix_len:].lstrip())
            self._pastes.clear()
            await self._run_deepreid(task)
            return
        text = self._expand_pastes(text)
        self._pastes.clear()
        await self._submit_text(text)

    async def _run_deepreid(self, task: str) -> None:
        """Run the real Researcher->Planner->Critic pipeline (deepreid/pipeline.py)
        and render its Markdown output, instead of a normal single-agent turn."""
        if not task.strip():
            return
        self._append_output(lambda: render.console.print(Text(f"  DeepReid: {task}", style="bold #5fd75f")))
        self._deepreid_running = True
        self.app.invalidate()

        assert self._loop is not None
        loop = self._loop

        def progress(stage: str) -> None:
            loop.call_soon_threadsafe(lambda: self._append_output(lambda: render.print_info(f"  {stage}...")))

        try:
            result = await loop.run_in_executor(
                None,
                functools.partial(
                    run_deepreid,
                    self.orchestrator.config,
                    self.orchestrator.provider,
                    self.orchestrator.state.session.workspace,
                    task,
                    on_progress=progress,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the TUI must not die on runtime errors
            log.exception("deepreid failed")
            error_text = str(exc)
            self._append_output(lambda: render.print_error(error_text))
        else:
            path = save_deepreid_result(self.orchestrator.config, result)
            self._append_output(lambda: render.print_assistant(format_markdown(result)))
            self._append_output(lambda: render.print_info(f"saved to {path}"))
        finally:
            self._deepreid_running = False
            self.app.invalidate()

    async def _submit_text(self, text: str) -> None:
        """Run one turn for `text` — shared by the Enter key binding and by
        an injected initial prompt (`reidcli "<prompt>"` / piped stdin)."""
        if not text.strip():
            return

        if text.startswith("/"):
            outcome = self._run_slash(text)
            if outcome == "exit":
                self.app.exit(result=0)
            elif outcome.startswith("workflow-run:"):
                await self._run_workflow(outcome.split(":", 1)[1])
            elif outcome.startswith("prompt-run:"):
                await self._submit_text(outcome.split(":", 1)[1])
            elif outcome.startswith("deepreid-run:"):
                await self._run_deepreid(outcome.split(":", 1)[1])
            return

        self._append_output(lambda: render.print_user(text))
        self._thinking["flag"] = True
        self._thinking["start"] = time.monotonic()
        self._thinking["gerund"] = random.choice(_GERUNDS)
        self._thinking["last_swap"] = self._thinking["start"]
        self._cancel_confirm_until = 0.0
        self.app.invalidate()

        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        approver = self._make_approver()
        assert self._loop is not None
        try:
            result = await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.orchestrator.submit_task, text, approver=approver, cancel=cancel_event.is_set
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the TUI must not die on runtime errors
            log.exception("turn failed")
            error_text = str(exc)
            self._append_output(lambda: render.print_error(error_text))
        else:
            seconds = int(time.monotonic() - self._thinking["start"])
            self._emit_turn_result(result, seconds)
        finally:
            self._thinking["flag"] = False
            self._cancel_event = None
            self._cancel_confirm_until = 0.0
            self.app.invalidate()

    def _run_slash(self, text: str) -> str:
        if text.strip() == "/clear":
            self.output.reset()
            if self.app.is_running:
                self.app.invalidate()
            return "continue"
        outcome = "continue"

        def _do() -> None:
            nonlocal outcome
            outcome = handle_command(self.orchestrator, text)

        self._append_output(_do)
        return outcome

    async def _run_workflow(self, name: str) -> None:
        """Run a saved workflow's steps in sequence through `_submit_text`,
        so each step gets identical treatment to typing it in directly —
        slash commands and prompts both work, spinner/approval included."""
        workflow = self.orchestrator.workflow_store.get(name)
        if workflow is None:
            self._append_output(lambda: render.print_error(f"no such workflow: {name}"))
            return
        self._append_output(
            lambda: render.print_info(f"running workflow '{name}' ({len(workflow.steps)} steps)")
        )
        for step in workflow.steps:
            await self._submit_text(step)

    # --- approval bridge (worker thread <-> main loop thread) --------------

    def _make_approver(self) -> Callable[[str], bool]:
        loop = self._loop
        assert loop is not None

        def approve(prompt_text: str) -> bool:
            done = threading.Event()

            def _show() -> None:
                self._approving["prompt"] = prompt_text
                self._approving["result"] = False
                self._approving["event"] = done
                self._approving["flag"] = True
                self._append_output(lambda: render.console.print(Text(prompt_text, style=f"bold {WARN}")))

            loop.call_soon_threadsafe(_show)
            done.wait()
            return bool(self._approving["result"])

        return approve

    def _resolve_approval(self, value: bool) -> None:
        self._approving["result"] = value
        self._approving["flag"] = False
        event: threading.Event | None = self._approving["event"]
        self._approving["event"] = None
        self.app.invalidate()
        if event is not None:
            event.set()


def _fullscreen_run(orchestrator: Orchestrator, initial_prompt: str | None = None) -> int:
    """Entry point: build and run the full-screen chat app.

    Reuses an already-resumed session or starts fresh (matching the prior
    ui/repl.py::repl behavior). Returns 0 on a clean exit.

    If `initial_prompt` is given, it's submitted as the first turn as soon as
    the app starts rendering — the interactive equivalent of typing it into
    the box and pressing Enter, so the session stays open afterward (unlike
    `reidcli exec`, which runs one prompt headless and exits).
    """
    chat = ChatApp(orchestrator, initial_prompt=initial_prompt)
    original_console = render.console
    render.console = chat.capture.console
    try:
        chat.start()
        code = asyncio.run(chat.main())
    finally:
        render.console = original_console
    render.console.print(Text("bye.", style="dim"))
    return code or 0


def _terminal_header(orchestrator: Orchestrator) -> None:
    if orchestrator.state is None:
        return
    st = orchestrator.state.session
    render.console.print(
        Text.assemble(
            ("ReidCLI", f"bold {PRIMARY}"),
            ("  ·  ", DIM),
            (st.provider, DIM),
            ("  ·  ", DIM),
            (st.model, DIM),
            ("  ·  ", DIM),
            (st.permission_mode.value, DIM),
            ("  ·  ", DIM),
            (f"effort:{st.reasoning_effort}", DIM),
        )
    )
    render.console.print()


def _terminal_approve(prompt_text: str) -> bool:
    answer = render.console.input(f"[bold {WARN}]{prompt_text}[/] [dim]y/N[/] ")
    return answer.strip().lower() in ("y", "yes")


def _terminal_emit_result(result: dict, *, show_thinking: bool) -> None:
    thinking = (result.get("thinking") or "").strip()
    if thinking and show_thinking:
        render.print_thinking(thinking)
    render.print_tool_calls(result.get("tools", []))
    render.print_assistant(result["text"])


def _terminal_error_message(orchestrator: Orchestrator, exc: Exception) -> str:
    raw = str(exc).strip() or exc.__class__.__name__
    provider = orchestrator.state.session.provider if orchestrator.state is not None else "provider"
    lower = raw.lower()
    if "http 401" in lower or "invalid api key" in lower or "unauthorized" in lower:
        return (
            f"{provider} rejected the API key (401). "
            "Update it with /config set openai_api_key <key>, set OPENAI_API_KEY, or switch with /use stub."
        )
    if "connection error" in lower:
        return f"could not reach {provider}: {raw}"
    if len(raw) > 300:
        raw = raw[:297] + "..."
    return raw


def _terminal_turn_summary(orchestrator: Orchestrator, result: dict, seconds: int) -> Text:
    state = orchestrator.state
    prompt_tokens = state.last_usage_prompt_tokens if state else 0
    completion_tokens = state.last_usage_completion_tokens if state else 0
    total_tokens = prompt_tokens + completion_tokens
    tools = result.get("tools", [])
    files_read = sum(1 for entry in tools if entry.get("name") == "read_file" and entry.get("ok"))
    parts: list[tuple[str, str]] = [
        (f"    {SPARKLE} Thought {_format_elapsed(seconds)}", DIM),
        ("  ·  ", DIM),
        (f"↑ {fmt_tokens(total_tokens)} tokens", DIM),
    ]
    if files_read:
        parts.extend([("  ·  ", DIM), (f"{files_read} files read", DIM)])
    if tools:
        parts.extend([("  ·  ", DIM), (f"{len(tools)} tools", DIM)])
    return Text.assemble(*parts)


def _terminal_turn_summary_line(orchestrator: Orchestrator, result: dict, seconds: int) -> str:
    state = orchestrator.state
    prompt_tokens = state.last_usage_prompt_tokens if state else 0
    completion_tokens = state.last_usage_completion_tokens if state else 0
    total_tokens = prompt_tokens + completion_tokens
    tools = result.get("tools", [])
    files_read = sum(1 for entry in tools if entry.get("name") == "read_file" and entry.get("ok"))
    parts = [f"    {SPARKLE} Thought {_format_elapsed(seconds)}", f"↑ {fmt_tokens(total_tokens)} tokens"]
    if files_read:
        parts.append(f"{files_read} files read")
    if tools:
        parts.append(f"{len(tools)} tools")
    return "  ·  ".join(parts)


def _format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rem}s" if rem else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _poll_ctrl_b() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import msvcrt
    except ImportError:
        return False
    pressed = False
    while msvcrt.kbhit():
        ch = msvcrt.getwch()
        if ch == "\x02":
            pressed = True
    return pressed


def _poll_escape() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import msvcrt
    except ImportError:
        return False
    pressed = False
    while msvcrt.kbhit():
        ch = msvcrt.getwch()
        if ch == "\x1b":
            pressed = True
    return pressed


def _poll_terminal_keys() -> tuple[bool, bool]:
    if sys.platform != "win32":
        return False, False
    try:
        import msvcrt
    except ImportError:
        return False, False
    ctrl_b = False
    esc = False
    while msvcrt.kbhit():
        ch = msvcrt.getwch()
        if ch == "\x02":
            ctrl_b = True
        elif ch == "\x1b":
            esc = True
    return ctrl_b, esc


def _clear_thinking_line(width: int = 120) -> None:
    stream = render.console.file
    stream.write("\r" + (" " * width) + "\r")
    stream.flush()


def _clear_previous_status_line(width: int) -> None:
    stream = render.console.file
    stream.write("\x1b[1A\r" + (" " * width) + "\r")
    stream.flush()


def _thinking_line(
    seconds: int,
    frame: int,
    show_thinking: bool,
    *,
    confirm_cancel: bool = False,
    stopping: bool = False,
) -> str:
    colors = ("31", "91", "37", "90")
    color = colors[frame % len(colors)]
    logo = "\x1b[97m◇\x1b[0m"
    if stopping:
        detail = "stopping..."
    elif confirm_cancel:
        detail = "esc again to interrupt"
    else:
        detail = "details:on" if show_thinking else "ctrl+b details"
    return (
        f"\r    {logo} \x1b[{color}mThinking\x1b[0m"
        f"\x1b[90m {_format_elapsed(seconds)}  ·  {detail}\x1b[0m"
    )


def _run_turn_with_thinking(orchestrator: Orchestrator, text: str) -> tuple[dict, int, bool]:
    if not sys.stdout.isatty():
        started = time.monotonic()
        return orchestrator.submit_task(text, approver=_terminal_approve), int(time.monotonic() - started), False

    holder: dict = {"result": None, "error": None}
    show_thinking = False
    cancel_event = threading.Event()
    cancel_confirm_until = 0.0
    started = time.monotonic()

    def _worker() -> None:
        try:
            holder["result"] = orchestrator.submit_task(text, approver=_terminal_approve, cancel=cancel_event.is_set)
        except Exception as exc:  # noqa: BLE001 - surface after spinner clears
            holder["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    frame = 0
    while thread.is_alive():
        ctrl_b, esc = _poll_terminal_keys()
        if ctrl_b:
            show_thinking = not show_thinking
        now = time.monotonic()
        if esc:
            if now < cancel_confirm_until:
                cancel_event.set()
                cancel_confirm_until = 0.0
            else:
                cancel_confirm_until = now + 2.0
        render.console.file.write(
            _thinking_line(
                int(now - started),
                frame,
                show_thinking,
                confirm_cancel=now < cancel_confirm_until,
                stopping=cancel_event.is_set(),
            )
        )
        render.console.file.flush()
        frame += 1
        time.sleep(0.16)
    thread.join()
    _clear_thinking_line()
    if holder["error"] is not None:
        raise holder["error"]
    return holder["result"], int(time.monotonic() - started), show_thinking


def _terminal_submit(orchestrator: Orchestrator, text: str) -> tuple[str, str | None]:
    if text.startswith("/"):
        if text.strip() == "/clear":
            render.console.clear()
            return "continue", None
        outcome = handle_command(orchestrator, text)
        if outcome.startswith("prompt-run:"):
            return _terminal_submit(orchestrator, outcome.split(":", 1)[1])
        if outcome.startswith("deepreid-run:"):
            from reidcli.deepreid import format_markdown, run_deepreid, save_deepreid_result

            task = outcome.split(":", 1)[1]
            result = run_deepreid(orchestrator.config, orchestrator.provider, Path.cwd(), task, on_progress=render.print_info)
            path = save_deepreid_result(orchestrator.config, result)
            render.print_assistant(format_markdown(result))
            render.print_info(f"saved to {path}")
            return "continue", None
        return outcome, None
    try:
        result, seconds, show_thinking = _run_turn_with_thinking(orchestrator, text)
    except Exception as exc:  # noqa: BLE001 - keep interactive terminal alive
        render.print_error(_terminal_error_message(orchestrator, exc))
        render.console.print()
        return "continue", None
    summary = _terminal_turn_summary_line(orchestrator, result, seconds)
    _terminal_emit_result(result, show_thinking=show_thinking)
    render.console.print()
    return "continue", summary


def _terminal_run_workflow(orchestrator: Orchestrator, name: str) -> None:
    workflow = orchestrator.workflow_store.get(name)
    if workflow is None:
        render.print_error(f"no such workflow: {name}")
        return
    render.print_info(f"running workflow '{name}' ({len(workflow.steps)} steps)")
    for step in workflow.steps:
        outcome, _summary = _terminal_submit(orchestrator, step)
        if outcome == "exit":
            break


def _terminal_box_width() -> int:
    cols = shutil.get_terminal_size(fallback=(100, 30)).columns
    return max(48, cols - 4)


def _terminal_box_top() -> None:
    width = _terminal_box_width()
    render.console.print(Text("╭" + ("─" * (width - 2)) + "╮", style=PRIMARY))


def _terminal_box_bottom() -> None:
    width = _terminal_box_width()
    render.console.print(Text("╰" + ("─" * (width - 2)) + "╯", style=PRIMARY))


def _terminal_box_bottom_fragments() -> list[tuple[str, str]]:
    width = _terminal_box_width()
    return [(PRIMARY, "╰" + ("─" * (width - 2)) + "╯")]


def _terminal_prompt_fragments() -> list[tuple[str, str]]:
    return [
        ("#ff5f5f", "│"),
        ("#ff5f5f bold", " You"),
        ("#8a8a8a", " > "),
    ]


def _terminal_prompt_right_fragments() -> list[tuple[str, str]]:
    return [(PRIMARY, "│")]


def _terminal_read_input(orchestrator: Orchestrator, history: InMemoryHistory, status_line: str | None) -> str:
    width = _terminal_box_width()
    prompt_width = 8
    input_width = max(20, width - prompt_width - 1)
    text_buffer = Buffer(
        history=history,
        completer=SlashCommandCompleter(orchestrator),
        complete_while_typing=True,
        multiline=False,
    )

    kb = KeyBindings()

    @kb.add("enter")
    def _accept(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit(result=text_buffer.text)

    @kb.add("c-c")
    def _cancel(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit(exception=KeyboardInterrupt)

    top = Window(
        FormattedTextControl([(PRIMARY, "╭" + ("─" * (width - 2)) + "╮")]),
        width=Dimension.exact(width),
        height=1,
    )
    middle = VSplit(
        [
            Window(
                FormattedTextControl(
                    [
                        (PRIMARY, "│"),
                        (f"{PRIMARY} bold", " You"),
                        ("#8a8a8a", " > "),
                    ]
                ),
                width=Dimension.exact(prompt_width),
                height=1,
            ),
            Window(
                BufferControl(buffer=text_buffer),
                width=Dimension.exact(input_width),
                height=1,
                wrap_lines=False,
            ),
            Window(FormattedTextControl([(PRIMARY, "│")]), width=Dimension.exact(1), height=1),
        ],
        width=Dimension.exact(width),
        height=1,
    )
    bottom = Window(
        FormattedTextControl([(PRIMARY, "╰" + ("─" * (width - 2)) + "╯")]),
        width=Dimension.exact(width),
        height=1,
    )
    status = Window(
        FormattedTextControl([("#6f6f6f", status_line or "")]),
        width=Dimension.exact(width),
        height=1,
    )
    content = [top, middle, bottom]
    if status_line:
        content.append(status)
    root = FloatContainer(
        content=HSplit(content, width=Dimension.exact(width)),
        floats=[Float(xcursor=True, ycursor=True, content=_DarkCompletionsMenu())],
    )
    app = Application(
        layout=Layout(root, focused_element=text_buffer),
        key_bindings=kb,
        full_screen=False,
        erase_when_done=False,
        style=Style.from_dict(
            {
                "completion-menu": "#ff5f5f",
                "completion-menu.completion": "#ff5f5f",
                "completion-menu.completion.current": "#ff5f5f bold",
                "completion-menu.meta": "#ff5f5f",
                "completion-menu.meta.completion.current": "#ff8a8a",
            }
        ),
    )
    text = (app.run() or "").strip()
    if status_line:
        _clear_previous_status_line(width)
    return text


def _terminal_run(orchestrator: Orchestrator, initial_prompt: str | None = None) -> int:
    """Run the normal terminal chat loop with native scrollback."""
    if orchestrator.state is None:
        orchestrator.start_session(title="interactive")
    render.banner()
    _terminal_header(orchestrator)

    if initial_prompt and initial_prompt.strip():
        outcome, _summary = _terminal_submit(orchestrator, initial_prompt.strip())
        if outcome == "exit":
            render.console.print(Text("bye.", style=DIM))
            return 0
        if outcome.startswith("workflow-run:"):
            _terminal_run_workflow(orchestrator, outcome.split(":", 1)[1])

        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return 0

    history = InMemoryHistory()
    last_summary: str | None = None

    while True:
        try:
            text = _terminal_read_input(orchestrator, history, last_summary)
        except (EOFError, KeyboardInterrupt):
            render.console.print(Text("bye.", style=DIM))
            return 0
        if not text:
            continue
        outcome, summary = _terminal_submit(orchestrator, text)
        if summary is not None:
            last_summary = summary
        if outcome == "exit":
            render.console.print(Text("bye.", style=DIM))
            return 0
        if outcome.startswith("workflow-run:"):
            _terminal_run_workflow(orchestrator, outcome.split(":", 1)[1])


def run(orchestrator: Orchestrator, initial_prompt: str | None = None) -> int:
    return _terminal_run(orchestrator, initial_prompt=initial_prompt)
