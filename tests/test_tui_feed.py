"""#69: the Comms feed pane (agent-to-agent mail + decisions, filterable) and the kill-switch key's
STOPPED banner / Resume / pre-#57 "unavailable" handling. REQ-TUI-010 (Comms), REQ-TUI-020 (`s`).
"""
import asyncio
import time

from tui_fixture import FixtureServer

from troupe.tui.app import TroupeApp
from troupe.tui.panes.feed import FeedPane

AGENTS = [
    dict(id="lead", handle="lead_1@t", name="Lead", role="lead", state="idle", status="",
         current_run=None, runs=3, tokens=0, cost=0.0, last_run_at=0.0, activity="",
         waiting_on=None, mail_queued=0, mail_reading=0),
    dict(id="builder-1", handle="builder_1@t", name="Builder 1", role="builder", state="running",
         status="", current_run=1, runs=5, tokens=0, cost=0.0, last_run_at=0.0,
         activity="editing app.py", waiting_on=None, mail_queued=2, mail_reading=1),
]
USAGE = dict(providers=[])
ENGINE_LIVE = dict(state="live", stopped=False, paused=False, throttled=None, heartbeat=0,
                   version="0", pid=1, started_at=0, running_runs=0, draining_runs=0, config_errors=[])
MILESTONES = []


def message(id, sender, recipient, subject="", body="", fyi=False, kind="msg", ts=None):
    return dict(id=id, ts=ts if ts is not None else id, sender=sender, recipient=recipient,
               subject=subject, body=body, kind=kind, reply_to=None, task_id=None,
               read_at=None, fyi=fyi, room=None)


def decision(id, agent, title, rationale="", ts=None):
    return dict(id=id, ts=ts if ts is not None else id, agent=agent, kind="decision", title=title,
               content="", rationale=rationale, scope="team", pinned=False, superseded_by=None,
               status="active", major=False, comments_open=0)


def _app(project, **server_kwargs):
    cfg, _store = project
    return TroupeApp(cfg, owns_engine=True), cfg


async def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.02)


def lines_of(pane: FeedPane) -> list[str]:
    log = pane.query_one("#feed-log")
    return [s.text.rstrip() for s in log.lines]


def test_feed_shows_messages_and_decisions_oldest_to_newest_and_ignores_chat(project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, usage=USAGE, engine=ENGINE_LIVE, milestones=MILESTONES,
                               messages=[
                                   message(1, "lead", "builder-1", subject="Do the thing", ts=100),
                                   message(2, "human", "lead", subject="chat here", kind="chat", ts=150),
                               ],
                               memories=[decision(1, "lead", "Use SQLite", ts=125)])
        await server.start()
        try:
            app, _ = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                feed = next(p for p in app._right_panes if isinstance(p, FeedPane))
                assert isinstance(feed, FeedPane)
                text = lines_of(feed)
                assert any("Do the thing" in line for line in text)
                assert any("Use SQLite" in line for line in text)
                assert not any("chat here" in line for line in text)  # chat kind, not agent-to-agent mail
                # sticky-to-bottom: newest (decision at ts=125, then nothing later) after the ts=100 mail
                mail_idx = next(i for i, line in enumerate(text) if "Do the thing" in line)
                decision_idx = next(i for i, line in enumerate(text) if "Use SQLite" in line)
                assert mail_idx < decision_idx
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_feed_fyi_filter_cycles_all_action_fyi(project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, usage=USAGE, engine=ENGINE_LIVE, milestones=MILESTONES,
                               messages=[
                                   message(1, "lead", "builder-1", subject="Action item", fyi=False, ts=100),
                                   message(2, "lead", "builder-1", subject="FYI note", fyi=True, ts=101),
                               ])
        await server.start()
        try:
            app, _ = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                feed = next(p for p in app._right_panes if isinstance(p, FeedPane))
                feed.focus()
                assert any("Action item" in l for l in lines_of(feed))
                assert any("FYI note" in l for l in lines_of(feed))

                await pilot.press("f")  # -> FYI only
                await pilot.pause()
                assert not any("Action item" in l for l in lines_of(feed))
                assert any("FYI note" in l for l in lines_of(feed))
                assert "FYI only" in feed.border_title

                await pilot.press("f")  # -> action only
                await pilot.pause()
                assert any("Action item" in l for l in lines_of(feed))
                assert not any("FYI note" in l for l in lines_of(feed))
                assert "action only" in feed.border_title

                await pilot.press("f")  # -> back to all
                await pilot.pause()
                assert any("Action item" in l for l in lines_of(feed))
                assert any("FYI note" in l for l in lines_of(feed))
                assert feed.border_title == "COMMS"
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_feed_decisions_only_toggle(project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, usage=USAGE, engine=ENGINE_LIVE, milestones=MILESTONES,
                               messages=[message(1, "lead", "builder-1", subject="Mail here", ts=100)],
                               memories=[decision(1, "lead", "A decision", ts=101)])
        await server.start()
        try:
            app, _ = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                feed = next(p for p in app._right_panes if isinstance(p, FeedPane))
                feed.focus()
                await pilot.press("d")
                await pilot.pause()
                text = lines_of(feed)
                assert not any("Mail here" in l for l in text)
                assert any("A decision" in l for l in text)
                assert "decisions only" in feed.border_title
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_feed_agent_filter_cycles_through_participants(project):
    """Filtering by an agent shows items that agent participates in (sender or recipient for mail,
    author for a decision) — not just items where it's uniquely the only party."""
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, usage=USAGE, engine=ENGINE_LIVE, milestones=MILESTONES,
                               messages=[message(1, "lead", "qa", subject="Lead to QA", ts=100),
                                        message(2, "builder-1", "qa", subject="Builder to QA", ts=101)],
                               memories=[decision(1, "builder-1", "Builder decision", ts=102)])
        await server.start()
        try:
            app, _ = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                feed = next(p for p in app._right_panes if isinstance(p, FeedPane))
                feed.focus()
                await pilot.press("]")  # first agent alphabetically: builder-1
                await pilot.pause()
                assert "builder_1@t" in feed.border_title
                text = lines_of(feed)
                assert any("Builder to QA" in l for l in text)
                assert any("Builder decision" in l for l in text)
                assert not any("Lead to QA" in l for l in text)

                await pilot.press("]")  # next: lead
                await pilot.pause()
                assert "lead_1@t" in feed.border_title
                text = lines_of(feed)
                assert any("Lead to QA" in l for l in text)
                assert not any("Builder to QA" in l for l in text)

                await pilot.press("]")  # next: qa (not in the agents snapshot — falls back to raw id)
                await pilot.pause()
                assert "qa" in feed.border_title

                await pilot.press("]")  # wraps back to "all"
                await pilot.pause()
                assert feed.border_title == "COMMS"
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_feed_live_events_append_message_and_upsert_decision(project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, usage=USAGE, engine=ENGINE_LIVE, milestones=MILESTONES)
        await server.start()
        try:
            app, _ = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                feed = next(p for p in app._right_panes if isinstance(p, FeedPane))

                await server.push_event("message.new", {"message": message(9, "lead", "builder-1", subject="Live mail")})
                await _wait_until(lambda: any("Live mail" in l for l in lines_of(feed)))

                await server.push_event("memory.new", {"memory": decision(9, "lead", "Live decision v1")})
                await _wait_until(lambda: any("Live decision v1" in l for l in lines_of(feed)))

                await server.push_event("memory.changed", {"memory": decision(9, "lead", "Live decision v2")})
                await _wait_until(lambda: any("Live decision v2" in l for l in lines_of(feed)))
                assert not any("Live decision v1" in l for l in lines_of(feed))  # replaced, not duplicated
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_kill_switch_shows_unavailable_before_57(project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, usage=USAGE, engine=ENGINE_LIVE, milestones=MILESTONES,
                               errors={"stop_now": ("unavailable", "stop_now is not available yet (task #57)")})
        await server.start()
        try:
            app, _ = _app(project)
            notices = []
            app.notify = lambda message, **kw: notices.append((message, kw.get("severity")))
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                await pilot.press("tab")  # #83: startup now focuses the chat composer, which eats "s"
                await pilot.press("s")
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause(0.2)
                assert any("#57" in m and sev == "warning" for m, sev in notices)
                assert not app._engine_stopped
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_kill_switch_success_shows_stopped_banner_and_resume_recovers(project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, usage=USAGE, engine=ENGINE_LIVE, milestones=MILESTONES)
        await server.start()
        try:
            app, _ = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                assert not app.query(".-visible")
                assert app.check_action("resume_action", ()) is False

                await pilot.press("tab")  # #83: startup now focuses the chat composer, which eats "s"
                await pilot.press("s")
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause(0.1)
                assert any(m == "stop_now" for m, _ in server.commands)

                # The banner reacts to the pushed engine.state event, the same source of truth a
                # real second client would see it from — not a local flip on a successful call.
                await server.push_event("engine.state", {"engine": dict(ENGINE_LIVE, state="stopped", stopped=True)})
                await _wait_until(lambda: app._engine_stopped)
                await pilot.pause()
                assert app.query(".-visible")
                assert "STOPPED" in app._stopped_banner.content
                assert app.check_action("resume_action", ()) is True

                await pilot.press("tab")  # #83: closing the kill-switch dialog refocuses the composer, which eats "R"
                await pilot.press("R")
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause(0.1)
                assert any(m == "resume" for m, _ in server.commands)

                await server.push_event("engine.state", {"engine": dict(ENGINE_LIVE, state="live", stopped=False)})
                await _wait_until(lambda: not app._engine_stopped)
                await pilot.pause()
                assert not app.query(".-visible")
                assert app.check_action("resume_action", ()) is False
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_resume_key_is_inert_while_not_stopped(project):
    """check_action gates it off, so it's neither shown in the footer nor triggerable."""
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, usage=USAGE, engine=ENGINE_LIVE, milestones=MILESTONES)
        await server.start()
        try:
            app, _ = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                assert app.check_action("resume_action", ()) is False
                await pilot.press("R")
                await pilot.pause()
                assert len(app.screen_stack) == 1  # no confirm screen was pushed
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_snapshot_renders_without_error(tmp_path, project):
    cfg, _store = project

    async def scenario():
        server = FixtureServer(cfg.root, agents=AGENTS, usage=USAGE, engine=ENGINE_LIVE, milestones=MILESTONES,
                               messages=[message(1, "lead", "builder-1", subject="Snapshot mail")],
                               memories=[decision(1, "lead", "Snapshot decision")])
        await server.start()
        try:
            app, _ = _app(project)
            async with app.run_test(size=(140, 42)) as pilot:
                await _wait_until(lambda: app.client.connected)
                await pilot.pause()
                svg = pilot.app.export_screenshot()
                assert "<svg" in svg
                # Border titles don't come through Textual's SVG export in this version, so this
                # checks actual pane content rather than the "COMMS" border label.
                assert "Snapshot" in svg
                (tmp_path / "feed.svg").write_text(svg)
        finally:
            await server.stop()

    asyncio.run(scenario())
