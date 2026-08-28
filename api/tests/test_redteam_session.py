"""RedTeamObserver tests — the harness itself is monkeypatched out (no real
LLM/network/Postgres calls), exercising only the observer's session
lifecycle and pub/sub broadcast.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from api.redteam_session import RedTeamObserver

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class _FakeResult:
    branch_id: str
    session_id: str
    steps_taken: int
    committed: bool


def _fake_run_session_module(monkeypatch: pytest.MonkeyPatch, steps: list[dict[str, object]], committed: bool) -> None:
    """Patch agents.redteam.harness.run_session/run_session_via_graph as seen
    from api.redteam_session's lazy import site.
    """
    import types

    def fake_run_session(sim_config, redteam_config, warmup_checkpoint_id=None,  # noqa: ANN001
                          from_genesis=False, session_id=None, router=None,
                          identity_file=None, on_step: Callable[[dict[str, object]], None] | None = None):
        for entry in steps:
            if on_step is not None:
                on_step(entry)
        return _FakeResult(
            branch_id=f"red-team/{session_id}", session_id=session_id,
            steps_taken=len(steps), committed=committed,
        )

    fake_config_module = types.SimpleNamespace(RedTeamConfig=lambda **kw: kw)
    fake_harness_module = types.SimpleNamespace(
        run_session=fake_run_session, run_session_via_graph=fake_run_session,
    )
    monkeypatch.setitem(__import__("sys").modules, "agents.redteam.config", fake_config_module)
    monkeypatch.setitem(__import__("sys").modules, "agents.redteam.harness", fake_harness_module)


@pytest.mark.asyncio
async def test_session_completes_and_broadcasts_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    steps = [
        {"step": 1, "tool_name": "create_account", "success": True, "error_code": None,
         "parameters": {}, "reasoning": "", "provider_model": "groq/x", "latency_ms": 12.3},
        {"step": 2, "tool_name": "commit_strategy", "success": True, "error_code": None,
         "parameters": {}, "reasoning": "", "provider_model": "groq/x", "latency_ms": 8.1},
    ]
    _fake_run_session_module(monkeypatch, steps, committed=True)

    observer = RedTeamObserver()
    q = None

    session_id = await observer.start_session(from_genesis=True, checkpoint_id=None, seed=1, use_graph=False)
    q = observer.subscribe(session_id)

    # Steps run in a background thread; wait for both step messages + done.
    messages = []
    for _ in range(3):
        messages.append(await q.get())

    assert [m["type"] for m in messages] == ["step", "step", "done"]
    assert messages[0]["tool_name"] == "create_account"
    assert messages[1]["tool_name"] == "commit_strategy"
    assert messages[2]["status"] == "done"
    assert messages[2]["committed"] is True

    state = observer.get_session(session_id)
    assert state.status == "done"
    assert state.steps_taken == 2
    assert state.committed is True
    assert len(state.step_log) == 2


@pytest.mark.asyncio
async def test_session_reports_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    def failing_run_session(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("boom")

    fake_config_module = types.SimpleNamespace(RedTeamConfig=lambda **kw: kw)
    fake_harness_module = types.SimpleNamespace(
        run_session=failing_run_session, run_session_via_graph=failing_run_session,
    )
    monkeypatch.setitem(__import__("sys").modules, "agents.redteam.config", fake_config_module)
    monkeypatch.setitem(__import__("sys").modules, "agents.redteam.harness", fake_harness_module)

    observer = RedTeamObserver()
    session_id = await observer.start_session(from_genesis=True, checkpoint_id=None, seed=1, use_graph=False)
    q = observer.subscribe(session_id)

    message = await q.get()
    assert message["type"] == "done"
    assert message["status"] == "error"
    assert "boom" in message["error"]

    state = observer.get_session(session_id)
    assert state.status == "error"
    assert state.error is not None and "boom" in state.error


@pytest.mark.asyncio
async def test_list_sessions_evicts_oldest_past_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run_session_module(monkeypatch, [], committed=False)
    observer = RedTeamObserver()

    import api.redteam_session as mod
    monkeypatch.setattr(mod, "_MAX_SESSIONS", 2)

    ids = []
    for _ in range(3):
        sid = await observer.start_session(from_genesis=True, checkpoint_id=None, seed=1, use_graph=False)
        q = observer.subscribe(sid)
        await q.get()  # drain "done" so the background task finishes deterministically
        ids.append(sid)

    listed = [s.session_id for s in observer.list_sessions()]
    assert ids[0] not in listed
    assert set(listed) == {ids[1], ids[2]}
