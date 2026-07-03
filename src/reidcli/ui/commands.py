"""Slash command routing for the REPL.

Each command returns a string hint for the REPL loop:
  "continue"  -> keep the loop running
  "exit"      -> stop the loop
Commands mutate orchestrator/state in place. Add new commands here — and add
a matching entry to SLASH_COMMANDS (or WORKFLOW_SUBCOMMANDS) below, which is
the single source both /help and the "/" completion menu (ui/app.py) render
from, so they can't drift out of sync.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from reidcli.config.loader import ConfigLoader
from reidcli.policy.models import PermissionMode
from reidcli.provider.base import Message
from reidcli.provider.store import SUPPORTED_KINDS, ProviderRecord, ProviderStore, build_provider
from reidcli.runtime.orchestrator import Orchestrator
from reidcli.session.models import Session
from reidcli.ui import render
from reidcli.ui.theme import APP_NAME, BOX, PRIMARY
from reidcli.workflows.models import Workflow

_EFFORT_LEVELS = ("low", "medium", "high", "xhigh")

# (command, args-hint, description, help-group). Order here is display order.
SLASH_COMMANDS: list[tuple[str, str, str, str]] = [
    ("/status", "", "show current session + mode + tasks", "Session"),
    ("/sessions", "", "list all sessions", "Session"),
    ("/sessions search", "<text>", "search saved transcripts for text", "Session"),
    ("/session", "<rename|delete> ...", "rename or delete sessions", "Session"),
    ("/resume", "<id>", "resume a prior session", "Session"),
    ("/transcript", "[n]", "show last n messages (default 20)", "Session"),
    ("/rewind", "", "drop the last turn from state", "Session"),
    ("/undo", "", "remove last assistant/tool output but keep the prompt", "Session"),
    ("/retry", "", "rerun the last user prompt", "Session"),
    ("/edit", "", "edit the last user prompt in $EDITOR and rerun it", "Session"),
    ("/fork", "[n]", "start a new session copied from the last n messages", "Session"),
    ("/export", "[md|json]", "export current transcript to storage", "Session"),
    ("/compact", "[keep]", "summarize older context and keep recent messages", "Session"),
    ("/tasks", "[status]", "list tasks (filter: pending|active|completed|failed|blocked)", "Tasks"),
    ("/usage", "", "show current context and last provider token usage", "Config & Policy"),
    ("/config", "<get|set> ...", "inspect or update supported config keys", "Config & Policy"),
    ("/env", "", "show runtime paths, shell, provider, and masked key status", "Config & Policy"),
    ("/pwd", "", "show current session workspace", "Config & Policy"),
    ("/cd", "<path>", "change current session workspace", "Config & Policy"),
    ("/model", "<name>", "set model for the session", "Config & Policy"),
    ("/models", "", "show available provider/model choices known locally", "Config & Policy"),
    ("/effort", "<level>", "set reasoning effort (low|medium|high|xhigh)", "Config & Policy"),
    ("/mode", "<mode>", "set permission mode (strict|balanced|autonomous|custom)", "Config & Policy"),
    ("/plan", "[on|off]", "toggle plan-first guidance for later turns", "Config & Policy"),
    ("/nyx", "[on|off]", "toggle Nyx redteam/offensive-security persona", "Config & Policy"),
    ("/permissions", "", "show current policy + gates", "Config & Policy"),
    ("/tools", "", "list registered tools with risk levels", "Config & Policy"),
    ("/tools enable", "<name>", "enable a tool for this session", "Config & Policy"),
    ("/tools disable", "<name>", "disable a tool for this session", "Config & Policy"),
    ("/approvals", "", "show approval policy state and recent gate capability", "Config & Policy"),
    ("/cost", "", "show token/cost estimate for current session", "Config & Policy"),
    ("/web", "[on|off]", "toggle web/search tools for this session", "Config & Policy"),
    ("/workflows", "", "list saved workflows", "Workflows"),
    ("/workflow", "<run|save|show|delete> ...", "manage saved workflows", "Workflows"),
    ("/save", "<name> [text]", "save a reusable prompt snippet", "Prompts"),
    ("/load", "<name>", "show a saved prompt snippet", "Prompts"),
    ("/prompt", "<name> [args...]", "run a saved prompt template with arguments", "Prompts"),
    ("/providers", "", "list registered providers (stub is always default)", "Providers"),
    ("/connect", "<name> <kind> <base_url> [api_key] [model]", "add a provider (kind: anthropic|openai|openai-compatible|ollama)", "Providers"),
    ("/disconnect", "<name>", "remove a saved provider", "Providers"),
    ("/use", "<name>", "switch this session to a registered provider", "Providers"),
    ("/mcp", "<list|connect|disconnect> ...", "manage MCP server config stubs", "Integrations"),
    ("/agents", "", "show active and recently finished subagents", "Agents"),
    ("/agent", "<name> <prompt>", "ask a named child agent to work on a task", "Agents"),
    ("/deepreid", "<task>", "run Researcher/Planner/Critic review pipeline", "Agents"),
    ("/theme", "[name]", "view or set terminal theme preference", "Meta"),
    ("/keys", "", "show keybindings", "Meta"),
    ("/update", "", "show local version and update guidance", "Meta"),
    ("/help", "", "show this help", "Meta"),
    ("/clear", "", "clear the screen", "Meta"),
    ("/exit", "", f"quit {APP_NAME}", "Meta"),
]

# (subcommand, args-hint, description) for "/workflow <subcommand>".
WORKFLOW_SUBCOMMANDS: list[tuple[str, str, str]] = [
    ("run", "<name>", "run a workflow's steps in sequence"),
    ("save", "<name> [n]", "save the last n user turns as a workflow (default 5)"),
    ("show", "<name>", "show a workflow's steps"),
    ("delete", "<name>", "delete a workflow"),
]


def _build_help() -> Group:
    def section(header: str, body: str) -> Text:
        # Text(..., style=...) applies to just the constructor's own content
        # (the header); .append() with no style keeps the body literal — this
        # avoids Text.from_markup(), which would otherwise parse literal "["
        # in args hints like "[n]"/"[status]" as (invalid) markup tags and
        # silently swallow them.
        text = Text(f"{header}\n", style="bold")
        text.append(f"{body}\n")
        return text

    groups: dict[str, list[str]] = {}
    for cmd, args, desc, group in SLASH_COMMANDS:
        left = f"{cmd} {args}".rstrip()
        groups.setdefault(group, []).append(f"  {left:<28} {desc}")

    parts = [Panel(Text(f"{APP_NAME} commands", style=f"bold {PRIMARY}"), box=BOX, border_style=PRIMARY, padding=(0, 2))]
    for group, lines in groups.items():
        parts.append(section(group, "\n".join(lines)))

    sub_lines = "\n".join(f"    /workflow {name:<8} {args:<14} {desc}" for name, args, desc in WORKFLOW_SUBCOMMANDS)
    parts.append(section("Workflow subcommands", sub_lines))

    parts.append(
        section(
            "Tip",
            "  Type / to see a completion menu for every command above — Tab/↓ to select, Enter to accept.",
        )
    )
    return Group(*parts)


HELP = _build_help()


@dataclass(frozen=True)
class _Snippet:
    name: str
    text: str
    updated_at: str


def _storage_root(orchestrator: Orchestrator) -> Path:
    return orchestrator.config.storage_root or (Path.home() / ".reidcli")


def _json_path(orchestrator: Orchestrator, name: str) -> Path:
    root = _storage_root(orchestrator)
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def _read_json(path: Path, default):  # type: ignore[no-untyped-def]
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else default
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _split_args(arg: str) -> list[str] | None:
    try:
        parts = shlex.split(arg, posix=(os.name != "nt"))
    except ValueError as exc:
        render.print_error(f"invalid arguments: {exc}")
        return None
    return [part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in ("'", '"') else part for part in parts]


def _require_state(orchestrator: Orchestrator) -> bool:
    if orchestrator.state is None:
        render.print_error("no active session")
        return False
    return True


def _last_user_message(orchestrator: Orchestrator) -> str | None:
    if orchestrator.state is None:
        return None
    for msg in reversed(orchestrator.state.messages):
        if msg.role == "user" and msg.content.strip():
            return msg.content
    return None


def _rewrite_transcript(orchestrator: Orchestrator) -> None:
    if orchestrator.state is None:
        return
    sid = orchestrator.state.session.id
    path = orchestrator.session_store.session_dir(sid) / "transcript.jsonl"
    messages = orchestrator.state.messages
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(m.model_dump_json() for m in messages) + ("\n" if messages else ""),
        encoding="utf-8",
    )
    orchestrator.session_store.update(orchestrator.state.session)


def _simple_summary(messages: list[Message], limit: int = 20) -> str:
    lines = []
    for msg in messages[-limit:]:
        content = " ".join((msg.content or "").split())
        if not content and msg.tool_calls:
            content = "tool calls: " + ", ".join(call.name for call in msg.tool_calls)
        if content:
            lines.append(f"- {msg.role}: {content[:240]}")
    return "\n".join(lines) or "- no content"


def _snippets_path(orchestrator: Orchestrator) -> Path:
    return _json_path(orchestrator, "prompts.json")


def _load_snippets(orchestrator: Orchestrator) -> dict[str, _Snippet]:
    raw = _read_json(_snippets_path(orchestrator), {"prompts": []})
    out: dict[str, _Snippet] = {}
    for item in raw.get("prompts", []):
        if isinstance(item, dict) and item.get("name") and isinstance(item.get("text"), str):
            out[item["name"]] = _Snippet(
                name=item["name"],
                text=item["text"],
                updated_at=item.get("updated_at", ""),
            )
    return out


def _save_snippets(orchestrator: Orchestrator, snippets: dict[str, _Snippet]) -> None:
    _write_json(
        _snippets_path(orchestrator),
        {"prompts": [s.__dict__ for s in sorted(snippets.values(), key=lambda item: item.name)]},
    )


def _mcp_path(orchestrator: Orchestrator) -> Path:
    return _json_path(orchestrator, "mcp_servers.json")


def _theme_path(orchestrator: Orchestrator) -> Path:
    return _json_path(orchestrator, "theme.json")


def _set_runtime_flag(orchestrator: Orchestrator, name: str, value: bool) -> None:
    setattr(orchestrator, name, value)
    if getattr(orchestrator, "agent", None) is not None:
        orchestrator.agent.context_extras[name] = value


def _runtime_flag(orchestrator: Orchestrator, name: str, default: bool = False) -> bool:
    return bool(getattr(orchestrator, name, default))


def _disabled_tools(orchestrator: Orchestrator) -> set[str]:
    disabled = getattr(orchestrator, "disabled_tools", None)
    if disabled is None:
        disabled = set()
        orchestrator.disabled_tools = disabled
        if getattr(orchestrator, "agent", None) is not None:
            orchestrator.agent.context_extras["disabled_tools"] = disabled
    return disabled


def _session_by_id(orchestrator: Orchestrator, session_id: str) -> Session | None:
    session = orchestrator.session_store.get(session_id)
    if session is None:
        matches = [s for s in orchestrator.session_store.list() if s.id.startswith(session_id)]
        if len(matches) == 1:
            return matches[0]
    return session


def _set_mode(orchestrator: Orchestrator, value: str) -> bool:
    try:
        mode = PermissionMode(value)
    except ValueError:
        render.print_error(f"unknown mode: {value}")
        return False
    orchestrator.set_permission_mode(mode)
    render.print_info(f"mode → {mode.value}")
    return True


def _handle_nyx(orchestrator: Orchestrator, arg: str) -> None:
    value = arg.strip().lower()
    if not value:
        render.print_info(f"nyx: {'on' if orchestrator.nyx_enabled else 'off'}")
        return
    if value not in ("on", "off"):
        render.print_error("usage: /nyx [on|off]")
        return
    orchestrator.set_nyx(value == "on")
    render.print_info(f"nyx → {value}")


def _handle_usage(orchestrator: Orchestrator) -> None:
    st = orchestrator.state
    if st is None:
        render.print_info("no active session")
        return
    chars = sum(len(m.content or "") for m in st.messages)
    estimate = max(1, chars // 4) if chars else 0
    real = st.last_usage_prompt_tokens + st.last_usage_completion_tokens
    table = Table(box=BOX, expand=False, show_header=False)
    table.add_column("metric", style="dim")
    table.add_column("value")
    table.add_row("messages", str(len(st.messages)))
    table.add_row("chars", str(chars))
    table.add_row("estimated tokens", str(estimate))
    table.add_row("last prompt tokens", str(st.last_usage_prompt_tokens))
    table.add_row("last completion tokens", str(st.last_usage_completion_tokens))
    table.add_row("last total tokens", str(real))
    table.add_row("context sent max", "80 messages")
    render.console.print(table)


def _handle_sessions_search(orchestrator: Orchestrator, arg: str) -> None:
    query = arg.strip().lower()
    if not query:
        render.print_error("usage: /sessions search <text>")
        return
    table = Table(title="session search", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY)
    table.add_column("id", style="dim", width=14)
    table.add_column("title", width=24)
    table.add_column("match")
    hits = 0
    for session in orchestrator.session_store.list():
        messages = orchestrator.session_store.read_messages(session.id, limit=1000)
        for msg in messages:
            content = (msg.content or "").replace("\n", " ")
            idx = content.lower().find(query)
            if idx >= 0:
                start = max(0, idx - 50)
                end = min(len(content), idx + len(query) + 80)
                table.add_row(session.id, session.title, content[start:end])
                hits += 1
                break
    if hits:
        render.console.print(table)
    else:
        render.print_info("no matching sessions")


def _handle_session(orchestrator: Orchestrator, arg: str) -> None:
    parts = _split_args(arg)
    if parts is None:
        return
    if not parts:
        render.print_error("usage: /session <rename|delete> ...")
        return
    sub = parts[0]
    if sub == "rename":
        if len(parts) == 1:
            render.print_error("usage: /session rename [id] <title>")
            return
        if len(parts) == 2:
            if not _require_state(orchestrator):
                return
            session = orchestrator.state.session
            title = parts[1]
        else:
            session = _session_by_id(orchestrator, parts[1])
            title = " ".join(parts[2:])
        if session is None:
            render.print_error(f"session not found: {parts[1]}")
            return
        session.title = title
        orchestrator.session_store.update(session)
        if orchestrator.state and orchestrator.state.session.id == session.id:
            orchestrator.state.session.title = title
        render.print_info(f"renamed session {session.id} -> {title}")
        return
    if sub == "delete":
        if len(parts) < 2:
            render.print_error("usage: /session delete <id> --yes")
            return
        session_id = parts[1]
        if "--yes" not in parts[2:]:
            render.print_error("refusing delete without --yes")
            return
        session = _session_by_id(orchestrator, session_id)
        if session is None:
            render.print_error(f"session not found: {session_id}")
            return
        if orchestrator.state and orchestrator.state.session.id == session.id:
            render.print_error("cannot delete active session")
            return
        target = orchestrator.session_store.session_dir(session.id)
        if target.exists():
            shutil.rmtree(target)
        render.print_info(f"deleted session {session.id}")
        return
    render.print_error(f"unknown /session subcommand: {sub} (try rename|delete)")


def _handle_export(orchestrator: Orchestrator, arg: str) -> None:
    if not _require_state(orchestrator):
        return
    fmt = (arg.strip() or "md").lower()
    if fmt not in ("md", "json"):
        render.print_error("usage: /export [md|json]")
        return
    state = orchestrator.state
    assert state is not None
    out_dir = _storage_root(orchestrator) / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{state.session.id}.{fmt}"
    if fmt == "json":
        payload = {
            "session": state.session.model_dump(mode="json"),
            "messages": [m.model_dump(mode="json") for m in state.messages],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    else:
        lines = [f"# {state.session.title or state.session.id}", ""]
        for msg in state.messages:
            if msg.role == "system":
                continue
            lines.extend([f"## {msg.role}", "", msg.content or "", ""])
        path.write_text("\n".join(lines), encoding="utf-8")
    render.print_info(f"exported transcript to {path}")


def _handle_compact(orchestrator: Orchestrator, arg: str) -> None:
    if not _require_state(orchestrator):
        return
    state = orchestrator.state
    assert state is not None
    keep = int(arg) if arg.isdigit() else 12
    if keep < 2:
        render.print_error("usage: /compact [keep]  (keep must be >= 2)")
        return
    system = state.messages[:1] if state.messages and state.messages[0].role == "system" else []
    body = state.messages[len(system):]
    if len(body) <= keep:
        render.print_info("nothing to compact")
        return
    older = body[:-keep]
    recent = body[-keep:]
    summary = Message(
        role="system",
        content="Compact session summary of earlier messages:\n" + _simple_summary(older, limit=80),
    )
    state.messages = [*system, summary, *recent]
    _rewrite_transcript(orchestrator)
    render.print_info(f"compacted {len(older)} older messages; kept {len(recent)} recent messages")


def _handle_undo(orchestrator: Orchestrator) -> None:
    if not _require_state(orchestrator):
        return
    state = orchestrator.state
    assert state is not None
    last_user = None
    for i in range(len(state.messages) - 1, -1, -1):
        if state.messages[i].role == "user":
            last_user = i
            break
    if last_user is None or last_user == len(state.messages) - 1:
        render.print_info("nothing to undo")
        return
    removed = len(state.messages) - last_user - 1
    del state.messages[last_user + 1 :]
    _rewrite_transcript(orchestrator)
    render.print_info(f"undid {removed} assistant/tool messages")


def _handle_fork(orchestrator: Orchestrator, arg: str) -> None:
    if not _require_state(orchestrator):
        return
    state = orchestrator.state
    assert state is not None
    n = int(arg) if arg.isdigit() else len(state.messages)
    copied = state.messages[-n:] if n > 0 else []
    old = state.session
    new_session = Session(
        title=f"{old.title or old.id} fork",
        workspace=old.workspace,
        provider=old.provider,
        model=old.model,
        reasoning_effort=old.reasoning_effort,
        permission_mode=old.permission_mode,
    )
    orchestrator.session_store.create(new_session)
    for msg in copied:
        orchestrator.session_store.append_message(new_session.id, msg)
    orchestrator.resume_session(new_session.id)
    render.print_info(f"forked to {new_session.id} ({len(copied)} messages copied)")


def _handle_config(orchestrator: Orchestrator, arg: str) -> None:
    parts = _split_args(arg)
    if parts is None:
        return
    if not parts or parts[0] not in ("get", "set"):
        render.print_error("usage: /config <get|set> <key> [value]")
        return
    cfg = orchestrator.config
    supported = {
        "default_provider": ("default_provider", str),
        "workspace_root": ("workspace_root", Path),
        "storage_root": ("storage_root", Path),
        "log_level": ("log_level", str),
        "policy.default_mode": ("policy.default_mode", PermissionMode),
        "policy.shell_timeout_seconds": ("policy.shell_timeout_seconds", int),
    }
    if parts[0] == "get":
        if len(parts) == 1:
            render.console.print(cfg.model_dump_json(indent=2, exclude={"providers": {"__all__": {"api_key"}}}))
            return
        key = parts[1]
        if key not in supported:
            render.print_error(f"unsupported config key: {key}")
            return
        target, _caster = supported[key]
        value = cfg
        for chunk in target.split("."):
            value = getattr(value, chunk)
        render.print_info(f"{key} = {value}")
        return
    if len(parts) < 3:
        render.print_error("usage: /config set <key> <value>")
        return
    key = parts[1]
    raw = " ".join(parts[2:])
    if key not in supported:
        render.print_error(f"unsupported config key: {key}")
        return
    target, caster = supported[key]
    try:
        value = caster(raw)
    except Exception as exc:  # noqa: BLE001
        render.print_error(f"invalid value for {key}: {exc}")
        return
    parent = cfg
    chunks = target.split(".")
    for chunk in chunks[:-1]:
        parent = getattr(parent, chunk)
    setattr(parent, chunks[-1], value)
    if key == "policy.default_mode":
        orchestrator.set_permission_mode(value)
    ConfigLoader(global_dir=_storage_root(orchestrator)).save_global(cfg)
    render.print_info(f"saved {key} = {value}")


def _handle_env(orchestrator: Orchestrator) -> None:
    import platform
    import sys

    table = Table(title="environment", box=BOX, show_header=False, border_style=PRIMARY)
    table.add_column("key", style="dim")
    table.add_column("value")
    table.add_row("os", f"{platform.system()} {platform.release()}")
    table.add_row("python", sys.executable)
    table.add_row("cwd", str(Path.cwd()))
    table.add_row("workspace", str(orchestrator.state.session.workspace if orchestrator.state else orchestrator.config.workspace_root))
    table.add_row("storage", str(_storage_root(orchestrator)))
    table.add_row("provider", orchestrator.provider.name)
    table.add_row("anthropic key", "set" if os.environ.get("ANTHROPIC_API_KEY") else "not set")
    table.add_row("openai key", "set" if os.environ.get("OPENAI_API_KEY") else "not set")
    table.add_row("editor", os.environ.get("EDITOR") or os.environ.get("VISUAL") or ("notepad" if os.name == "nt" else "not set"))
    render.console.print(table)


def _handle_cd(orchestrator: Orchestrator, arg: str) -> None:
    if not _require_state(orchestrator):
        return
    parts = _split_args(arg)
    if parts is None:
        return
    if len(parts) != 1:
        render.print_error("usage: /cd <path>")
        return
    path = Path(parts[0]).expanduser()
    if not path.is_absolute():
        path = (orchestrator.state.session.workspace / path).resolve()
    if not path.exists() or not path.is_dir():
        render.print_error(f"not a directory: {path}")
        return
    orchestrator.state.session.workspace = path.resolve()
    orchestrator.config.workspace_root = path.resolve()
    orchestrator.session_store.update(orchestrator.state.session)
    render.print_info(f"workspace -> {path.resolve()}")


def _handle_models(orchestrator: Orchestrator) -> None:
    table = Table(title="models", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY)
    table.add_column("provider", style="bold")
    table.add_column("model")
    table.add_column("source", style="dim")
    active_provider = orchestrator.state.session.provider if orchestrator.state else orchestrator.provider.name
    active_model = orchestrator.state.session.model if orchestrator.state else ""
    names = orchestrator.providers.names() if orchestrator.providers is not None else [orchestrator.provider.name]
    for name in names:
        provider = orchestrator.providers.get(name) if orchestrator.providers is not None else orchestrator.provider
        model = getattr(provider, "default_model", "") or orchestrator.config.providers.get(name, None).default_model if name in orchestrator.config.providers else getattr(provider, "default_model", "")
        marker = "* " if name == active_provider and (not active_model or model == active_model) else "  "
        table.add_row(f"{marker}{name}", model or "-", "local config/provider")
    render.console.print(table)


def _handle_plan(orchestrator: Orchestrator, arg: str) -> None:
    value = arg.strip().lower()
    if not value:
        render.print_info(f"plan: {'on' if _runtime_flag(orchestrator, 'plan_enabled') else 'off'}")
        return
    if value not in ("on", "off"):
        render.print_error("usage: /plan [on|off]")
        return
    _set_runtime_flag(orchestrator, "plan_enabled", value == "on")
    render.print_info(f"plan -> {value}")


def _handle_tools(orchestrator: Orchestrator, arg: str) -> None:
    parts = _split_args(arg)
    if parts is None:
        return
    if not parts:
        render.print_tools(orchestrator.tools.definitions())
        disabled = _disabled_tools(orchestrator)
        if disabled:
            render.print_info("disabled this session: " + ", ".join(sorted(disabled)))
        return
    if len(parts) != 2 or parts[0] not in ("enable", "disable"):
        render.print_error("usage: /tools [enable|disable <name>]")
        return
    action, name = parts
    if orchestrator.tools.get(name) is None:
        render.print_error(f"unknown tool: {name}")
        return
    disabled = _disabled_tools(orchestrator)
    if action == "disable":
        disabled.add(name)
    else:
        disabled.discard(name)
    render.print_info(f"tool {name} -> {'enabled' if action == 'enable' else 'disabled'}")


def _handle_approvals(orchestrator: Orchestrator) -> None:
    table = Table(title="approvals", box=BOX, show_header=False, border_style=PRIMARY)
    table.add_column("key", style="dim")
    table.add_column("value")
    table.add_row("mode", orchestrator.policy.mode.value)
    table.add_row("shell", "prompts in strict/balanced unless allowlisted; autonomous allows")
    table.add_row("outside workspace paths", "prompted when read/write crosses workspace")
    table.add_row("blocked commands", ", ".join(sorted(orchestrator.policy.blocked_commands)) or "(none)")
    render.console.print(table)


def _handle_cost(orchestrator: Orchestrator) -> None:
    if not _require_state(orchestrator):
        return
    state = orchestrator.state
    assert state is not None
    chars = sum(len(m.content or "") for m in state.messages)
    estimate = chars // 4 if chars else 0
    last = state.last_usage_prompt_tokens + state.last_usage_completion_tokens
    table = Table(title="cost", box=BOX, show_header=False, border_style=PRIMARY)
    table.add_column("metric", style="dim")
    table.add_column("value")
    table.add_row("provider", state.session.provider)
    table.add_row("model", state.session.model)
    table.add_row("estimated context tokens", str(estimate))
    table.add_row("last provider tokens", str(last))
    table.add_row("money estimate", "not available without provider pricing table")
    render.console.print(table)


def _handle_web(orchestrator: Orchestrator, arg: str) -> None:
    value = arg.strip().lower()
    web_tools = {d.name for d in orchestrator.tools.definitions() if "web" in d.name or "search" in d.name}
    if not value:
        disabled = _disabled_tools(orchestrator)
        enabled = bool(web_tools - disabled)
        render.print_info(f"web: {'on' if enabled else 'off'} ({', '.join(sorted(web_tools)) or 'no web tools'})")
        return
    if value not in ("on", "off"):
        render.print_error("usage: /web [on|off]")
        return
    disabled = _disabled_tools(orchestrator)
    for name in web_tools:
        if value == "off":
            disabled.add(name)
        else:
            disabled.discard(name)
    render.print_info(f"web -> {value}")


def _handle_save(orchestrator: Orchestrator, arg: str) -> None:
    parts = _split_args(arg)
    if parts is None:
        return
    if not parts:
        render.print_error("usage: /save <name> [text]")
        return
    name = parts[0]
    text = " ".join(parts[1:]).strip() or _last_user_message(orchestrator)
    if not text:
        render.print_error("no text supplied and no last user prompt")
        return
    snippets = _load_snippets(orchestrator)
    snippets[name] = _Snippet(name=name, text=text, updated_at=datetime.now(UTC).isoformat())
    _save_snippets(orchestrator, snippets)
    render.print_info(f"saved prompt '{name}'")


def _handle_load(orchestrator: Orchestrator, arg: str) -> None:
    name = arg.strip()
    if not name:
        snippets = _load_snippets(orchestrator)
        if not snippets:
            render.print_info("no saved prompts")
            return
        table = Table(title="prompts", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY)
        table.add_column("name", style="bold")
        table.add_column("preview")
        for snippet in snippets.values():
            table.add_row(snippet.name, snippet.text[:90])
        render.console.print(table)
        return
    snippet = _load_snippets(orchestrator).get(name)
    if snippet is None:
        render.print_error(f"no saved prompt: {name}")
        return
    render.console.print(Panel(Text(snippet.text), title=name, box=BOX, border_style=PRIMARY))


def _handle_prompt(orchestrator: Orchestrator, arg: str) -> str | None:
    parts = _split_args(arg)
    if parts is None:
        return None
    if not parts:
        render.print_error("usage: /prompt <name> [args...]")
        return None
    snippet = _load_snippets(orchestrator).get(parts[0])
    if snippet is None:
        render.print_error(f"no saved prompt: {parts[0]}")
        return None
    args = parts[1:]
    text = snippet.text.replace("{args}", " ".join(args))
    for i, value in enumerate(args, 1):
        text = text.replace("{" + str(i) + "}", value)
    return "prompt-run:" + text


def _handle_mcp(orchestrator: Orchestrator, arg: str) -> None:
    parts = _split_args(arg)
    if parts is None:
        return
    data = _read_json(_mcp_path(orchestrator), {"servers": []})
    servers = {s["name"]: s for s in data.get("servers", []) if isinstance(s, dict) and s.get("name")}
    if not parts or parts[0] == "list":
        if not servers:
            render.print_info("no MCP servers configured")
            return
        table = Table(title="mcp servers", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY)
        table.add_column("name", style="bold")
        table.add_column("command")
        table.add_column("args", style="dim")
        for server in servers.values():
            table.add_row(server["name"], server.get("command", ""), " ".join(server.get("args", [])))
        render.console.print(table)
        return
    if parts[0] == "connect":
        if len(parts) < 3:
            render.print_error("usage: /mcp connect <name> <command> [args...]")
            return
        servers[parts[1]] = {"name": parts[1], "command": parts[2], "args": parts[3:]}
        _write_json(_mcp_path(orchestrator), {"servers": list(servers.values())})
        render.print_info(f"configured MCP server '{parts[1]}'")
        return
    if parts[0] == "disconnect":
        if len(parts) != 2:
            render.print_error("usage: /mcp disconnect <name>")
            return
        removed = servers.pop(parts[1], None)
        _write_json(_mcp_path(orchestrator), {"servers": list(servers.values())})
        render.print_info(f"removed MCP server '{parts[1]}'" if removed else f"no MCP server named '{parts[1]}'")
        return
    render.print_error("usage: /mcp <list|connect|disconnect> ...")


def _handle_agents(orchestrator: Orchestrator) -> None:
    rows = orchestrator.subagents.visible_rows()
    if not rows:
        render.print_info("no active or recent subagents")
        return
    table = Table(title="agents", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY)
    table.add_column("id", style="dim")
    table.add_column("name", style="bold")
    table.add_column("status")
    table.add_column("last action")
    for row in rows:
        table.add_row(row.id, row.name, row.status, row.last_action)
    render.console.print(table)


def _handle_agent(arg: str) -> str | None:
    parts = _split_args(arg)
    if parts is None:
        return None
    if len(parts) < 2:
        render.print_error("usage: /agent <name> <prompt>")
        return None
    name = parts[0]
    task = " ".join(parts[1:])
    return (
        "prompt-run:Spawn a subagent named "
        + name
        + " to handle this task, using available tools as needed: "
        + task
    )


def _handle_theme(orchestrator: Orchestrator, arg: str) -> None:
    allowed = ("reid", "dark", "high-contrast")
    value = arg.strip().lower()
    path = _theme_path(orchestrator)
    if not value:
        data = _read_json(path, {"theme": "reid"})
        render.print_info(f"theme: {data.get('theme', 'reid')} (choices: {', '.join(allowed)})")
        return
    if value not in allowed:
        render.print_error(f"unknown theme: {value} (try {', '.join(allowed)})")
        return
    _write_json(path, {"theme": value})
    render.print_info(f"theme -> {value} (restart TUI to fully apply)")


def _handle_keys() -> None:
    table = Table(title="keys", box=BOX, show_header=True, header_style=f"bold {PRIMARY}", border_style=PRIMARY)
    table.add_column("key", style="bold")
    table.add_column("action")
    rows = [
        ("Enter", "submit prompt or accept completion"),
        ("Esc", "cancel current turn at next safe point"),
        ("Ctrl+C", "clear current input"),
        ("Ctrl+D", "exit"),
        ("Ctrl+O", "toggle collapsed/expanded tool and thinking blocks"),
        ("Left/Right", "cycle effort when input is empty"),
        ("PageUp/PageDown", "scroll output"),
    ]
    for key, action in rows:
        table.add_row(key, action)
    render.console.print(table)


def _handle_update() -> None:
    from reidcli import __version__

    render.print_info(f"installed reidcli {__version__}")
    render.print_info("update check is local-only; reinstall package or pull latest source to update")


def _handle_edit(orchestrator: Orchestrator) -> str | None:
    text = _last_user_message(orchestrator)
    if not text:
        render.print_error("no last user prompt to edit")
        return None
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "")
    if not editor:
        render.print_error("set EDITOR or VISUAL to use /edit")
        return None
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        tmp = fh.name
    try:
        subprocess.run([editor, tmp], check=False)
        edited = Path(tmp).read_text(encoding="utf-8").strip()
    finally:
        try:
            Path(tmp).unlink()
        except OSError:
            pass
    if not edited:
        render.print_error("edited prompt is empty")
        return None
    return "prompt-run:" + edited


def _handle_workflow(orchestrator: Orchestrator, arg: str) -> str | None:
    """Handles /workflow <run|save|show|delete> ...

    Returns "workflow-run:<name>" for /workflow run (the caller — ui.app's
    async turn loop — is the only thing that can actually execute a
    workflow's steps, since that requires awaiting each step's turn); returns
    None for every other subcommand (handled fully here).
    """
    raw_parts = _split_args(arg)
    if raw_parts is None:
        return None
    parts = [raw_parts[0], " ".join(raw_parts[1:])] if raw_parts else []
    if not parts:
        render.print_error("usage: /workflow <run|save|show|delete> <name> ...")
        return None
    sub, rest = parts[0], (parts[1] if len(parts) > 1 else "").strip()

    if sub == "run":
        if not rest:
            render.print_error("usage: /workflow run <name>")
        elif orchestrator.workflow_store.get(rest) is None:
            render.print_error(f"no such workflow: {rest}")
        else:
            return f"workflow-run:{rest}"
        return None

    if sub == "show":
        wf = orchestrator.workflow_store.get(rest) if rest else None
        if wf is None:
            render.print_error(f"no such workflow: {rest or '(missing name)'}")
        else:
            render.print_workflow_steps(wf)
        return None

    if sub == "save":
        save_parts = _split_args(rest)
        if save_parts is None:
            return None
        if not save_parts:
            render.print_error("usage: /workflow save <name> [n]  (n = last n user turns, default 5)")
            return None
        name = save_parts[0]
        count_str = save_parts[1].strip() if len(save_parts) > 1 else ""
        n = int(count_str) if count_str.isdigit() else 5
        if orchestrator.state is None or not orchestrator.state.messages:
            render.print_error("no turns to save yet")
            return None
        steps = [m.content for m in orchestrator.state.messages if m.role == "user"][-n:]
        if not steps:
            render.print_error("no user turns to save yet")
            return None
        orchestrator.workflow_store.save(Workflow(name=name, steps=steps, description=f"last {len(steps)} turn(s)"))
        render.print_info(f"saved workflow '{name}' ({len(steps)} steps)")
        return None

    if sub == "delete":
        if not rest:
            render.print_error("usage: /workflow delete <name>")
        elif orchestrator.workflow_store.delete(rest):
            render.print_info(f"deleted workflow '{rest}'")
        else:
            render.print_error(f"no such workflow: {rest}")
        return None

    render.print_error(f"unknown /workflow subcommand: {sub} (try run|save|show|delete)")
    return None


_BUILTIN_PROVIDER_NAMES = ("stub", "anthropic", "gemini")
_BUILTIN_PROVIDER_KINDS = {"anthropic": "anthropic", "gemini": "gemini"}


def _providers_store(orchestrator: Orchestrator) -> ProviderStore:
    root = orchestrator.config.storage_root or (Path.home() / ".reidcli")
    return ProviderStore(root)


def _handle_providers(orchestrator: Orchestrator) -> None:
    store = _providers_store(orchestrator)
    persisted = store.list()
    persisted_names = {r.name for r in persisted}
    active = orchestrator.state.session.provider if orchestrator.state else orchestrator.config.default_provider
    extra: list[str] = []
    if orchestrator.providers is not None:
        for name in orchestrator.providers.names():
            if name not in persisted_names:
                extra.append(name)
    render.print_providers(persisted, active, extra)


def _handle_connect(orchestrator: Orchestrator, arg: str) -> None:
    parts = _split_args(arg)
    if parts is None:
        return
    if len(parts) < 3:
        render.print_error(
            "usage: /connect <name> <kind> <base_url> [api_key] [model]  "
            f"(kind: {'|'.join(SUPPORTED_KINDS)})"
        )
        return
    name, kind, base_url = parts[0], parts[1], parts[2]
    if base_url in ("-", "default"):
        base_url = ""
    if kind not in SUPPORTED_KINDS:
        render.print_error(f"unknown kind: {kind} (try {'|'.join(SUPPORTED_KINDS)})")
        return
    if name in _BUILTIN_PROVIDER_NAMES and _BUILTIN_PROVIDER_KINDS.get(name) != kind:
        render.print_error(f"name '{name}' is reserved for the built-in provider")
        return
    api_key = parts[3] if len(parts) > 3 else ""
    model = parts[4] if len(parts) > 4 else ""
    record = ProviderRecord(name=name, kind=kind, base_url=base_url, api_key=api_key, default_model=model)
    try:
        provider = build_provider(record)
    except ValueError as exc:
        render.print_error(f"failed to build provider: {exc}")
        return
    _providers_store(orchestrator).save(record)
    if orchestrator.providers is not None:
        orchestrator.providers.register(name, provider)
    render.print_info(f"connected provider '{name}' ({kind}) → {base_url or '(default)'}")
    render.print_info(f"switch with: /use {name}")


def _handle_disconnect(orchestrator: Orchestrator, arg: str) -> None:
    name = arg.strip()
    if not name:
        render.print_error("usage: /disconnect <name>")
        return
    if name in _BUILTIN_PROVIDER_NAMES:
        render.print_error(f"cannot disconnect built-in provider '{name}'")
        return
    active = orchestrator.state.session.provider if orchestrator.state else ""
    if name == active:
        render.print_error(f"'{name}' is active; /use stub first, then disconnect")
        return
    removed = _providers_store(orchestrator).delete(name)
    if orchestrator.providers is not None:
        orchestrator.providers.unregister(name)
    if removed:
        render.print_info(f"disconnected '{name}'")
    else:
        render.print_error(f"no saved provider named '{name}'")


def _handle_use(orchestrator: Orchestrator, arg: str) -> None:
    name = arg.strip()
    if not name:
        render.print_error("usage: /use <name> (see /providers)")
        return
    if orchestrator.providers is None or not orchestrator.providers.has(name):
        render.print_error(f"provider '{name}' is not registered (see /providers)")
        return
    if orchestrator.state is None:
        render.print_error("no active session")
        return
    try:
        orchestrator.use_provider(name)
    except (KeyError, RuntimeError) as exc:
        render.print_error(str(exc))
        return
    render.print_info(f"active provider → {name}  (model: {orchestrator.state.session.model})")


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
        if arg.startswith("search "):
            _handle_sessions_search(orchestrator, arg[len("search ") :])
        else:
            render.print_sessions(orchestrator.session_store.list())
    elif cmd == "session":
        _handle_session(orchestrator, arg)
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
            valid = {t.status.value for t in tasks} | {"pending", "active", "completed", "failed", "blocked", "skipped"}
            if arg not in valid:
                render.print_error("unknown task status: " + arg)
                return "continue"
            tasks = [t for t in tasks if t.status.value == arg]
        render.print_tasks(tasks)
    elif cmd == "usage":
        _handle_usage(orchestrator)
    elif cmd == "config":
        _handle_config(orchestrator, arg)
    elif cmd == "env":
        _handle_env(orchestrator)
    elif cmd == "pwd":
        if orchestrator.state:
            render.print_info(str(orchestrator.state.session.workspace))
        else:
            render.print_info(str(orchestrator.config.workspace_root))
    elif cmd == "cd":
        _handle_cd(orchestrator, arg)
    elif cmd == "transcript":
        if orchestrator.state is None:
            render.print_info("no active session")
        else:
            if arg and not arg.isdigit():
                render.print_error("usage: /transcript [n]")
                return "continue"
            n = int(arg) if arg.isdigit() else 20
            if n <= 0:
                render.print_error("usage: /transcript [n]  (n must be positive)")
                return "continue"
            render.print_transcript(orchestrator.state.messages, n)
    elif cmd == "export":
        _handle_export(orchestrator, arg)
    elif cmd == "compact":
        _handle_compact(orchestrator, arg)
    elif cmd == "undo":
        _handle_undo(orchestrator)
    elif cmd == "retry":
        last = _last_user_message(orchestrator)
        if last:
            return "prompt-run:" + last
        render.print_error("no last user prompt to retry")
    elif cmd == "edit":
        outcome = _handle_edit(orchestrator)
        if outcome is not None:
            return outcome
    elif cmd == "fork":
        _handle_fork(orchestrator, arg)
    elif cmd == "model":
        if not arg or orchestrator.state is None:
            render.print_error("usage: /model <name> (with an active session)")
        else:
            orchestrator.state.session.model = arg
            orchestrator.session_store.update(orchestrator.state.session)
            render.print_info(f"model → {arg}")
            if orchestrator.state.session.provider == "stub" and arg != "stub-v0":
                render.print_info(
                    "model name changed, but provider is still stub; use /providers then /use <provider>"
                )
    elif cmd == "models":
        _handle_models(orchestrator)
    elif cmd == "effort":
        if orchestrator.state is None:
            render.print_error("usage: /effort <low|medium|high|xhigh> (with an active session)")
        elif not arg:
            render.print_info(f"current effort: {orchestrator.state.session.reasoning_effort}")
        elif arg not in _EFFORT_LEVELS:
            render.print_error(f"unknown effort: {arg} (try low|medium|high|xhigh)")
        else:
            orchestrator.state.session.reasoning_effort = arg
            orchestrator.session_store.update(orchestrator.state.session)
            render.print_info(f"effort → {arg}")
    elif cmd == "mode":
        if not arg:
            render.print_info(f"current mode: {orchestrator.policy.mode.value}")
        else:
            _set_mode(orchestrator, arg)
    elif cmd == "plan":
        _handle_plan(orchestrator, arg)
    elif cmd == "nyx":
        _handle_nyx(orchestrator, arg)
    elif cmd == "permissions":
        render.print_permissions(orchestrator.policy)
    elif cmd == "tools":
        _handle_tools(orchestrator, arg)
    elif cmd == "approvals":
        _handle_approvals(orchestrator)
    elif cmd == "cost":
        _handle_cost(orchestrator)
    elif cmd == "web":
        _handle_web(orchestrator, arg)
    elif cmd == "rewind":
        if orchestrator.state is None or not orchestrator.state.messages:
            render.print_info("nothing to rewind")
        else:
            orchestrator.rewind()
            render.print_info(f"rewound to {len(orchestrator.state.messages)} messages")
    elif cmd == "workflows":
        render.print_workflows(orchestrator.workflow_store.list())
    elif cmd == "workflow":
        outcome = _handle_workflow(orchestrator, arg)
        if outcome is not None:
            return outcome
    elif cmd == "save":
        _handle_save(orchestrator, arg)
    elif cmd == "load":
        _handle_load(orchestrator, arg)
    elif cmd == "prompt":
        outcome = _handle_prompt(orchestrator, arg)
        if outcome is not None:
            return outcome
    elif cmd == "providers":
        _handle_providers(orchestrator)
    elif cmd == "connect":
        _handle_connect(orchestrator, arg)
    elif cmd == "disconnect":
        _handle_disconnect(orchestrator, arg)
    elif cmd == "use":
        _handle_use(orchestrator, arg)
    elif cmd == "mcp":
        _handle_mcp(orchestrator, arg)
    elif cmd == "agents":
        _handle_agents(orchestrator)
    elif cmd == "agent":
        outcome = _handle_agent(arg)
        if outcome is not None:
            return outcome
    elif cmd == "deepreid":
        if not arg.strip():
            render.print_error("usage: /deepreid <task>")
        else:
            return "deepreid-run:" + arg.strip()
    elif cmd == "theme":
        _handle_theme(orchestrator, arg)
    elif cmd == "keys":
        _handle_keys()
    elif cmd == "update":
        _handle_update()
    elif cmd == "clear":
        render.console.clear()
    elif cmd in ("exit", "quit", "q"):
        return "exit"
    else:
        render.print_error(f"unknown command: /{cmd} (try /help)")
    return "continue"
