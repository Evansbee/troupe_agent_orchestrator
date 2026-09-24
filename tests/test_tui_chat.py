"""#68 TUI slice C: PM chat pane. Pilot tests against a fixture API client (REQ-TUI-031)."""
from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static

from troupe.tui.panes.chat import ChatPane, Composer

PM = {"id": "pm_1", "name": "pm_1", "role": "pm", "state": "idle"}


class FixtureClient:
    """A local stub for the (not-yet-landed) #66 TuiClient: async .call(method, timeout=5.0, **params)."""

    def __init__(self, agents=(PM,), messages=(), fail_chat=False, chat_gate=None):
        self.agents = list(agents)
        self.messages = list(messages)
        self.calls: list[tuple[str, float, dict]] = []
        self.fail_chat = fail_chat
        self.chat_gate = chat_gate  # an asyncio.Event a test can hold closed to simulate a slow send
        self._next_id = max((m["id"] for m in self.messages), default=0) + 1

    async def call(self, method, timeout=5.0, **params):
        self.calls.append((method, timeout, params))
        if method == "agents":
            return {"items": self.agents}
        if method == "messages":
            chat_with = params.get("chat_with")
            limit = params.get("limit", 100)
            items = [m for m in self.messages
                     if m["kind"] == "chat" and {m["sender"], m["recipient"]} == {"human", chat_with}]
            return {"items": list(reversed(items))[:limit]}
        if method == "chat":
            if self.chat_gate is not None:
                await self.chat_gate.wait()
            if self.fail_chat:
                raise TimeoutError("engine offline")
            msg = {"id": self._next_id, "sender": "human", "recipient": params["agent"],
                   "kind": "chat", "body": params["text"]}
            self._next_id += 1
            self.messages.append(msg)
            return {"message_id": msg["id"]}
        raise AssertionError(f"unexpected method {method}")


def message(mid, sender, recipient, body, kind="chat"):
    return {"id": mid, "sender": sender, "recipient": recipient, "body": body, "kind": kind}


class ChatTestApp(App):
    def __init__(self, client):
        super().__init__()
        self.client = client

    def compose(self) -> ComposeResult:
        yield ChatPane(self.client, id="chat")

    async def on_mount(self) -> None:
        await self.query_one(ChatPane).load()


def push(pane: ChatPane, event: str, **data):
    pane.on_troupe_event({"event": event, "seq": 1, "data": data})


async def push_and_settle(pilot, pane, event, settle_pauses=5, **data):
    """push() a message.new/agent.state event and wait for its effects to fully land.

    message.new dispatches to a background worker (mount the row, maybe scroll_end()), and
    scroll_end() itself only *schedules* its scroll via call_after_refresh — neither is guaranteed
    to have visibly landed after a single pilot.pause(), especially under load. Wait for the
    worker, then pump a few more refresh cycles to flush any chained deferred callback."""
    push(pane, event, **data)
    await pilot.app.workers.wait_for_complete()
    for _ in range(settle_pauses):
        await pilot.pause()


def test_load_populates_thread_oldest_first_and_finds_pm_by_role():
    client = FixtureClient(messages=[
        message(1, "human", "pm_1", "hi"),
        message(2, "pm_1", "human", "hello there"),
    ])

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            assert pane.pm_id == "pm_1"
            thread = pane.query_one("#chat-thread", VerticalScroll)
            bodies = list(thread.query(Markdown))
            assert len(bodies) == 2
            methods = {(m, t) for m, t, _ in client.calls}
            assert ("agents", 5.0) in methods and ("messages", 5.0) in methods
            _, _, params = next(c for c in client.calls if c[0] == "messages")
            assert params["chat_with"] == "pm_1"
    asyncio.run(scenario())


def test_send_calls_chat_with_hard_timeout_and_clears_composer():
    client = FixtureClient()

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            composer = pane.query_one("#chat-composer", Composer)
            composer.focus()
            await pilot.pause()
            for ch in "hello pm":
                await pilot.press("space" if ch == " " else ch)
            await pilot.press("enter")
            await pilot.pause()
            assert composer.text == ""
            chat_calls = [c for c in client.calls if c[0] == "chat"]
            assert len(chat_calls) == 1
            _, timeout, params = chat_calls[0]
            assert timeout == 5.0
            assert params == {"agent": "pm_1", "text": "hello pm"}
    asyncio.run(scenario())


def test_shift_enter_inserts_newline_instead_of_sending():
    client = FixtureClient()

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            composer = pane.query_one("#chat-composer", Composer)
            composer.focus()
            await pilot.pause()
            await pilot.press("a")
            await pilot.press("shift+enter")
            await pilot.press("b")
            await pilot.pause()
            assert composer.text == "a\nb"
            assert not any(c[0] == "chat" for c in client.calls)
    asyncio.run(scenario())


def test_pm_working_indicator_tracks_running_state_and_reply():
    client = FixtureClient(messages=[message(1, "human", "pm_1", "hi")])

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            working = pane.query_one("#chat-working", Static)
            assert working.content == ""
            push(pane, "agent.state", agent={"id": "pm_1", "state": "running"})
            await pilot.pause()
            assert "is working" in str(working.content)
            await push_and_settle(pilot, pane, "message.new", message=message(2, "pm_1", "human", "hello!"))
            assert working.content == ""
    asyncio.run(scenario())


def test_a_markup_looking_pm_name_is_escaped_not_interpreted():
    """QA non-blocking ask: escape the name label so a weird agent name can't inject Rich markup."""
    weird_pm = {"id": "pm_1", "name": "[red]pm[/red]", "role": "pm", "state": "idle"}
    client = FixtureClient(agents=(weird_pm,), messages=[message(1, "pm_1", "human", "hi")])

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            thread = pane.query_one("#chat-thread", VerticalScroll)
            header = thread.children[0].query_one(Static)
            assert "\\[red]pm\\[/red]" in header.content
    asyncio.run(scenario())


def test_working_indicator_shows_the_pms_live_activity():
    client = FixtureClient(messages=[message(1, "human", "pm_1", "hi")])

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            working = pane.query_one("#chat-working", Static)
            push(pane, "agent.state", agent={"id": "pm_1", "state": "running", "activity": "reading #62"})
            await pilot.pause()
            assert "is working… reading #62" in str(working.content)
            push(pane, "agent.state", agent={"id": "pm_1", "state": "running", "activity": "writing tests"})
            await pilot.pause()
            assert "is working… writing tests" in str(working.content)
    asyncio.run(scenario())


def test_working_indicator_ignores_other_agents_and_unrelated_mail():
    client = FixtureClient()

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            working = pane.query_one("#chat-working", Static)
            push(pane, "agent.state", agent={"id": "builder-1", "state": "running"})
            await push_and_settle(pilot, pane, "message.new",
                                   message=message(9, "lead", "builder-1", "unrelated", kind="msg"))
            assert working.content == ""
            thread = pane.query_one("#chat-thread", VerticalScroll)
            assert len(thread.children) == 0
    asyncio.run(scenario())


def test_sticks_to_bottom_unless_the_human_scrolled_away():
    seed = [message(i, "pm_1" if i % 2 else "human", "human" if i % 2 else "pm_1", f"line {i}\n" * 3)
            for i in range(1, 31)]
    client = FixtureClient(messages=seed)

    async def scenario():
        async with ChatTestApp(client).run_test(size=(80, 24)) as pilot:
            pane = pilot.app.query_one(ChatPane)
            thread = pane.query_one("#chat-thread", VerticalScroll)
            await pilot.pause()
            assert thread.max_scroll_y > 0, "test needs overflow to be meaningful"
            assert thread.is_vertical_scroll_end
            await push_and_settle(pilot, pane, "message.new", message=message(31, "pm_1", "human", "still stuck"))
            assert thread.is_vertical_scroll_end

            thread.scroll_home(animate=False)
            await pilot.pause()
            assert not thread.is_vertical_scroll_end
            await push_and_settle(pilot, pane, "message.new",
                                   message=message(32, "pm_1", "human", "should not yank the view"))
            assert not thread.is_vertical_scroll_end
    asyncio.run(scenario())


def test_duplicate_message_push_is_not_double_mounted():
    seeded = message(1, "human", "pm_1", "hi")
    client = FixtureClient(messages=[seeded])

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            thread = pane.query_one("#chat-thread", VerticalScroll)
            assert len(thread.children) == 1
            await push_and_settle(pilot, pane, "message.new", message=seeded)
            assert len(thread.children) == 1
    asyncio.run(scenario())


def test_send_failure_keeps_the_text_in_the_box():
    """QA #68 repro A: a failed send (engine offline/timeout) must not lose the human's message."""
    client = FixtureClient(fail_chat=True)

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            composer = pane.query_one("#chat-composer", Composer)
            composer.focus()
            await pilot.pause()
            await pilot.press("h", "i", "enter")
            await pilot.pause()
            assert composer.text == "hi"
            assert any("Not sent" in n.message for n in pilot.app._notifications)
            assert any(c[0] == "chat" for c in client.calls)  # the call was made, just failed
            assert pane._sending is False  # doesn't get stuck locked out after a failure
    asyncio.run(scenario())


def test_no_pm_keeps_text_and_notifies_without_calling_chat():
    """QA #68 repro B: load() finding no PM must not silently discard what the human typed."""
    client = FixtureClient(agents=())

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            assert pane.pm_id is None
            composer = pane.query_one("#chat-composer", Composer)
            composer.focus()
            await pilot.pause()
            await pilot.press("h", "i", "enter")
            await pilot.pause()
            assert composer.text == "hi"
            assert not any(c[0] == "chat" for c in client.calls)
            assert any("No PM" in n.message for n in pilot.app._notifications)
    asyncio.run(scenario())


def test_double_enter_during_a_slow_send_only_sends_once():
    """QA #68 ask: pressing Enter again while a send is still in flight must not double-send."""
    gate = asyncio.Event()
    client = FixtureClient(chat_gate=gate)

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            composer = pane.query_one("#chat-composer", Composer)
            composer.focus()
            await pilot.pause()
            await pilot.press("h", "i")
            await pilot.press("enter")
            await pilot.pause()
            assert pane._sending is True
            chat_calls = [c for c in client.calls if c[0] == "chat"]
            assert len(chat_calls) == 1
            # The text is still visible (not yet cleared) and non-empty, so a second Enter would
            # re-post Submitted with the same text if the pane didn't guard against it.
            await pilot.press("enter")
            await pilot.pause()
            assert len([c for c in client.calls if c[0] == "chat"]) == 1
            gate.set()
            await pilot.pause()
            assert composer.text == ""
            assert pane._sending is False
            assert len([c for c in client.calls if c[0] == "chat"]) == 1
    asyncio.run(scenario())
