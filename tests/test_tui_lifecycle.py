"""REQ-TUI-001: "run and everything runs, quit and everything quits" — engine lifecycle."""
import asyncio
import os

import pytest
from tui_fixture import FixtureServer

from troupe.tui import lifecycle
from troupe.tui.app import TroupeApp

ENGINE_IDLE = dict(state="live", stopped=False, paused=False, throttled=None, heartbeat=0,
                   version="0", pid=1, started_at=0, running_runs=0, draining_runs=0, config_errors=[])
ENGINE_BUSY = dict(ENGINE_IDLE, running_runs=2)


async def _wait_until(predicate, timeout=5.0):  # #71: generous default under load
    import time
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.02)


def test_ensure_engine_starts_a_child_when_none_is_running(project, monkeypatch):
    cfg, _store = project
    calls = []
    monkeypatch.setattr(lifecycle, "service_status", lambda root: {"state": "stopped"})
    monkeypatch.setattr(lifecycle, "start_service", lambda c: calls.append(c))

    owns = asyncio.run(lifecycle.ensure_engine(cfg))

    assert owns is True
    assert calls == [cfg]


def test_ensure_engine_attaches_without_owning_when_already_running(project, monkeypatch):
    cfg, _store = project
    monkeypatch.setattr(lifecycle, "service_status", lambda root: {"state": "running"})
    monkeypatch.setattr(lifecycle, "start_service", lambda c: None)
    monkeypatch.setattr(lifecycle, "owner_alive", lambda root: True)
    set_owner_calls = []
    monkeypatch.setattr(lifecycle, "set_owner", lambda d, pid: set_owner_calls.append((d, pid)))

    owns = asyncio.run(lifecycle.ensure_engine(cfg))

    assert owns is False
    assert set_owner_calls == []


def test_ensure_engine_starts_a_child_records_ownership(project, monkeypatch):
    cfg, _store = project
    monkeypatch.setattr(lifecycle, "service_status", lambda root: {"state": "stopped"})
    monkeypatch.setattr(lifecycle, "start_service", lambda c: None)
    set_owner_calls = []
    monkeypatch.setattr(lifecycle, "set_owner", lambda d, pid: set_owner_calls.append((d, pid)))

    owns = asyncio.run(lifecycle.ensure_engine(cfg))

    assert owns is True
    assert set_owner_calls == [(cfg.state_dir, os.getpid())]


def test_ensure_engine_readopts_an_orphan_whose_owner_is_dead(project, monkeypatch):
    """#100 F3: after a previous TUI died without stopping its engine (a `kill -9`, which can't be
    caught), a later `troupe` must adopt the still-running engine rather than attach and leave it
    forever un-stoppable via `q`."""
    cfg, _store = project
    monkeypatch.setattr(lifecycle, "service_status", lambda root: {"state": "running"})
    monkeypatch.setattr(lifecycle, "start_service", lambda c: None)
    monkeypatch.setattr(lifecycle, "owner_alive", lambda root: False)
    set_owner_calls = []
    monkeypatch.setattr(lifecycle, "set_owner", lambda d, pid: set_owner_calls.append((d, pid)))

    owns = asyncio.run(lifecycle.ensure_engine(cfg))

    assert owns is True
    assert set_owner_calls == [(cfg.state_dir, os.getpid())]


def test_restart_engine_only_acts_when_offline(project, monkeypatch):
    cfg, _store = project
    calls = []
    monkeypatch.setattr(lifecycle, "service_status", lambda root: {"state": "running"})
    monkeypatch.setattr(lifecycle, "start_service", lambda c: calls.append(c))
    assert asyncio.run(lifecycle.restart_engine(cfg)) is False
    assert calls == []

    monkeypatch.setattr(lifecycle, "service_status", lambda root: {"state": "stopped"})
    assert asyncio.run(lifecycle.restart_engine(cfg)) is True
    assert calls == [cfg]


def test_quit_with_no_runs_in_flight_stops_the_owned_engine(project, monkeypatch):
    cfg, _store = project
    stop_calls = []
    monkeypatch.setattr("troupe.tui.app.stop_owned_engine",
                        lambda c: stop_calls.append(c) or asyncio.sleep(0))

    async def scenario():
        server = FixtureServer(cfg.root, engine=ENGINE_IDLE)
        await server.start()
        app = TroupeApp(cfg, owns_engine=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_until(lambda: app.client.connected)
            await pilot.press("tab")  # #83: startup now focuses the chat composer, which eats "q"
            await pilot.press("q")
            await _wait_until(lambda: stop_calls == [cfg])
        await server.stop()

    asyncio.run(scenario())
    assert stop_calls == [cfg]


def test_quit_with_runs_in_flight_asks_first_and_a_no_answer_cancels(project, monkeypatch):
    cfg, _store = project
    stop_calls = []
    monkeypatch.setattr("troupe.tui.app.stop_owned_engine",
                        lambda c: stop_calls.append(c) or asyncio.sleep(0))

    async def scenario():
        server = FixtureServer(cfg.root, engine=ENGINE_BUSY)
        await server.start()
        app = TroupeApp(cfg, owns_engine=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_until(lambda: app.client.connected)
            await pilot.press("tab")  # #83: startup now focuses the chat composer, which eats "q"
            await pilot.press("q")
            await _wait_until(lambda: len(app.screen_stack) > 1)  # the confirmation modal is up
            await pilot.press("n")
            await pilot.pause()
            assert not app._exit
        await server.stop()

    asyncio.run(scenario())
    assert stop_calls == []


def test_quit_with_runs_in_flight_stops_on_yes(project, monkeypatch):
    cfg, _store = project
    stop_calls = []
    monkeypatch.setattr("troupe.tui.app.stop_owned_engine",
                        lambda c: stop_calls.append(c) or asyncio.sleep(0))

    async def scenario():
        server = FixtureServer(cfg.root, engine=ENGINE_BUSY)
        await server.start()
        app = TroupeApp(cfg, owns_engine=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_until(lambda: app.client.connected)
            await pilot.press("tab")  # #83: startup now focuses the chat composer, which eats "q"
            await pilot.press("q")
            await _wait_until(lambda: len(app.screen_stack) > 1)
            await pilot.press("y")
            await _wait_until(lambda: stop_calls == [cfg])
        await server.stop()

    asyncio.run(scenario())
    assert stop_calls == [cfg]


def test_ctrl_q_quits_with_confirm_even_while_the_composer_is_focused(project, monkeypatch):
    """QA #83: action_quit was `async def ... : await self.action_quit_app()`, but
    action_quit_app is @work — awaiting the Worker it returns raised TypeError, killing the app
    before the confirm dialog or stop_owned_engine ran. ctrl+q is Textual's own priority binding
    (fires regardless of focus), and #83 made plain "q" text once the composer has startup focus,
    so ctrl+q is now the one key that must always quit cleanly."""
    from troupe.tui.panes.chat import Composer

    cfg, _store = project
    stop_calls = []
    monkeypatch.setattr("troupe.tui.app.stop_owned_engine",
                        lambda c: stop_calls.append(c) or asyncio.sleep(0))

    async def scenario():
        server = FixtureServer(cfg.root, engine=dict(ENGINE_BUSY, running_runs=3))
        await server.start()
        app = TroupeApp(cfg, owns_engine=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_until(lambda: app.client.connected)
            await pilot.pause()
            assert isinstance(app.focused, Composer)  # startup focus (#83), unlike plain "q"
            await pilot.press("ctrl+q")
            await _wait_until(lambda: len(app.screen_stack) > 1)
            await pilot.press("n")
            await pilot.pause()
            assert not app._exit and stop_calls == []

            await pilot.press("ctrl+q")
            await _wait_until(lambda: len(app.screen_stack) > 1)
            await pilot.press("y")
            await _wait_until(lambda: stop_calls == [cfg])
        await server.stop()

    asyncio.run(scenario())
    assert stop_calls == [cfg]


def test_non_owner_quit_leaves_the_engine_running(project, monkeypatch):
    cfg, _store = project
    stop_calls = []
    monkeypatch.setattr("troupe.tui.app.stop_owned_engine",
                        lambda c: stop_calls.append(c) or asyncio.sleep(0))

    async def scenario():
        server = FixtureServer(cfg.root, engine=ENGINE_IDLE)
        await server.start()
        app = TroupeApp(cfg, owns_engine=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_until(lambda: app.client.connected)
            await pilot.press("tab")  # #83: startup now focuses the chat composer, which eats "q"
            await pilot.press("q")
            await _wait_until(lambda: app._exit)
        await server.stop()

    asyncio.run(scenario())
    assert stop_calls == []


def test_sighup_stops_everything_without_asking_when_owner(project, monkeypatch):
    cfg, _store = project
    stop_calls = []
    monkeypatch.setattr("troupe.tui.app.stop_owned_engine",
                        lambda c, timeout=15: stop_calls.append((c, timeout)) or asyncio.sleep(0))

    async def scenario():
        app = TroupeApp(cfg, owns_engine=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await app._kill_now()
            await pilot.pause()

    asyncio.run(scenario())
    assert stop_calls == [(cfg, 5)]


def test_sighup_does_not_touch_a_foreign_engine(project, monkeypatch):
    cfg, _store = project
    stop_calls = []
    monkeypatch.setattr("troupe.tui.app.stop_owned_engine",
                        lambda c, timeout=15: stop_calls.append((c, timeout)) or asyncio.sleep(0))

    async def scenario():
        app = TroupeApp(cfg, owns_engine=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await app._kill_now()
            await pilot.pause()

    asyncio.run(scenario())
    assert stop_calls == []


def test_ctrl_c_kills_immediately_even_while_the_composer_is_focused(project, monkeypatch):
    """#100 F1: Input/TextArea (the chat composer) bind ctrl+c to "copy selected text" (show=False)
    and would otherwise eat the keypress before App-level bindings ever see it. The App's own
    ctrl+c binding is registered with priority=True (REQ-TUI-001), which Textual checks from the
    App down *before* the focused widget's own bindings -- so this must fire no matter what."""
    from troupe.tui.panes.chat import Composer

    cfg, _store = project
    stop_calls = []
    monkeypatch.setattr("troupe.tui.app.stop_owned_engine",
                        lambda c, timeout=15: stop_calls.append((c, timeout)) or asyncio.sleep(0))

    async def scenario():
        server = FixtureServer(cfg.root, engine=ENGINE_BUSY)
        await server.start()
        app = TroupeApp(cfg, owns_engine=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_until(lambda: app.client.connected)
            await pilot.pause()
            assert isinstance(app.focused, Composer)  # startup focus (#83)
            await pilot.press("ctrl+c")
            await _wait_until(lambda: app._exit)
            assert len(app.screen_stack) == 1  # no confirm dialog: kill intent
        await server.stop()

    asyncio.run(scenario())
    assert stop_calls == [(cfg, 5)]


def test_second_ctrl_c_escalates_to_immediate_kill(project, monkeypatch):
    """REQ-TUI-001: "a second Ctrl-C SIGKILLs any remaining runs" — no waiting out the first
    call's graceful-stop window."""
    stop_calls, kill_calls = [], []
    cfg, _store = project

    async def slow_stop(c, timeout=15):
        stop_calls.append((c, timeout))
        await asyncio.sleep(10)  # never resolves within the test; the second Ctrl-C must not wait

    monkeypatch.setattr("troupe.tui.app.stop_owned_engine", slow_stop)
    monkeypatch.setattr("troupe.tui.app.kill_owned_engine_now",
                        lambda c: kill_calls.append(c) or asyncio.sleep(0))

    async def scenario():
        app = TroupeApp(cfg, owns_engine=True)
        async with app.run_test(size=(120, 40)) as pilot:
            asyncio.ensure_future(app._kill_now())
            await pilot.pause()
            await app._kill_now()
            await _wait_until(lambda: app._exit)

    asyncio.run(scenario())
    assert len(stop_calls) == 1
    assert kill_calls == [cfg]
