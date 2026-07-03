"""UI rendering helpers (Rich-based).

A Claude-Code-style skin: a rounded welcome box, ⏺ bullets for assistant turns
and tool calls, ⎿ connectors for tool results, and a low-noise status line.
Rendered in ReidVerse-Cli's red palette. Assistant output stays markdown with
syntax highlighting; tool calls hang under a bullet with their result.
"""
from __future__ import annotations

# ruff: noqa: E402
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

# The Claude-Code-style glyphs (✻ ⏺ ⎿ › ·) are non-ASCII; a legacy Windows
# codepage (cp1252) would raise UnicodeEncodeError mid-render. Force UTF-8 so
# the branding survives regardless of the host console's default encoding.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from reidcli.policy.engine import PolicyEngine
from reidcli.policy.models import PermissionMode
from reidcli.provider.base import Message
from reidcli.session.models import Session
from reidcli.tasks.models import Task
from reidcli.ui.theme import (
    APP_NAME,
    BOX,
    BULLET,
    DANGER,
    DIM,
    MAX_WIDTH,
    MODE_STYLE,
    PRIMARY,
    PROMPT,
    RISK_STYLE,
    ROLE_ICON,
    ROLE_STYLE,
    SPARKLE,
    STATUS_STYLE,
    SUCCESS,
    TREE,
    WARN,
    context_window_for,
    fmt_tokens,
    short_path,
)

console = Console()


# --- Thinking spinner constants (Claude-Code-style: "✻ Gerund… (12s · ↑ 2.1k tokens)") ---
# Rendered natively by ui.app's persistent footer (prompt_toolkit), not Rich —
# the spinner ticks too often (~8Hz) to round-trip through the console-capture
# bridge each frame. These constants are shared so both look the same.

_GERUNDS = [
    "Flibbertigibbeting", "Cogitating", "Percolating", "Ruminating", "Noodling",
    "Conjuring", "Finagling", "Marinating", "Puttering", "Simmering",
    "Frolicking", "Galavanting", "Bamboozling", "Whirring", "Pondering",
    "Scheming", "Effervescing", "Transmuting", "Kerfuffling", "Wrangling",
    "Vibing", "Tinkering", "Synthesizing", "Meandering", "Incubating",
    "Hornswoggling", "Discombobulating", "Moseying", "Percolatin'", "Brewing",
]


_STAR_FRAMES = "✶✸✹✺✹✷"


def _bullet_grid(marker: Text, body) -> Table:  # type: ignore[no-untyped-def]
    """A two-column grid: a bullet marker + hanging-indented body.

    Wrapped lines in the body align under the body column, mirroring the
    Claude Code '⏺ text' layout.
    """
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row(marker, body)
    return grid


# Mascot ASCII art, printed to the left of the welcome panel in the empty
# space there. Purely decorative — kept as its own constant so it's easy to
# swap out.
_MASCOT = r"""                 #####
            ###############
         ######################
       ##########################
      ##########  ##  ###########
    ##### ####   ##################
   ######      #####################
  ### ##         ###################
  ####### ###   #####################
 ############################### ####
 ############### ###################
 ## ## ## ###################  #####
    ##   ##  ################## ####
   #          ############### ##  ##
               ##### ### ### ##  ##
                 ##   ## ##  ##
                  ##   ##    ##"""


def banner() -> None:
    """Claude-Code-style welcome box, with a mascot to its left."""
    from reidcli import __version__

    body = Text.assemble(
        (f"{SPARKLE} ", PRIMARY),
        ("Welcome to ", "bold"),
        (APP_NAME, f"bold {PRIMARY}"),
        ("!", "bold"),
        (f"  v{__version__}\n\n", DIM),
        ("  /help for help, /status for your current setup\n\n", DIM),
        ("  cwd: ", DIM),
        (str(Path.cwd()), DIM),
    )
    cols = shutil.get_terminal_size(fallback=(100, 30)).columns
    mascot_width = max(len(line) for line in _MASCOT.splitlines())
    show_mascot = cols >= mascot_width + 50
    panel_width = min(MAX_WIDTH, max(44, cols - 4))
    if show_mascot:
        panel_width = min(MAX_WIDTH, max(44, cols - mascot_width - 6))
    panel = Panel(body, box=BOX, border_style=PRIMARY, padding=(0, 1), width=panel_width)

    if show_mascot:
        grid = Table.grid(padding=(0, 2))
        grid.add_column()
        grid.add_column()
        grid.add_row(Text(_MASCOT, style=PRIMARY), panel)
        console.print(grid)
    else:
        console.print(panel)


def status_line_text(status: dict) -> Text:
    """Build the full status line: app · mode · model · effort · tokens · cwd · tasks."""
    mode = status.get("mode", "—")
    model = status.get("model", "—")
    effort = status.get("effort", "—")
    used = status.get("tokens_used", 0)
    window = status.get("context_window", 0)
    workspace = status.get("workspace", "—")
    tasks = status.get("tasks", 0)

    mode_color = MODE_STYLE.get(mode, DIM)
    sep = ("  ·  ", DIM)
    pct = f"{(used / window * 100):.0f}%" if window else "—"
    usage = f"{fmt_tokens(used)}/{fmt_tokens(window)} ({pct})" if window else fmt_tokens(used)

    return Text.assemble(
        (f"  {APP_NAME}", f"bold {PRIMARY}"), sep,
        (mode, f"bold {mode_color}"), sep,
        (model, DIM), sep,
        (f"effort:{effort}", DIM), sep,
        (usage, DIM), sep,
        (short_path(workspace), DIM), sep,
        (f"{tasks} tasks", WARN if tasks else DIM),
    )


def status_bar(session: Session | None, mode: PermissionMode, task_count: int = 0, tokens_used: int = 0) -> None:
    """Low-noise status line, Claude-Code-style — dim, dotted, no box."""
    if session is None:
        console.print(Text("  no active session", style=DIM))
        return
    console.print(
        status_line_text(
            {
                "mode": mode.value,
                "model": session.model,
                "effort": session.reasoning_effort,
                "tokens_used": tokens_used,
                "context_window": context_window_for(session.model),
                "workspace": str(session.workspace),
                "tasks": task_count,
            }
        )
    )


def status_prompt(session: Session | None, mode: PermissionMode | None) -> Text:
    """Input caret, Claude-Code-style. Session context lives in the status line."""
    return Text(f"{PROMPT} ", style=f"bold {PRIMARY}")


def print_user(text: str) -> None:
    """Echo the submitted prompt after the input box collapses, under a small
    "User" label, with extra blank lines before/after to clearly separate it
    from whatever came before (the prior reply) and after (this turn's
    reply)."""
    console.print()
    console.print(Text("You", style=DIM))
    console.print(Text(text, style="bold"))
    console.print()


def print_thinking(text: str) -> None:
    """Chain-of-thought shown above the answer: dim italic under a ✻ marker."""
    if not text or not text.strip():
        return
    console.print(_bullet_grid(Text(SPARKLE, style=DIM), Text(text.strip(), style="dim italic")))


def print_assistant(text: str) -> None:
    """Markdown assistant output hanging under a ⏺ bullet."""
    console.print()
    console.print(Text("Reid", style=f"bold {PRIMARY}"))
    console.print()
    console.print(Markdown(text))
    console.print()


def print_tool_calls(tool_log: list[dict]) -> None:
    """Tool calls as ⏺ Name(args) with a ⎿ result line beneath each."""
    if not tool_log:
        return
    for entry in tool_log:
        name = entry["name"]
        ok = entry["ok"]
        error = entry.get("error", "")
        args = entry.get("args", {})
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
        header = Text.assemble(
            (name, "bold"), ("(", DIM), (args_str, DIM), (")", DIM),
        )
        console.print(_bullet_grid(Text(BULLET, style=PRIMARY), header))
        if ok:
            result = Text("ok", style=SUCCESS)
        else:
            result = Text(f"Error: {error}", style=DANGER)
        console.print(Text.assemble(("  ", ""), (TREE, DIM), ("  ", ""), result))


def print_tasks(tasks: list[Task]) -> None:
    if not tasks:
        console.print(Text("no tasks", style=DIM))
        return
    table = Table(title="tasks", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("id", style=DIM, width=12)
    table.add_column("status", width=12)
    table.add_column("title")
    for t in tasks:
        color = STATUS_STYLE.get(t.status.value, "white")
        table.add_row(t.id, Text(t.status.value, style=f"bold {color}"), t.title)
    console.print(table)
    # Summary line.
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    parts = []
    for k, v in counts.items():
        color = STATUS_STYLE.get(k, DIM)
        parts.append(Text(f"{v} {k}", style=color))
    summary = Text("  ").join(parts)
    console.print(summary)


def print_sessions(sessions: list[Session]) -> None:
    if not sessions:
        console.print(Text("no sessions", style=DIM))
        return
    table = Table(title="sessions", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("id", style=DIM, width=14)
    table.add_column("status", width=10)
    table.add_column("title")
    table.add_column("updated", width=12)
    table.add_column("workspace", style=DIM)
    now = datetime.now(UTC)
    for s in sessions:
        color = STATUS_STYLE.get(s.status.value, "white")
        age = now - s.updated_at
        mins = int(age.total_seconds() // 60)
        when = f"{mins}m ago" if mins < 60 else f"{mins // 60}h ago"
        table.add_row(
            s.id,
            Text(s.status.value, style=f"bold {color}"),
            s.title,
            when,
            str(s.workspace),
        )
    console.print(table)


def print_workflows(workflows: list) -> None:  # type: ignore[no-untyped-def]
    if not workflows:
        console.print(Text("no workflows", style=DIM))
        return
    table = Table(title="workflows", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("name", style="bold", width=18)
    table.add_column("steps", width=6)
    table.add_column("description")
    for wf in workflows:
        table.add_row(wf.name, str(len(wf.steps)), wf.description or "(none)")
    console.print(table)


def print_workflow_steps(workflow) -> None:  # type: ignore[no-untyped-def]
    console.print(Text.assemble((workflow.name, f"bold {PRIMARY}"), ("  ", ""), (workflow.description, DIM)))
    for i, step in enumerate(workflow.steps, 1):
        console.print(Text.assemble((f"  {i}. ", DIM), (step, "white")))


def print_providers(records, active_name: str, extra_names: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Show connected providers.

    `records` is the persisted list (name, kind, base_url, default_model);
    `extra_names` is any provider registered but not persisted (e.g. `stub`,
    or `anthropic` picked up from env vars) — those show as kind=built-in.
    `active_name` is the current session provider (highlighted).
    """
    table = Table(title="providers", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("name", style="bold", width=18)
    table.add_column("kind", width=20)
    table.add_column("model", width=22)
    table.add_column("base_url")
    for name in extra_names:
        marker = "● " if name == active_name else "  "
        table.add_row(f"{marker}{name}", "built-in", "-", "-")
    for r in records:
        marker = "● " if r.name == active_name else "  "
        table.add_row(f"{marker}{r.name}", r.kind, r.default_model or "-", r.base_url or "-")
    console.print(table)
    console.print(Text("  ● = active provider (use /use <name> to switch)", style=DIM))


def print_permissions(policy: PolicyEngine) -> None:
    """Structured permissions view using a table for readability."""
    cfg = policy.config.policy
    table = Table(title="permissions", box=BOX, show_header=False, border_style=PRIMARY, padding=(0, 1), width=MAX_WIDTH)
    table.add_column("key", style=DIM, width=18)
    table.add_column("value")
    mode_color = MODE_STYLE.get(policy.mode.value, DIM)
    table.add_row("mode", Text(policy.mode.value, style=f"bold {mode_color}"))
    table.add_row("blocked commands", ", ".join(sorted(policy.blocked_commands)) or "(none)")
    table.add_row("allowed commands", ", ".join(sorted(policy.allowed_commands)) or "(none)")
    table.add_row(
        "writable roots",
        ", ".join(str(r) for r in cfg.additional_writable_roots) or "(workspace only)",
    )
    table.add_row("read-only paths", ", ".join(str(r) for r in cfg.read_only_paths) or "(none)")
    table.add_row("shell timeout", f"{cfg.shell_timeout_seconds}s")
    console.print(table)


def print_transcript(messages: list[Message], n: int = 20) -> None:
    if not messages:
        console.print(Text("no transcript", style=DIM))
        return
    for m in messages[-n:]:
        icon = ROLE_ICON.get(m.role, "·")
        style = ROLE_STYLE.get(m.role, "white")
        if m.tool_calls:
            calls = ", ".join(c.name for c in m.tool_calls)
            console.print(Text.assemble((f"{icon} ", style), (f"{m.role} ", f"bold {style}"), (f"tools: {calls}", DIM)))
        else:
            text = m.content[:300] + ("…" if len(m.content) > 300 else "")
            console.print(Text.assemble((f"{icon} ", style), (f"{m.role} ", f"bold {style}"), (text, "white")))


def print_tools(definitions: list) -> None:  # type: ignore[no-untyped-def]
    """Grouped tool listing with risk badges."""
    table = Table(title="tools", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY, width=MAX_WIDTH)
    table.add_column("name", style="bold", width=18)
    table.add_column("risk", width=8)
    table.add_column("description")
    for d in definitions:
        risk_color = RISK_STYLE.get(d.risk.value, DIM)
        table.add_row(d.name, Text(d.risk.value, style=f"bold {risk_color}"), d.description)
    console.print(table)


def print_error(text: str) -> None:
    """Inline red error under a ⏺ bullet, Claude-Code-style."""
    console.print(_bullet_grid(Text(BULLET, style=DANGER), Text(f"Error: {text}", style=DANGER)))


def print_info(text: str) -> None:
    console.print(Text(text, style=DIM))


def print_warn(text: str) -> None:
    console.print(Text(text, style=WARN))


def rule(title: str = "") -> None:
    """Horizontal separator between sections/turns."""
    if title:
        console.rule(Text(title, style=DIM), style=DIM, align="left")
    else:
        console.rule(style=DIM, align="left")


# Backward-compat aliases for any callers expecting the old names.
status_line = status_bar
