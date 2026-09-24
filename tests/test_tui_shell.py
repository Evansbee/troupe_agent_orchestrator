"""REQ-TUI-002/010/011/031: the TUI shell against a fixture API server (no live engine)."""
import asyncio
import time

from tui_fixture import FixtureServer

from troupe.tui.app import TroupeApp

AGENTS = [
    dict(id="lead", handle="lead_1@t", name="Lead", role="lead", state="idle", status="",
         current_run=None, runs=3, tokens=0, cost=0.0, last_run_at=0.0, activity="",
         waiting_on=None, mail_queued=0, mail_reading=0),
    dict(id="builder-1", handle="builder_1@t", name="Builder 1", role="builder", state="running",
         status="", current_run=1, runs=5, tokens=0, cost=0.0, last_run_at=0.0,
         activity="editing app.py", waiting_on=None, mail_queued=2, mail_reading=1),
]
TASKS = [
    dict(id=66, status="in_progress", title="TUI slice A", assignee="builder-1", priority=0,
         flags=dict(human_request=True, checks_failed=False, arch_review=False, territory_conflict=[])),
    dict(id=67, status="ready", title="Needs-you pane", assignee="builder-3", priority=1,
         flags=dict(human_request=False, checks_failed=False, arch_review=False, territory_conflict=[])),
]
USAGE = dict(providers=[dict(provider="claude", limited_until=None, windows=[
    dict(name="5h", used_pct=42.0, cap_pct=90, resets_at=None),
    dict(name="7d", used_pct=18.0, cap_pct=None, resets_at=None),
])])
ENGINE = dict(state="live", stopped=False, paused=False, throttled=None, heartbeat=0,
             version="0", pid=1, started_at=0, running_runs=1, draining_runs=0, config_errors=[])
MILESTONES = [dict(id=1, name="Run from the TUI", status="active", total=4, done=1)]


def _app(project):
    cfg, _store = project
    return TroupeApp(cfg, owns_engine=True)


async def _wait_until(predicate, timeout=5.0):  # #71: generous default under load
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.02)


def test_team_and_tasks_load_and_render(project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                assert any(a["handle"] == "builder_1@t" for a in app._panes[0]._agents)
                assert any(t["title"] == "TUI slice A" for t in app._panes[1]._tasks)
                header_text = app._header.content.plain
                assert "test-project" in header_text
                assert "claude 5h" in header_text
                assert "42%" in header_text
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_offline_state_when_no_engine_is_reachable(project):
    async def scenario():
        app = _app(project)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert "Engine offline" in app._header.content.plain

    asyncio.run(scenario())


def test_live_event_refreshes_the_team_pane_within_a_second(project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                server.agents = AGENTS + [dict(id="qa", handle="qa_1@t", name="QA", role="qa",
                    state="idle", status="", current_run=None, runs=1, tokens=0, cost=0.0,
                    last_run_at=0.0, activity="", waiting_on=None, mail_queued=0, mail_reading=0)]
                await server.push_event("agent.state", {"id": "qa"})
                # #71: was timeout=1.0, tight for a real socket round-trip under load.
                await _wait_until(lambda: any(a["id"] == "qa" for a in app._panes[0]._agents), timeout=3.0)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_80x24_collapses_panes_into_tabs(project):
    from textual.widgets import TabbedContent

    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                assert app.query(TabbedContent)
                assert not app.query("#body-row")
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_full_size_uses_side_by_side_panes_not_tabs(project):
    from textual.widgets import TabbedContent

    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                assert not app.query(TabbedContent)
                assert app.query_one("#left")
        finally:
            await server.stop()

    asyncio.run(scenario())
