"""Provider quota signals, persistent dispatch gates, and queued chat."""
import asyncio
import time
from pathlib import Path

import pytest

from troupe.engine import Engine, Wake
from troupe.gui.data import Data
from troupe.runners import ClaudeRunner, CodexRunner, RunResult, RunSpec, limit_reset


@pytest.mark.parametrize("runner_cls,event,limited", [
    (ClaudeRunner, {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 2000}}, True),
    (ClaudeRunner, {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}, False),
    (ClaudeRunner, {"type": "result", "is_error": True, "result": "Usage limit reached"}, True),
    (CodexRunner, {"type": "error", "message": "Rate limit reached", "reset_at": 2000}, True),
    (CodexRunner, {"type": "turn.failed", "error": {"message": "You've hit your usage limit", "reset_at": 2000}}, True),
    (CodexRunner, {"type": "item.completed", "item": {"type": "error", "message": "usage_limit_reached"}}, True),
    (CodexRunner, {"type": "error", "message": "network unavailable"}, False),
])
def test_provider_events(project, monkeypatch, runner_cls, event, limited):
    cfg, _ = project
    runner = runner_cls()
    calls, signals = [], []
    async def stream(args, spec, prompt, callback):
        calls.append(args)
        callback(event)
        return 1, ""
    monkeypatch.setattr(runner, "_stream", stream)
    monkeypatch.setattr("troupe.runners.time.time", lambda: 1000)
    spec = RunSpec(cfg, cfg.agents[0], "system", "prompt", cfg.root, "resume-me", Path("unused"))
    result = asyncio.run(runner.run(spec, lambda kind, text: signals.append((kind, text))))
    assert bool(result.extra.get("limit_until")) == limited
    assert any(kind == "backend_limit" for kind, _ in signals) == limited
    assert len(calls) == (1 if limited else 2)
    if limited:
        assert result.extra["limit_until"] in (1900, 2000)


def test_reset_formats():
    assert limit_reset({"message": "Usage limit; try again in 3 minutes"}, 1000) == 1180
    assert limit_reset({"message": "resets at 2026-09-23T22:00:00Z"}, 1000) == 1790200800
    assert limit_reset({"reset_at": "2026-09-23T22:00:00Z"}, 1000) == 1790200800
    assert limit_reset({"resetsAt": 1790200800000}, 1000) == 1790200800
    assert limit_reset({"error": {"retry_after": 120}}, 1000) == 1120
    assert limit_reset({"reset_at": "invalid"}, 1000) == 1900
    assert limit_reset({"reset_at": "NaN"}, 1000) == 1900


def test_backend_gate_survives_restart_and_expires(project, monkeypatch):
    cfg, store = project
    a, other = cfg.agents[:2]
    a.backend, other.backend = "claude", "codex"
    clock = [1000]
    monkeypatch.setattr("troupe.engine.now", lambda: clock[0])
    store.kv_set("limit.claude", 1100)
    for agent in (a, other):
        store.send("human", agent.id, "hello", kind="chat")
    store.x("UPDATE messages SET ts=990")
    engine = Engine(cfg)
    assert a.id not in [w.agent.id for w in engine.candidates(True)]
    assert other.id in [w.agent.id for w in engine.candidates(True)]
    asyncio.run(engine.launch(Wake(0, a, "chat")))
    assert store.runs() == []
    assert store.unread(a.id)
    launched = []
    async def launch(wake):
        launched.append(wake.agent.id)
    monkeypatch.setattr(engine, "launch", launch)
    store.kv_set("paused", True)
    asyncio.run(engine.tick())
    assert launched == [other.id]
    engine = Engine(cfg)
    assert engine.backend_limited("claude")
    clock[0] = 1100
    assert a.id in [w.agent.id for w in engine.candidates(True)]


@pytest.mark.parametrize("reason", ["task", "chat"])
def test_limited_run_requeues_without_failure_or_attempt(project, monkeypatch, reason):
    cfg, store = project
    cfg.git_autocommit = False
    a = cfg.agent("builder-1")
    a.backend = "codex"
    task_id = store.add_task("Quota test", status="in_progress", assignee=a.id)
    store.send("human" if reason == "chat" else "lead", a.id, "please work", kind=reason)
    class LimitedRunner:
        cancelled = False
        async def run(self, spec, emit):
            emit("backend_limit", '{"until": 2000000000}')
            assert store.kv_get("limit.codex") == 2000000000
            return RunResult(ok=False, error="usage limit reached")
    monkeypatch.setattr("troupe.engine.make_runner", lambda _: LimitedRunner())
    engine = Engine(cfg)
    engine.failures[a.id] = (2, 0)
    async def run():
        await engine.launch(Wake(0, a, reason, store.task(task_id)))
        await engine.running[a.id][1]
    asyncio.run(run())
    assert engine.failures[a.id] == (2, 0)
    assert store.unread(a.id)
    assert store.task(task_id)["attempts"] == 0
    assert store.runs()[0]["status"] == "limited"
    assert not store.chat_thread(a.id) or store.chat_thread(a.id)[-1]["sender"] == "human"


def test_gui_snapshot_label_expires(project, monkeypatch):
    cfg, store = project
    store.kv_set("limit.claude", time.time() + 600)
    data = Data(cfg)
    data.refresh(force=True)
    assert data.limit_label("claude").startswith("Claude limited until ")
    assert not data.limit_label("codex")
    monkeypatch.setattr("troupe.gui.data.time.time", lambda: 2000000000)
    assert not data.limit_label("claude")
