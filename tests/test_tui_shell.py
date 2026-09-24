"""REQ-TUI-002/010/011/031: the TUI shell against a fixture API server (no live engine)."""
import asyncio
import time

import pytest

from tui_fixture import FixtureServer

from troupe.tui.app import TroupeApp

AGENTS = [
    dict(id="lead", handle="lead_1@t", name="Lead", role="lead", state="idle", status="",
         current_run=None, runs=3, tokens=0, cost=0.0, last_run_at=0.0, activity="",
         waiting_on=None, mail_queued=0, mail_reading=0),
    dict(id="builder-1", handle="builder_1@t", name="Builder 1", role="builder", state="running",
         status="", current_run=1, runs=5, tokens=0, cost=0.0, last_run_at=0.0,
         activity="editing app.py", waiting_on=None, mail_queued=2, mail_reading=1),
    dict(id="pm", handle="pm_1@t", name="PM", role="pm", state="idle", status="",
         current_run=None, runs=2, tokens=0, cost=0.0, last_run_at=0.0, activity="",
         waiting_on=None, mail_queued=0, mail_reading=0),
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


def test_chatpane_is_mounted_alongside_needs_you_in_the_right_column(project):
    """#77: ChatPane (#68) mounted into RIGHT_PANES, after Needs-you and Comms (#69) per
    design/tui.md."""
    from troupe.tui.panes.chat import ChatPane
    from troupe.tui.panes.feed import FeedPane
    from troupe.tui.panes.needs_you import NeedsYouPane

    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                right = app.query_one("#right")
                mounted = list(right.children)
                assert [type(w) for w in mounted] == [NeedsYouPane, FeedPane, ChatPane]
                await _wait_until(lambda: app.query_one(ChatPane).pm_id == "pm")
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_80x24_tab_order_matches_the_design_priority(project):
    """design/tui.md: "Needs you first when non-empty, then Team, Tasks, Comms, Chat" — so the
    collapsed 80x24 tab bar and the expanded layout agree on what matters most."""
    from textual.widgets import TabPane

    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                titles = [tp._title.plain if hasattr(tp._title, "plain") else str(tp._title)
                         for tp in app.query(TabPane)]
                assert titles == ["Needs you", "Team", "Tasks", "Comms", "Chat"]
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_live_resize_collapses_and_restores_on_the_same_event(project):
    """#76: on_resize/_is_compact used to read self.size, which still held the *previous* size
    during the Resize event, so the layout lagged one resize behind (140x42 -> 80x24 stayed
    side-by-side; the next resize back to 140x42 showed tabs instead). Each resize below must take
    effect immediately, and the same pane instances must keep their loaded data throughout."""
    from textual.widgets import TabbedContent

    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                assert app.query_one("#body-row") and not app.query(TabbedContent)
                team_pane, tasks_pane = app._panes

                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                assert app.query(TabbedContent) and not app.query("#body-row")
                assert app._panes == [team_pane, tasks_pane]  # same instances, not rebuilt
                assert any(a["handle"] == "builder_1@t" for a in team_pane._agents)
                assert any(t["title"] == "TUI slice A" for t in tasks_pane._tasks)

                await pilot.resize_terminal(140, 42)
                await pilot.pause()
                assert app.query_one("#body-row") and not app.query(TabbedContent)
                assert app._panes == [team_pane, tasks_pane]
                assert any(a["handle"] == "builder_1@t" for a in team_pane._agents)
                assert any(t["title"] == "TUI slice A" for t in tasks_pane._tasks)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_tasks_pane_load_failure_shows_inline_error_and_app_keeps_running(project):
    """#108: a pane's load() failing (a dropped connection, an oversized response, whatever) must
    not take the rest of the TUI down with it -- app.py gathers every pane's load() together, so an
    uncaught exception here used to crash the whole app."""
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES,
                               errors={"tasks": ("internal", "boom")})
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                tasks_pane = app._panes[1]
                assert "couldn't load: boom" in tasks_pane.content.plain
                assert "r to retry" in tasks_pane.content.plain
                # the rest of the app is unaffected: Team still loaded fine
                assert any(a["handle"] == "builder_1@t" for a in app._panes[0]._agents)
                assert not app._exit

                del server.errors["tasks"]
                app.set_focus(tasks_pane)
                await pilot.press("r")
                await _wait_until(lambda: tasks_pane._tasks)
                assert any(t["title"] == "TUI slice A" for t in tasks_pane._tasks)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_any_pane_load_failure_is_survivable_not_just_tasks(project):
    """The base Pane class (panes/__init__.py), not just TasksPane, catches a load() failure --
    live-testing #108's fix against a realistically sized seeded project (300 tasks/8000 events/
    700 mail) surfaced the exact same crash from TeamPane's `agents` call timing out under load."""
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES,
                               errors={"agents": ("internal", "team unavailable")})
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                team_pane = app._panes[0]
                assert "couldn't load: team unavailable" in team_pane.content.plain
                # the rest of the app is unaffected: Tasks still loaded fine, app still running
                assert any(t["title"] == "TUI slice A" for t in app._panes[1]._tasks)
                assert not app._exit
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_empty_tasks_pane_shows_one_line_not_squashed_into_the_glyph_column(project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=[], usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                tasks_pane = app._panes[1]
                assert tasks_pane._tasks == []
                table = tasks_pane._table
                assert table.columns[0]._cells == [""]  # glyph column: not squashed into here
                assert table.columns[2]._cells[0].plain == "nothing in flight"  # title column
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_startup_focuses_the_chat_composer_and_typing_system_opens_no_modal(project):
    """#83: the TUI used to start with nothing focused, so plain typing fell through to
    app-level bindings — typing "system" opened the kill-switch confirm live in tmux."""
    from troupe.tui.panes.chat import Composer

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
                assert isinstance(app.focused, Composer)

                await pilot.press(*"system")
                await pilot.press("enter")
                await pilot.pause()
                assert len(app.screen_stack) == 1  # no confirm modal pushed
                await _wait_until(
                    lambda: ("chat", dict(agent="pm", text="system")) in server.commands)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_slash_focuses_the_chat_composer_from_another_pane(project):
    from textual.widgets import ListView

    from troupe.tui.panes.chat import Composer

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
                app.query_one("#ny-cards", ListView).focus()
                await pilot.pause()
                assert not isinstance(app.focused, Composer)

                await pilot.press("/")
                await pilot.pause()
                assert isinstance(app.focused, Composer)
        finally:
            await server.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("source_tab, focus_target", [
    ("tab-TeamPane", "Tabs"),
    ("tab-NeedsYouPane", "#ny-cards"),
    ("tab-FeedPane", "FeedPane"),
])
def test_slash_switches_to_the_chat_tab_in_compact_mode(project, source_tab, focus_target):
    from textual.widgets import TabbedContent

    from troupe.tui.panes.chat import Composer

    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(80, 24)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                tabs = app.query_one(TabbedContent)
                tabs.active = source_tab
                await pilot.pause()
                source = app.query_one(focus_target)
                source.focus()
                await pilot.pause()
                assert app.focused is source
                assert not isinstance(app.focused, Composer)

                await pilot.press("/")
                await pilot.pause()
                assert tabs.active == "tab-ChatPane"
                assert isinstance(app.focused, Composer)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_resize_round_trip_restores_focus_to_the_composer(project):
    from textual.widgets import TabbedContent

    from troupe.tui.panes.chat import Composer

    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, tasks=TASKS, usage=USAGE,
                               engine=ENGINE, milestones=MILESTONES)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                assert isinstance(app.focused, Composer)

                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                assert app.query(TabbedContent)
                assert isinstance(app.focused, Composer)

                await pilot.resize_terminal(140, 42)
                await pilot.pause()
                assert not app.query(TabbedContent)
                assert isinstance(app.focused, Composer)
        finally:
            await server.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(140, 42), (80, 24)])
@pytest.mark.parametrize("answer", ["y", "n"])
def test_stop_and_resume_dialogs_restore_composer_focus(project, size, answer):
    from troupe.tui.app import ConfirmScreen
    from troupe.tui.panes.chat import Composer

    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, engine=ENGINE)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=size) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                for key, method in [("s", "stop_now"), ("R", "resume")]:
                    if key == "R":
                        await server.push_event("engine.state", {
                            "engine": dict(ENGINE, state="stopped", stopped=True)})
                        await _wait_until(lambda: app._engine_stopped)
                        assert "STOPPED" in app._stopped_banner.content
                    app.set_focus(None)
                    await pilot.press(key)
                    await _wait_until(lambda: isinstance(app.screen, ConfirmScreen))
                    await pilot.press(answer)
                    await pilot.pause()
                    assert len(app.screen_stack) == 1
                    assert isinstance(app.focused, Composer)
                    assert any(m == method for m, _ in server.commands) == (answer == "y")
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_run_tui_exits_nonzero_when_the_app_panicked(project, monkeypatch):
    """#108: Textual's own crash handling catches an unhandled exception, prints it, and returns
    normally from App.run() with a non-zero return_code -- it never raises or exits the process on
    its own (its own docstring's example is `sys.exit(app.return_code)`). Without this, a TUI that
    crashed on startup looked exactly like a clean exit to anything checking the process."""
    from troupe.tui.app import TroupeApp, run_tui

    cfg, _store = project

    def fake_run(self, **kwargs):
        self._return_code = 1

    monkeypatch.setattr(TroupeApp, "run", fake_run)
    try:
        run_tui(cfg, owns_engine=True)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("run_tui did not exit non-zero after a panicked app")


def test_run_tui_exits_cleanly_when_the_app_did_not_panic(project, monkeypatch):
    from troupe.tui.app import TroupeApp, run_tui

    cfg, _store = project

    def fake_run(self, **kwargs):
        pass  # return_code stays None: a normal quit

    monkeypatch.setattr(TroupeApp, "run", fake_run)
    run_tui(cfg, owns_engine=True)  # must not raise SystemExit
