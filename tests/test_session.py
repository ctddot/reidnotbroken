"""Session + task store tests: round-trip, message persistence, resume."""
from __future__ import annotations

from pathlib import Path

from reidcli.config.models import default_config
from reidcli.provider.base import Message
from reidcli.provider.stub import StubProvider
from reidcli.runtime.orchestrator import Orchestrator
from reidcli.session.models import Session, SessionStatus
from reidcli.session.store import SessionStore
from reidcli.tasks.models import TaskStatus
from reidcli.tasks.store import TaskStore
from reidcli.tools import default_registry


def test_session_create_get_list(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    s = store.create(Session(title="test", workspace=tmp_path))
    assert store.get(s.id) is not None
    assert len(store.list()) == 1
    assert store.get(s.id).title == "test"


def test_session_status_update(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    s = store.create(Session(title="t", workspace=tmp_path))
    store.set_status(s.id, SessionStatus.ARCHIVED)
    assert store.get(s.id).status is SessionStatus.ARCHIVED


def test_message_persistence_and_resume(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    s = store.create(Session(title="t", workspace=tmp_path))
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi"),
    ]
    for m in msgs:
        store.append_message(s.id, m)
    restored = store.read_messages(s.id)
    assert len(restored) == 3
    assert restored[0].role == "system"
    assert restored[1].content == "hello"
    assert restored[2].role == "assistant"


def test_task_create_update_status(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    s = store.create(Session(title="t", workspace=tmp_path))
    ts = TaskStore(tmp_path, s.id)
    t = ts.create("do something")
    assert t.status is TaskStatus.PENDING
    ts.update_status(t.id, TaskStatus.ACTIVE)
    assert ts.get(t.id).status is TaskStatus.ACTIVE
    ts.update_status(t.id, TaskStatus.COMPLETED, summary="done")
    assert ts.get(t.id).status is TaskStatus.COMPLETED
    assert ts.get(t.id).summary == "done"


def test_orchestrator_task_count_cache_updates(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.workspace_root = tmp_path
    cfg.storage_root = tmp_path
    orch = Orchestrator(cfg, StubProvider(), default_registry())
    orch.start_session("t")
    assert orch.task_count() == 0
    orch.submit_task("hello")
    assert orch.task_count() == 1
