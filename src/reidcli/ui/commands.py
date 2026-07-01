"""Slash command routing for the REPL.

Each command returns a string hint for the REPL loop:
  "continue"  -> keep the loop running
  "exit"      -> stop the loop
Commands mutate orchestrator/state in place. Add new commands here.
"""
from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from reidcli.policy.models import PermissionMode
from reidcli.runtime.orchestrator import Orchestrator
from reidcli.ui import render
from reidcli.ui.theme import APP_NAME, BOX, PRIMARY

# Grouped help for better scannability.
_HELP_SESSION = """\
[bold]Session[/]
  /status            show current session + mode + tasks
  /sessions          list all sessions
  /resume <id>       resume a prior session
  /transcript [n]    show last n messages (default 20)
  /rewind            drop the last turn from state
"""

_HELP_TASK = """\
[bold]Tasks[/]
  /tasks [status]    list tasks (filter: pending|active|completed|failed|blocked)
"""

_HELP_CONFIG = """\
[bold]Config & Policy[/]
  /model <name>      set model for the session
  /effort <level>    set reasoning effort (low|medium|high)
  /mode <mode>       set permission mode (strict|balanced|autonomous|custom)
  /permissions       show current policy + gates
  /tools             list registered tools with risk levels
"""

_EFFORT_LEVELS = ("low", "medium", "high")

_HELP_META = f"""\
[bold]Meta[/]
  /help              show this help
  /clear             clear the screen
  /exit              quit {APP_NAME}
"""

HELP = Group(
    Panel(Text(f"{APP_NAME} commands", style=f"bold {PRIMARY}"), box=BOX, border_style=PRIMARY, padding=(0, 2)),
    Text(_HELP_SESSION),
    Text(_HELP_TASK),
    Text(_HELP_CONFIG),
    Text(_HELP_META),
)


def _set_mode(orchestrator: Orchestrator, value: str) -> bool:
    try:
        mode = PermissionMode(value)
    except ValueError:
        render.print_error(f"unknown mode: {value}")
        return False
    orchestrator.set_permission_mode(mode)
    render.print_info(f"mode → {mode.value}")
    return True


def handle(orchestrator: Orchestrator, line: str) -> str:
    parts = line.strip().split(None, 1)
    cmd = parts[0].lstrip("/")
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("help", "?"):
        render.console.print(HELP)
    elif cmd == "status":
        if orchestrator.state:
            chars = sum(len(m.content or "") for m in orchestrator.state.messages)
            render.status_bar(
                orchestrator.state.session,
                orchestrator.state.effective_mode,
                len(orchestrator.list_tasks()),
                tokens_used=max(1, chars // 4),
            )
        else:
            render.print_info("no active session")
    elif cmd == "sessions":
        render.print_sessions(orchestrator.session_store.list())
    elif cmd == "resume":
        if not arg:
            render.print_error("usage: /resume <session-id>")
        else:
            try:
                orchestrator.resume_session(arg)
                count = len(orchestrator.state.messages) if orchestrator.state else 0
                render.print_info(f"resumed {arg} ({count} messages restored)")
            except KeyError as exc:
                render.print_error(str(exc))
    elif cmd == "tasks":
        tasks = orchestrator.list_tasks()
        if arg:
            tasks = [t for t in tasks if t.status.value == arg]
        render.print_tasks(tasks)
    elif cmd == "transcript":
        if orchestrator.state is None:
            render.print_info("no active session")
        else:
            n = int(arg) if arg.isdigit() else 20
            render.print_transcript(orchestrator.state.messages, n)
    elif cmd == "model":
        if not arg or orchestrator.state is None:
            render.print_error("usage: /model <name> (with an active session)")
        else:
            orchestrator.state.session.model = arg
            orchestrator.session_store.update(orchestrator.state.session)
            render.print_info(f"model → {arg}")
    elif cmd == "effort":
        if orchestrator.state is None:
            render.print_error("usage: /effort <low|medium|high> (with an active session)")
        elif not arg:
            render.print_info(f"current effort: {orchestrator.state.session.reasoning_effort}")
        elif arg not in _EFFORT_LEVELS:
            render.print_error(f"unknown effort: {arg} (try low|medium|high)")
        else:
            orchestrator.state.session.reasoning_effort = arg
            orchestrator.session_store.update(orchestrator.state.session)
            render.print_info(f"effort → {arg}")
    elif cmd == "mode":
        if not arg:
            render.print_info(f"current mode: {orchestrator.policy.mode.value}")
        else:
            _set_mode(orchestrator, arg)
    elif cmd == "permissions":
        render.print_permissions(orchestrator.policy)
    elif cmd == "tools":
        render.print_tools(orchestrator.tools.definitions())
    elif cmd == "rewind":
        if orchestrator.state is None or not orchestrator.state.messages:
            render.print_info("nothing to rewind")
        else:
            orchestrator.rewind()
            render.print_info(f"rewound to {len(orchestrator.state.messages)} messages")
    elif cmd == "clear":
        render.console.clear()
    elif cmd in ("exit", "quit", "q"):
        return "exit"
    else:
        render.print_error(f"unknown command: /{cmd} (try /help)")
    return "continue"
