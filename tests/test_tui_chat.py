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

    message.new dispatches to a background worker (_handle_new_message), which — only if the
    thread was already scrolled to the bottom — starts a *second*, nested worker (_settle_scroll)
    to wait for the new message's layout to land before scrolling (#104: that wait can take real,
    observable time, since Markdown content lands over more than one layout pass). A single
    `wait_for_complete()` call only snapshots whichever workers already exist *at that instant* —
    called right after push(), that's just the outer worker, which returns almost immediately,
    before it has even spawned the nested one. Looping until no workers remain at all catches
    however many levels of nested worker there are, however long the last one takes."""
    push(pane, event, **data)
    while list(pilot.app.workers):
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
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
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
            await pilot.app.workers.wait_for_complete()
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


def test_long_realistic_history_settles_to_the_bottom_even_in_a_small_viewport():
    """#85: in an isolated ChatPane with the whole terminal to itself, a single layout pass is
    enough and this never reproduces — the live repro (/tmp/rt/demo/recipe-box) only showed up in
    the real five-pane TroupeApp, where the chat thread gets a small fraction of a typical
    terminal's height and long/markdown content needs more than one layout pass to reach its
    final wrapped size. See test_tui_shell.py's version of this test for the five-pane repro; this
    one just pins down the ChatPane-level contract a fix must keep: N>=20 history including a
    table and a code block, latest message body visible on load, and a live message that follows."""
    table = "| step | tool |\n|---|---|\n| 1 | oven |\n| 2 | mixer |\n| 3 | pan |"
    code = "```python\n" + "\n".join(f"step_{i}()" for i in range(8)) + "\n```"
    seed = []
    for i in range(1, 26):
        if i % 5 == 0:
            body = f"reply {i} with a table:\n\n{table}"
        elif i % 7 == 0:
            body = f"reply {i} with code:\n\n{code}"
        else:
            body = f"reply {i} " * 20  # long enough to soft-wrap across several visual lines
        seed.append(message(i, "pm_1" if i % 2 else "human", "human" if i % 2 else "pm_1", body))
    client = FixtureClient(messages=seed)

    async def scenario():
        async with ChatTestApp(client).run_test(size=(80, 24)) as pilot:
            pane = pilot.app.query_one(ChatPane)
            thread = pane.query_one("#chat-thread", VerticalScroll)
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert thread.max_scroll_y > 0, "test needs overflow to be meaningful"
            assert len(list(thread.query(Markdown))) == len(seed)  # every message mounted
            assert thread.is_vertical_scroll_end  # the last message's body is the visible bottom

            await push_and_settle(pilot, pane, "message.new",
                                  message=message(26, "pm_1", "human", "a brand new reply"))
            assert len(list(thread.query(Markdown))) == len(seed) + 1
            assert thread.is_vertical_scroll_end  # the new message scrolled into view
    asyncio.run(scenario())


def test_removing_and_remounting_the_pane_replays_full_history():
    """#104: TroupeApp._layout_body's compact<->wide transition removes every pane from its
    container and mounts the *same instances* into a new one (body.remove_children(), then
    body.mount(...)) — not a recompose() of children in place. That's the exact lifecycle this
    simulates directly (out of the isolated ChatTestApp's control, so no real resize is available
    here): remove the widget, mount it again, and confirm ChatPane.compose() running fresh doesn't
    leave the new #chat-thread permanently empty."""
    seed = [message(1, "pm_1", "human", "first"), message(2, "human", "pm_1", "second"),
            message(3, "pm_1", "human", "third")]
    client = FixtureClient(messages=seed)

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            await pilot.pause()
            thread = pane.query_one("#chat-thread", VerticalScroll)
            assert len(list(thread.query(Markdown))) == len(seed)

            container = pane.parent
            await pane.remove()
            await container.mount(pane)
            await pilot.pause()

            new_thread = pane.query_one("#chat-thread", VerticalScroll)
            assert new_thread is not thread  # compose() really ran again, not reusing the old one
            assert len(list(new_thread.query(Markdown))) == len(seed)
    asyncio.run(scenario())


def test_sending_at_the_initial_layout_renders_a_you_bubble_once_the_engine_echoes_it():
    """#104 acceptance criterion 2 ("at the initial wide layout, sending a message renders a 'you'
    bubble immediately"). The real engine broadcasts every new chat message to every subscribed
    connection, including the sender's own (api.py ConnectionManager.publish has no self-exclusion),
    so the TUI's own send is expected to come back as a message.new push like any other — this
    simulates that echo explicitly, since FixtureClient (like the real API) doesn't auto-generate
    one just from a "chat" call, and confirms the render path handles it on the very first mount,
    before any resize has ever happened."""
    client = FixtureClient()

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            thread = pane.query_one("#chat-thread", VerticalScroll)
            composer = pane.query_one("#chat-composer", Composer)
            composer.focus()
            await pilot.pause()
            for ch in "hello pm":
                await pilot.press("space" if ch == " " else ch)
            await pilot.press("enter")
            await pilot.pause()
            assert len(client.messages) == 1  # the send landed
            sent = client.messages[0]
            assert sent["sender"] == "human"

            await push_and_settle(pilot, pane, "message.new", message=sent)
            labels = [row.query_one(Static).content for row in thread.query(".chat-message")]
            assert labels and "you" in labels[-1]
    asyncio.run(scenario())


def test_mount_message_queues_instead_of_crashing_on_an_unattached_thread():
    """#124: `Widget.mount()` raises `MountError` if the target isn't linked into the DOM yet
    (`is_attached` is False) — the exact condition between `compose()` returning a fresh
    `#chat-thread` and it actually being attached (at startup, or mid-remount during a resize).
    `_mount_message` must detect that itself and queue rather than ever attempt the mount."""
    pane = ChatPane(FixtureClient())
    thread = VerticalScroll(id="chat-thread")  # constructed but never mounted anywhere
    assert not thread.is_attached

    async def scenario():
        m = message(1, "pm_1", "human", "hello while detached")
        mounted = await pane._mount_message(thread, m)
        assert mounted is False
        assert pane._pending == [m]
        assert len(thread.children) == 0  # .mount() was never attempted

        # A second push of the same id while still detached must not queue a duplicate.
        again = await pane._mount_message(thread, m)
        assert again is False
        assert pane._pending == [m]
    asyncio.run(scenario())


def test_live_message_arriving_while_the_thread_is_detached_is_queued_and_shown_once_reattached():
    """#124 repro: TroupeApp._layout_body's compact<->wide transition removes every pane from its
    container before mounting it into the new one (body.remove_children(), then body.mount(...)) —
    the same remove()/mount() sequence test_removing_and_remounting_the_pane_replays_full_history
    simulates directly. A live message.new arriving in that window used to crash the whole app with
    MountError; it must instead be queued and appear exactly once, after the existing history, once
    the pane is genuinely reattached."""
    seed = [message(1, "pm_1", "human", "first"), message(2, "human", "pm_1", "second")]
    client = FixtureClient(messages=seed)

    async def scenario():
        async with ChatTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            await pilot.pause()
            thread = pane.query_one("#chat-thread", VerticalScroll)
            assert len(list(thread.query(Markdown))) == len(seed)

            container = pane.parent
            await pane.remove()
            assert not thread.is_attached  # the exact condition the fix guards against

            live = message(3, "pm_1", "human", "arrived while detached")
            push(pane, "message.new", message=live)  # must not raise MountError
            while list(pilot.app.workers):
                await pilot.app.workers.wait_for_complete()
            assert pane._pending == [live]  # queued, not lost, not mounted anywhere yet

            await container.mount(pane)
            await pilot.pause()

            new_thread = pane.query_one("#chat-thread", VerticalScroll)
            assert new_thread is not thread  # compose() really ran again
            assert pane._pending == []  # flushed by on_mount
            rows = list(new_thread.query(".chat-message"))
            assert len(rows) == len(seed) + 1  # shown exactly once, not dropped or duplicated
            who = [row.query_one(Static).content for row in rows]
            assert "pm_1" in who[-1]  # the live message landed last, i.e. in arrival order
    asyncio.run(scenario())
