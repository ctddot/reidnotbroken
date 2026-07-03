"""Agent loop tests: StubProvider turn, single assistant message, task outcome."""
from __future__ import annotations

from pathlib import Path

from reidcli.config.models import default_config
from reidcli.policy.engine import PolicyEngine
from reidcli.provider.base import BaseProvider, Message, ProviderResponse
from reidcli.provider.stub import StubProvider
from reidcli.runtime.agent import MAX_CONTEXT_MESSAGES, Agent
from reidcli.runtime.state import RuntimeState
from reidcli.session.models import Session
from reidcli.tools import default_registry


def _agent(tmp_path: Path) -> Agent:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    return Agent(StubProvider(), default_registry(), PolicyEngine(cfg))


def _state(tmp_path: Path) -> RuntimeState:
    return RuntimeState(session=Session(title="t", workspace=tmp_path))


def test_stub_turn_executes_tool_call(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    state = _state(tmp_path)
    text, tools = agent.run_turn(state, "list the current dir")
    assert len(tools) == 1
    assert tools[0]["name"] == "list_dir"
    assert tools[0]["ok"]
    assert "Tool returned" in text


def test_single_assistant_message_per_turn(tmp_path: Path) -> None:
    """Audit §3.1: no double-append of assistant messages."""
    agent = _agent(tmp_path)
    state = _state(tmp_path)
    agent.run_turn(state, "list the current dir")
    assistant_msgs = [m for m in state.messages if m.role == "assistant"]
    # Exactly two: one with tool_calls, one final text (not a duplicate).
    assert len(assistant_msgs) == 2
    assert len(assistant_msgs[0].tool_calls) == 1
    assert len(assistant_msgs[1].tool_calls) == 0


def test_plain_answer_no_tool_call(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    state = _state(tmp_path)
    text, tools = agent.run_turn(state, "hello there")
    assert len(tools) == 0
    assert "Acknowledged" in text


def test_system_prompt_inserted_once(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    state = _state(tmp_path)
    agent.run_turn(state, "hello")
    agent.run_turn(state, "again")
    system_msgs = [m for m in state.messages if m.role == "system"]
    assert len(system_msgs) == 1


class _CapturingProvider(BaseProvider):
    name = "capture"

    def __init__(self) -> None:
        self.seen_messages: list[Message] = []

    def chat(self, messages, tools=None, model=None):  # type: ignore[no-untyped-def]
        self.seen_messages = list(messages)
        return ProviderResponse(text="done")


def test_provider_context_is_bounded(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    provider = _CapturingProvider()
    agent = Agent(provider, default_registry(), PolicyEngine(cfg))
    state = _state(tmp_path)
    state.messages = [Message(role="system", content="old system")]
    state.messages.extend(Message(role="user", content=f"old {i}") for i in range(120))

    agent.run_turn(state, "new")

    assert len(provider.seen_messages) == MAX_CONTEXT_MESSAGES
    assert provider.seen_messages[0].role == "system"
    assert provider.seen_messages[-1].content == "new"
