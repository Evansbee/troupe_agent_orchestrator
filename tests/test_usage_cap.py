"""REQ-BE-016: MVP usage cap — pause new autonomous claude runs at/above a per-window cap
(claude_cap_5h_percent/claude_cap_7d_percent, each falling back to claude_cap_percent when unset)."""
from troupe.engine import Engine, Wake


def _ratelimit(pct: float, reset_at: float, window="five_hour"):
    return {"at": 1_000_000.0, "unifiedWindows": {window: {"utilization": pct / 100, "resetsAt": reset_at}}}


def _two_windows(pct_5h: float, reset_5h: float, pct_7d: float, reset_7d: float):
    return {
        "at": 1_000_000.0,
        "unifiedWindows": {
            "five_hour": {"utilization": pct_5h / 100, "resetsAt": reset_5h},
            "seven_day": {"utilization": pct_7d / 100, "resetsAt": reset_7d},
        },
    }


def test_cap_off_by_default_zero_does_not_gate(project):
    cfg, store = project
    cfg.budget.claude_cap_percent = 0
    store.kv_set("claude_ratelimit", _ratelimit(95, 2_000_000.0))
    engine = Engine(cfg)

    engine.check_claude_cap()

    assert not engine.backend_limited("claude")


def test_cap_engages_when_utilization_at_or_above_threshold(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    store.kv_set("claude_ratelimit", _ratelimit(60, 1_003_600.0))
    engine = Engine(cfg)

    engine.check_claude_cap()

    assert engine.backend_limited("claude")
    assert engine.limit_reason("claude") == "cap"
    assert store.kv_get("limit.claude") == 1_003_600.0
    assert any(e["kind"] == "providers" and "cap engaged" in e["text"] for e in store.events(limit=50))


def test_cap_does_not_engage_below_threshold(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    store.kv_set("claude_ratelimit", _ratelimit(40, 1_003_600.0))
    engine = Engine(cfg)

    engine.check_claude_cap()

    assert not engine.backend_limited("claude")


def test_cap_checks_the_worse_of_5h_and_7d_windows(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    store.kv_set("claude_ratelimit", {
        "at": 1_000_000.0,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.30, "resetsAt": 1_003_600.0},
            "seven_day": {"utilization": 0.70, "resetsAt": 1_500_000.0},
        },
    })
    engine = Engine(cfg)

    engine.check_claude_cap()

    assert engine.backend_limited("claude")
    assert store.kv_get("limit.claude") == 1_500_000.0  # the 7d window's reset, since it's the one over


def test_5h_cap_trips_independently_with_7d_under_its_own_cap(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_5h_percent = 50
    cfg.budget.claude_cap_7d_percent = 90
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    store.kv_set("claude_ratelimit", _two_windows(60, 1_003_600.0, 30, 1_500_000.0))
    engine = Engine(cfg)

    engine.check_claude_cap()

    assert engine.backend_limited("claude")
    assert store.kv_get("limit.claude") == 1_003_600.0  # the 5h window's own reset


def test_7d_cap_trips_independently_with_5h_under_its_own_cap(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_5h_percent = 90
    cfg.budget.claude_cap_7d_percent = 50
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    store.kv_set("claude_ratelimit", _two_windows(30, 1_003_600.0, 60, 1_500_000.0))
    engine = Engine(cfg)

    engine.check_claude_cap()

    assert engine.backend_limited("claude")
    assert store.kv_get("limit.claude") == 1_500_000.0  # the 7d window's own reset


def test_unset_window_cap_falls_back_to_the_shared_claude_cap_percent(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50  # claude_cap_5h_percent/7d left unset
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    store.kv_set("claude_ratelimit", _ratelimit(60, 1_003_600.0))
    engine = Engine(cfg)

    engine.check_claude_cap()

    assert engine.backend_limited("claude")
    assert engine.limit_reason("claude") == "cap"


def test_a_window_cap_of_zero_is_off_even_when_the_shared_cap_is_set(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    cfg.budget.claude_cap_5h_percent = 0  # explicitly off for 5h, regardless of the fallback
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    store.kv_set("claude_ratelimit", _ratelimit(95, 1_003_600.0))  # way over the fallback, but 5h is off
    engine = Engine(cfg)

    engine.check_claude_cap()

    assert not engine.backend_limited("claude")


def test_cap_does_not_affect_other_backends(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    store.kv_set("claude_ratelimit", _ratelimit(90, 1_003_600.0))
    engine = Engine(cfg)

    engine.check_claude_cap()

    assert engine.backend_limited("claude")
    assert not engine.backend_limited("codex")
    assert not engine.backend_limited("local")


def test_cap_clears_at_the_window_reset_and_does_not_immediately_recap_stale_data(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("troupe.engine.now", lambda: clock["t"])
    store.kv_set("claude_ratelimit", _ratelimit(90, 1_003_600.0))
    engine = Engine(cfg)
    engine.check_claude_cap()
    assert engine.backend_limited("claude")

    clock["t"] = 1_003_700.0  # past the recorded reset; ratelimit data is unchanged (stale)
    engine.check_claude_cap()

    assert not engine.backend_limited("claude")  # cleared, and not immediately re-capped on stale data


def test_cap_recaps_once_fresh_data_still_shows_over_threshold(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("troupe.engine.now", lambda: clock["t"])
    store.kv_set("claude_ratelimit", _ratelimit(90, 1_003_600.0))
    engine = Engine(cfg)
    engine.check_claude_cap()
    clock["t"] = 1_003_700.0
    engine.check_claude_cap()
    assert not engine.backend_limited("claude")

    fresh = _ratelimit(80, 1_010_000.0)
    fresh["at"] = 1_003_800.0  # a real run reported fresh usage, still over the cap
    store.kv_set("claude_ratelimit", fresh)
    engine.check_claude_cap()

    assert engine.backend_limited("claude")
    assert store.kv_get("limit.claude") == 1_010_000.0


def test_a_real_provider_limit_is_not_overwritten_or_relabeled_as_a_cap(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    engine = Engine(cfg)
    engine.record_limit("claude", 1_050_000.0, reported=True)  # a real, provider-reported rate limit
    store.kv_set("claude_ratelimit", _ratelimit(90, 1_003_600.0))

    engine.check_claude_cap()

    assert engine.limit_reason("claude") == "provider"
    assert store.kv_get("limit.claude") == 1_050_000.0  # untouched


def test_candidates_still_wakes_chat_for_a_capped_claude_agent(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("troupe.engine.now", lambda: clock["t"])
    monkeypatch.setattr("troupe.store.now", lambda: clock["t"])  # so the chat message's own ts lines up
    store.kv_set("claude_ratelimit", _ratelimit(90, 1_003_600.0))
    engine = Engine(cfg)
    engine.check_claude_cap()
    a = cfg.agent("builder-1")  # default roster: claude-backed
    assert a.backend == "claude"
    store.send("human", a.id, "hello while capped", kind="chat")
    clock["t"] += 1  # past CHAT_DEBOUNCE

    wakes = engine.candidates(paused=False)

    chat_wakes = [w for w in wakes if w.agent.id == a.id and w.reason == "chat"]
    assert len(chat_wakes) == 1


def test_candidates_does_not_wake_capped_claude_agent_autonomously(project, monkeypatch):
    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    store.kv_set("claude_ratelimit", _ratelimit(90, 1_003_600.0))
    engine = Engine(cfg)
    engine.check_claude_cap()
    a = cfg.agent("builder-1")
    tid = store.add_task("Some work", status="ready", assignee=a.id)

    wakes = engine.candidates(paused=False)

    assert not any(w.agent.id == a.id for w in wakes)


def test_launch_allows_chat_but_not_autonomous_work_when_capped(project, monkeypatch):
    import asyncio
    from troupe.runners import RunResult

    class FakeRunner:
        cancelled = False
        proc = None

        async def run(self, spec, emit):
            emit("text", "hi")
            return RunResult(ok=True, final_text="hi")

    cfg, store = project
    cfg.budget.claude_cap_percent = 50
    monkeypatch.setattr("troupe.engine.now", lambda: 1_000_000.0)
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: FakeRunner())
    store.kv_set("claude_ratelimit", _ratelimit(90, 1_003_600.0))
    engine = Engine(cfg)
    engine.check_claude_cap()
    a = cfg.agent("builder-1")

    autonomous = Wake(0, a, "messages")
    task_wake = Wake(0, a, "task")
    chat_wake = Wake(0, a, "chat")

    async def scenario():
        await engine.launch(autonomous)
        assert a.id not in engine.running
        await engine.launch(task_wake)
        assert a.id not in engine.running
        await engine.launch(chat_wake)
        assert a.id in engine.running
        await engine.running[a.id][1]

    asyncio.run(scenario())
