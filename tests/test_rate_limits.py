"""Provider quota signals, persistent dispatch gates, and queued chat."""
import asyncio
import time
from pathlib import Path
from datetime import datetime

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
    assert limit_reset({"message": "try again at Sep 24th, 2026 10:00 AM."}, 1000) == datetime(2026, 9, 24, 10).timestamp()
    assert limit_reset({"message": "resets 11pm (UTC)"}, 1790200800) == 1790204400
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


@pytest.mark.parametrize("resets,expected", [
    ([None, 1120], 1120),
    ([1120, None], 1120),
    ([None, 1900, 1120], 1900),  # explicit reset equal to fallback is still authoritative
    ([1120, 1200, None, 1150], 1200),
])
def test_event_order_persists_authoritative_expiry(project, monkeypatch, resets, expected):
    cfg, store = project
    a = cfg.agent("builder-2")
    a.backend = "codex"
    clock = [1000]
    monkeypatch.setattr("troupe.engine.now", lambda: clock[0])
    monkeypatch.setattr("troupe.runners.time.time", lambda: clock[0])
    runner = CodexRunner()
    async def stream(args, spec, prompt, callback):
        for reset in resets:
            error = {"message": "Usage limit reached"}
            if reset is not None:
                error["reset_at"] = reset
            callback({"type": "error", **error} if reset is None else
                     {"type": "turn.failed", "error": error})
        # The persisted fallback must already be replaced before the run completes.
        assert store.kv_get("limit.codex") == expected
        return 1, "Usage limit reached"
    monkeypatch.setattr(runner, "_stream", stream)
    monkeypatch.setattr("troupe.engine.make_runner", lambda _: runner)
    engine = Engine(cfg)
    store.send("human", a.id, "hello", kind="chat")
    store.x("UPDATE messages SET ts=990")
    async def run():
        await engine.launch(Wake(0, a, "chat"))
        await engine.running[a.id][1]
    asyncio.run(run())
    assert store.kv_get("limit.codex") == expected
    assert store.kv_get("limit_meta.codex") == {"until": expected, "reported": True, "reason": "provider"}
    restarted = Engine(cfg)
    assert restarted.backend_limited("codex")
    clock[0] = expected
    assert not restarted.backend_limited("codex")
    assert a.id in [w.agent.id for w in restarted.candidates(True)]


def test_persisted_provenance_across_restart_and_independent_reports(project, monkeypatch):
    cfg, store = project
    clock = [1000]
    monkeypatch.setattr("troupe.engine.now", lambda: clock[0])
    Engine(cfg).record_limit("codex", 1900, reported=False)
    Engine(cfg).record_limit("codex", 1120, reported=True)
    assert store.kv_get("limit.codex") == 1120
    Engine(cfg).record_limit("codex", 1200, reported=True)
    Engine(cfg).record_limit("codex", 1900, reported=False)
    Engine(cfg).record_limit("codex", 1150, reported=True)
    assert store.kv_get("limit.codex") == 1200
    clock[0] = 1200
    Engine(cfg).record_limit("codex", 2100, reported=False)
    assert store.kv_get("limit.codex") == 2100
    assert store.kv_get("limit_meta.codex")["reported"] is False
