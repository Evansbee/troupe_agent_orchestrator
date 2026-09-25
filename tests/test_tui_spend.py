"""REQ-ENG-070: the TUI's `$` spend screen against a fixture API server (no live engine)."""
import asyncio
import time

from tui_fixture import FixtureServer

from troupe.tui.app import TroupeApp
from troupe.tui.spend_screen import SpendScreen

SPEND = dict(
    total=dict(runs=5, cost=9.25, tokens=2950, wall_seconds=600.0,
               rework_runs=1, rework_cost=2.0, rework_share=0.2),
    by="agent",
    rows=[dict(agent="builder-1", runs=5, cost=9.25, tokens=2950, wall_seconds=600.0,
               rework_runs=1, rework_cost=2.0, rework_share=0.2, avg_cost=1.85,
               produced_nothing=0, produced_nothing_share=0.0)],
)


def _app(project):
    cfg, _store = project
    return TroupeApp(cfg, owns_engine=True)


async def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if predicate():
                return
        except AttributeError:
            pass  # #spend-body's content is a bare str until the first reload() lands
        if time.monotonic() > deadline:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.02)


def _body_text(screen) -> str:
    content = screen.query_one("#spend-body").content
    return content.plain if hasattr(content, "plain") else str(content)


def test_dollar_key_opens_spend_screen_with_report(project):
    from textual.widgets import ListView
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, spend=SPEND)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_until(lambda: app.client.connected)
                # "$" is a plain (non-priority) character binding like "s"/"r": it only fires while
                # browsing another pane, not while the composer -- focused by default -- is typing.
                app.query_one("#ny-cards", ListView).focus()
                await pilot.pause()
                await pilot.press("$")
                await pilot.pause()
                screen = app.screen
                assert isinstance(screen, SpendScreen)
                await _wait_until(lambda: "builder-1" in _body_text(screen))
                body = _body_text(screen)
                assert "$9.25" in body
                assert "rework 1/5" in body
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, SpendScreen)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_tab_cycles_group_by_and_refetches(project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, spend=SPEND)
        await server.start()
        try:
            app = _app(project)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_until(lambda: app.client.connected)
                screen = SpendScreen(app.client)
                await app.push_screen(screen)
                await _wait_until(lambda: screen.by == "agent" and "builder-1" in _body_text(screen))
                await pilot.press("tab")
                await pilot.pause()
                assert screen.by == "task"
                await _wait_until(lambda: "no runs" in _body_text(screen))
        finally:
            await server.stop()

    asyncio.run(scenario())
