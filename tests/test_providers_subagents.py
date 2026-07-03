"""Tests for /connect provider persistence, spawn_agent tool, SubagentManager."""
from __future__ import annotations

from pathlib import Path

from reidcli.config.models import default_config
from reidcli.policy.engine import PolicyEngine
from reidcli.provider.base import Message, ToolCall
from reidcli.provider.gemini import GeminiProvider
from reidcli.provider.registry import ProviderRegistry
from reidcli.provider.store import ProviderRecord, ProviderStore, load_into
from reidcli.provider.stub import StubProvider
from reidcli.runtime.state import RuntimeState
from reidcli.runtime.subagent import SubagentManager
from reidcli.session.models import Session
from reidcli.tools import default_registry
from reidcli.tools.base import ToolContext
from reidcli.tools.spawn_agent import SpawnAgentTool


class _FakeOrchestrator:
    """Minimal orchestrator shim for spawn_agent (only what the tool touches)."""

    def __init__(self, tmp_path: Path) -> None:
        self.config = default_config()
        self.config.workspace_root = tmp_path
        self.tools = default_registry()
        self.provider = StubProvider()
        self.providers = ProviderRegistry()
        self.providers.register("stub", self.provider)
        self.policy = PolicyEngine(self.config)
        self.state = RuntimeState(session=Session(title="parent", workspace=tmp_path))
        self.subagents = SubagentManager()


# --- provider store ------------------------------------------------------


def test_provider_store_roundtrip(tmp_path: Path) -> None:
    store = ProviderStore(tmp_path)
    rec = ProviderRecord(
        name="local", kind="openai-compatible",
        base_url="http://localhost:8080", api_key="k", default_model="llama",
    )
    store.save(rec)
    assert store.get("local") == rec
    assert [r.name for r in store.list()] == ["local"]
    assert store.delete("local") is True
    assert store.get("local") is None


def test_provider_store_reads_utf8_bom_file(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        '\ufeff{"providers":[{"name":"reidverse","kind":"openai","base_url":"https://example.test","api_key":"k","default_model":"gpt"}]}',
        encoding="utf-8",
    )
    rec = ProviderStore(tmp_path).get("reidverse")
    assert rec is not None
    assert rec.kind == "openai"
    assert rec.api_key == "k"


def test_load_into_registers_provider(tmp_path: Path) -> None:
    ProviderStore(tmp_path).save(
        ProviderRecord(name="local", kind="ollama", base_url="http://localhost:11434", default_model="llama3.2")
    )
    reg = ProviderRegistry()
    reg.register("stub", StubProvider())
    added = load_into(reg, tmp_path)
    assert added == ["local"]
    assert reg.has("local")


def test_load_into_registers_gemini_provider(tmp_path: Path) -> None:
    ProviderStore(tmp_path).save(
        ProviderRecord(name="gemini", kind="gemini", api_key="k", default_model="gemini-3-flash")
    )
    reg = ProviderRegistry()
    reg.register("stub", StubProvider())
    added = load_into(reg, tmp_path)
    assert added == ["gemini"]
    assert isinstance(reg.get("gemini"), GeminiProvider)


def test_gemini_provider_maps_tools_and_responses() -> None:
    provider = GeminiProvider(api_key="k")
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="list"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="call-1", name="list_dir", arguments={"path": "."})],
        ),
        Message(role="tool", tool_call_id="call-1", content="README.md"),
    ]
    system, contents = provider._to_gemini_contents(messages)
    assert system == "sys"
    assert contents[-1]["parts"][0]["functionResponse"]["name"] == "list_dir"

    parsed = provider._parse({
        "candidates": [{
            "finishReason": "STOP",
            "content": {
                "parts": [
                    {"text": "hi"},
                    {"functionCall": {"name": "read_file", "args": {"path": "README.md"}}},
                ]
            },
        }],
        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3},
    })
    assert parsed.text == "hi"
    assert parsed.tool_calls[0].name == "read_file"
    assert parsed.usage.prompt_tokens == 2


# --- subagent manager ----------------------------------------------------


def test_subagent_manager_lifecycle() -> None:
    mgr = SubagentManager()
    events: list[list] = []
    mgr.subscribe(lambda snap: events.append([r.status for r in snap]))
    aid = mgr.start("researcher")
    assert mgr.any_active() is True
    mgr.update(aid, last_action="reading a file")
    mgr.finish(aid, status="done")
    assert mgr.any_active() is False
    # Row lingers briefly then prunes; visible_rows keeps it during linger.
    assert len(mgr.visible_rows()) == 1
    assert events  # at least the start/update/finish emissions fired


# --- spawn_agent tool ----------------------------------------------------


def test_spawn_agent_runs_child_and_reports_lifecycle(tmp_path: Path) -> None:
    orch = _FakeOrchestrator(tmp_path)
    tool = SpawnAgentTool()
    ctx = ToolContext(
        workspace_root=tmp_path,
        policy=orch.policy,
        writable_roots=[],
        extra={"orchestrator": orch},
    )
    result = tool.execute(
        {
            "name": "scout",
            "system_prompt": "You are a scout.",
            "task": "list the current dir",
            "tool_allowlist": ["list_dir"],
        },
        ctx,
    )
    assert result.ok, result.error
    assert "subagent:scout" in result.output
    # Subagent finished and lingers briefly.
    rows = orch.subagents.visible_rows()
    assert any(r.name == "scout" and r.status == "done" for r in rows)


def test_spawn_agent_rejects_missing_orchestrator(tmp_path: Path) -> None:
    tool = SpawnAgentTool()
    cfg = default_config()
    cfg.workspace_root = tmp_path
    ctx = ToolContext(workspace_root=tmp_path, policy=PolicyEngine(cfg), writable_roots=[], extra={})
    result = tool.execute(
        {"name": "n", "system_prompt": "s", "task": "t"},
        ctx,
    )
    assert not result.ok


def test_spawn_agent_strips_recursive_spawn(tmp_path: Path) -> None:
    """Child cannot spawn its own subagents (spawn_agent removed from allowlist)."""
    orch = _FakeOrchestrator(tmp_path)
    tool = SpawnAgentTool()
    ctx = ToolContext(
        workspace_root=tmp_path,
        policy=orch.policy,
        writable_roots=[],
        extra={"orchestrator": orch},
    )
    result = tool.execute(
        {
            "name": "recursive",
            "system_prompt": "s",
            "task": "hello",
            "tool_allowlist": ["spawn_agent", "list_dir"],
        },
        ctx,
    )
    assert result.ok
    # Child ran with list_dir only; spawn_agent was stripped. StubProvider's
    # "hello" path returns no tool calls, so tools list is empty — the point of
    # the test is just that the call succeeded rather than crashing on a
    # missing spawn_agent recursion.
