"""Experimental native-scroll TUI entrypoint.

This stays separate from the default command while testing the next UI shape.
It deliberately avoids prompt_toolkit's full-screen alternate buffer so the
host terminal owns scrollback, which is the only way to get truly native scroll.
"""
from __future__ import annotations

from reidcli.runtime.orchestrator import Orchestrator
from reidcli.ui.app import _terminal_run


def run(orchestrator: Orchestrator, initial_prompt: str | None = None) -> int:
    return _terminal_run(orchestrator, initial_prompt=initial_prompt)
