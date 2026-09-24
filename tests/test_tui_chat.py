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

    def __init__(self, agents=(PM,), messages=(), fail_chat=False):
        self.agents = list(agents)
        self.messages = list(messages)
        self.calls: list[tuple[str, float, dict]] = []
        self.fail_chat = fail_chat
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
            push(pane, "message.new", message=message(2, "pm_1", "human", "hello!"))
            await pilot.pause()
            assert working.content == ""
    asyncio.run(scenario())


def test_working_indicator_ignores_other_agents_and_unrelated_mail():
    client = FixtureClient()

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            working = pane.query_one("#chat-working", Static)
            push(pane, "agent.state", agent={"id": "builder-1", "state": "running"})
            push(pane, "message.new", message=message(9, "lead", "builder-1", "unrelated", kind="msg"))
            await pilot.pause()
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
            push(pane, "message.new", message=message(31, "pm_1", "human", "still stuck"))
            await pilot.pause()
            assert thread.is_vertical_scroll_end

            thread.scroll_home(animate=False)
            await pilot.pause()
            assert not thread.is_vertical_scroll_end
            push(pane, "message.new", message=message(32, "pm_1", "human", "should not yank the view"))
            await pilot.pause()
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
            push(pane, "message.new", message=seeded)
            await pilot.pause()
            assert len(thread.children) == 1
    asyncio.run(scenario())


def test_send_failure_notifies_instead_of_crashing():
    client = FixtureClient(fail_chat=True)

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            composer = pane.query_one("#chat-composer", Composer)
            composer.focus()
            await pilot.pause()
            await pilot.press("h", "i", "enter")
            await pilot.pause()
            assert composer.text == ""
            assert any("Couldn't reach the engine" in n.message for n in pilot.app._notifications)
    asyncio.run(scenario())
