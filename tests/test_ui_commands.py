from __future__ import annotations

from pathlib import Path

from reidcli.config.models import default_config
from reidcli.provider.base import Message
from reidcli.provider.stub import StubProvider
from reidcli.runtime.orchestrator import Orchestrator
from reidcli.tools import default_registry
from reidcli.ui.app import _terminal_error_message
from reidcli.ui.commands import handle


def _orch(tmp_path: Path) -> Orchestrator:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    cfg.storage_root = tmp_path
    orch = Orchestrator(cfg, StubProvider(), default_registry())
    orch.start_session("test")
    return orch


def test_save_prompt_and_run_template(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    assert handle(orch, "/save greet hello {1} {args}") == "continue"
    assert (tmp_path / "prompts.json").exists()
    assert handle(orch, "/prompt greet Reid CLI") == "prompt-run:hello Reid Reid CLI"


def test_retry_returns_last_user_prompt(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    orch.state.messages.append(Message(role="user", content="try again"))
    assert handle(orch, "/retry") == "prompt-run:try again"


def test_terminal_error_message_guides_bad_api_key(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    msg = _terminal_error_message(orch, RuntimeError('HTTP 401: {"error":"Invalid API key."}'))
    assert "rejected the API key" in msg
    assert "/config set openai_api_key" in msg
    assert "/use stub" in msg


def test_undo_keeps_last_user_prompt(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    orch.state.messages.extend(
        [
            Message(role="user", content="question"),
            Message(role="assistant", content="answer"),
            Message(role="tool", content="tool result"),
        ]
    )
    assert handle(orch, "/undo") == "continue"
    assert [m.role for m in orch.state.messages] == ["user"]
    assert orch.state.messages[0].content == "question"


def test_compact_reduces_old_messages(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    for i in range(20):
        orch.state.messages.append(Message(role="user", content=f"msg {i}"))
    assert handle(orch, "/compact 4") == "continue"
    assert len(orch.state.messages) == 5
    assert orch.state.messages[0].role == "system"
    assert "Compact session summary" in orch.state.messages[0].content


def test_export_writes_markdown(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    orch.state.messages.append(Message(role="user", content="hello"))
    assert handle(orch, "/export md") == "continue"
    assert (tmp_path / "exports" / f"{orch.state.session.id}.md").exists()


def test_session_rename_and_delete_guard(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    old_id = orch.state.session.id
    assert handle(orch, "/session rename better") == "continue"
    assert orch.session_store.get(old_id).title == "better"

    other = orch.session_store.create(orch.state.session.model_copy(update={"id": "delete-me"}))
    assert handle(orch, f"/session delete {other.id}") == "continue"
    assert orch.session_store.get(other.id) is not None
    assert handle(orch, f"/session delete {other.id} --yes") == "continue"
    assert orch.session_store.get(other.id) is None


def test_cd_changes_workspace(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    child = tmp_path / "child"
    child.mkdir()
    assert handle(orch, f"/cd {child}") == "continue"
    assert orch.state.session.workspace == child.resolve()


def test_tools_disable_sets_runtime_filter(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    assert handle(orch, "/tools disable read_file") == "continue"
    assert "read_file" in orch.disabled_tools
    assert "read_file" in orch.agent.context_extras["disabled_tools"]
    assert handle(orch, "/tools enable read_file") == "continue"
    assert "read_file" not in orch.disabled_tools


def test_config_get_and_set_supported_key(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    assert handle(orch, "/config get policy.default_mode") == "continue"
    assert handle(orch, "/config set policy.shell_timeout_seconds 9") == "continue"
    assert orch.config.policy.shell_timeout_seconds == 9


def test_mcp_connect_and_disconnect(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    assert handle(orch, "/mcp connect local python server.py") == "continue"
    assert (tmp_path / "mcp_servers.json").exists()
    assert handle(orch, "/mcp disconnect local") == "continue"
