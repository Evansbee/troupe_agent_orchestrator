"""TUI slice C (#68): chat with the project's PM. specs/45-tui.md REQ-TUI-021, design/tui.md "Chat pane".

Built against the shared pane interface (#66's note): ``Pane(client)`` with ``async load()`` and
``on_troupe_event(event)``. The PM is always the default and only partner in this MVP cut
(REQ-COM-029); ``:chat <handle>`` for other agents is out of scope here.
"""
from __future__ import annotations

import asyncio
from typing import Any

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Markdown, Static, TextArea

from .. import colors as C
from . import AutoRetrier, ReloadCoalescer

HUMAN_COLOR = C.ACCENT
DIM_COLOR = C.TEXT_DIM


class Composer(TextArea):
    """A multiline composer: plain Enter sends, Shift+Enter inserts a newline (spec REQ-TUI-021)."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            # Stays in the box until ChatPane confirms the send actually went through — clearing
            # here and only here meant a failed/undeliverable send silently lost the human's text
            # (QA #68 rejection).
            text = self.text
            if text.strip():
                self.post_message(self.Submitted(text))
            return
        if event.key == "shift+enter":
            # Not every terminal/multiplexer reports shift+enter as distinct from enter (needs the
            # Kitty keyboard protocol or similar); where it isn't, this branch is simply unreachable
            # and plain enter above inserts the newline instead. Best effort, matches every other
            # terminal chat composer's constraint here.
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return
        await super()._on_key(event)


class ChatPane(Widget):
    """Chat with the PM: composer, streaming "…is working" line, markdown thread, sticky-to-bottom."""

    PANE_TITLE = "Chat"  # matches TeamPane/TasksPane's convention for the future compact-tabs order

    DEFAULT_CSS = f"""
    ChatPane {{
        layout: vertical;
        height: 1fr;
        border: round {C.BORDER};
        border-title-align: left;
    }}
    ChatPane:focus-within {{
        border: round {C.ACCENT};
    }}
    ChatPane > #chat-thread {{
        height: 1fr;
        padding: 0 1;
    }}
    ChatPane > #chat-working {{
        height: auto;
        padding: 0 2;
    }}
    ChatPane > Composer {{
        height: auto;
        max-height: 6;
        margin: 0 1 1 1;
        border: round {C.BORDER};
    }}
    .chat-message {{
        height: auto;
        margin-bottom: 1;
    }}
    """

    def __init__(self, client: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.client = client
        self.pm_id: str | None = None
        self.pm_name = "PM"
        self.pm_color = C.role_color("pm")
        self._pm_running = False
        self._pm_activity = ""
        self._last_sender: str | None = None
        self._working_text = ""
        self._seen_ids: set[int] = set()
        self._sending = False
        # Every message ever mounted, oldest-first (#104): a compact<->wide layout transition tears
        # down and rebuilds every pane's DOM (TroupeApp._layout_body's remove_children()+mount()), so
        # ChatPane.compose() runs again and hands back a brand new, empty #chat-thread. `load()` only
        # fetches and mounts history once; without a local copy, a resize left the thread permanently
        # empty even though every message was still in the store. `on_mount` below replays this list
        # into whatever #chat-thread it's handed, so it works the same on the very first mount (where
        # this is still empty — load() fills both the thread and this together) and on every remount.
        self._history: list[dict] = []
        self._coalescer = ReloadCoalescer(self._attempt_load)
        self._retrier = AutoRetrier(self.load)

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="chat-thread")
        yield Static("", id="chat-working")
        yield Composer(
            id="chat-composer", tab_behavior="focus", soft_wrap=True, show_line_numbers=False,
            placeholder=f"Message {self.pm_name}…  Enter to send · Shift+Enter for a new line",
        )

    async def on_mount(self) -> None:
        """(#104) Fires after every `compose()`, including a compact<->wide remount, not just the
        very first mount. On the very first mount `self._history` is still empty (`load()` hasn't
        run yet — it's awaited separately, after the initial layout, by TroupeApp), so this is a
        no-op then and `load()` populates the thread as before. On a remount, `self._history` is
        already populated from the *previous* mount's `load()`/live messages, so this replays it
        into the fresh (otherwise permanently empty) #chat-thread `compose()` just handed back."""
        if not self._history:
            return
        thread = self.query_one("#chat-thread", VerticalScroll)
        self._seen_ids = set()  # the new thread is genuinely empty; forget the old one's dedup state
        for m in self._history:
            await self._mount_message(thread, m)
        self._scroll_to_end(thread)

    # ── shared pane interface ────────────────────────────────────────────
    async def load(self) -> None:
        # #108: coalesced (at most one in-flight reload plus one trailing) and auto-retried with
        # backoff on failure -- see panes/__init__.py's Pane.load(). A failure here only ever
        # touches the small #chat-working line, never the mounted #chat-thread history, so there's
        # nothing to preserve/replace the way Tasks/Team's single-Static render needed.
        await self._coalescer.trigger()

    async def _attempt_load(self) -> None:
        # #108: a load failure here (an oversized response, a timeout under load, a dropped
        # connection) must not propagate -- app.py gathers every pane's load() together, so one
        # uncaught exception used to take the whole TUI down with it. This used to deliberately let
        # errors propagate on the theory that the orchestrator was better placed to show one
        # unified "Engine offline" state, but the orchestrator never actually caught anything past
        # the initial connect() -- an error from here (or any other pane) reached the app itself.
        try:
            agents = await self.client.call("agents", timeout=5.0)
            pm = next((a for a in agents.get("items", []) if a.get("role") == "pm"), None)
            if pm is None:
                return
            self.pm_id = pm["id"]
            self.pm_name = pm.get("name") or pm["id"]
            self.border_title = f"CHAT — {self.pm_id}"
            composer = self.query_one("#chat-composer", Composer)
            composer.placeholder = f"Message {self.pm_name}…  Enter to send · Shift+Enter for a new line"
            result = await self.client.call("messages", timeout=5.0, chat_with=self.pm_id, limit=100)
            thread = self.query_one("#chat-thread", VerticalScroll)
            for m in reversed(result.get("items", [])):
                await self._mount_message(thread, m)
                self._history.append(m)
                self._last_sender = m.get("sender")
            self._scroll_to_end(thread)
            self._pm_running = pm.get("state") == "running"
            self._pm_activity = pm.get("activity") or ""
        except Exception as e:
            self.query_one("#chat-working", Static).update(f"couldn't load: {str(e) or type(e).__name__}")
            self._retrier.schedule()
            return
        self._retrier.reset()
        self._refresh_working()

    def on_troupe_event(self, event: dict) -> None:
        kind = event.get("event")
        data = event.get("data") or {}
        if kind == "message.new":
            m = data.get("message") or {}
            if self._is_ours(m):
                self.run_worker(self._handle_new_message(m), exclusive=False)
        elif kind == "agent.state":
            a = data.get("agent") or {}
            if a.get("id") == self.pm_id:
                self._pm_running = a.get("state") == "running"
                self._pm_activity = a.get("activity") or ""
                self._refresh_working()

    # ── internals ─────────────────────────────────────────────────────────
    def _is_ours(self, m: dict) -> bool:
        if not self.pm_id or m.get("kind") != "chat":
            return False
        sender, recipient = m.get("sender"), m.get("recipient")
        return (sender == "human" and recipient == self.pm_id) or (sender == self.pm_id and recipient == "human")

    async def _handle_new_message(self, m: dict) -> None:
        thread = self.query_one("#chat-thread", VerticalScroll)
        was_at_bottom = thread.is_vertical_scroll_end
        mounted = await self._mount_message(thread, m)
        if mounted:
            self._history.append(m)
        if mounted and was_at_bottom:
            self._scroll_to_end(thread)
        self._last_sender = m.get("sender")
        self._refresh_working()

    def _scroll_to_end(self, thread: VerticalScroll) -> None:
        """Wait for the just-mounted Markdown's layout to land in the full pane before scrolling."""
        self.run_worker(self._settle_scroll(thread), exclusive=False)

    async def _settle_scroll(self, thread: VerticalScroll) -> None:
        # `thread.wait_for_refresh()` resolves once this node's own message queue drains, which is
        # not the same as "the compositor has computed this content's real height" (#85 live repro:
        # max_scroll_y read back as a flat 0/0 for several such "refreshes" in a row — read as
        # converged — while the mounted Markdown's virtual_size was still Size(0, 0); the view then
        # never caught up once layout did land, since nothing rescrolled after this worker exited).
        # Polling on a real clock instead gives the compositor's own paint cycle room to actually
        # run between checks, which is what a passing live repro needed in practice.
        #
        # Two things had to be true at once, not just one (#104 merge-gate flake, found by direct
        # instrumentation): (1) layout can grow in more than one wave — max_scroll_y can hold still
        # long enough to look converged and then grow *again* later (observed live: 103 -> 105,
        # pause, -> 107) — a single scroll_end() call after declaring victory once just missed the
        # second wave, since nothing was watching anymore once this worker had already exited; (2)
        # scroll_end()'s effect on scroll_y isn't instant either. So this now re-snaps to the
        # bottom on *every* poll, for as long as it keeps polling, and only stops once max_scroll_y
        # has stopped changing *and* the thread is actually sitting at the bottom right now, both
        # true continuously for the whole stability window — any later growth wave un-sticks the
        # window and starts it over, so it can't be fooled by an early plateau the way a one-shot
        # "call scroll_end() once, after N stable samples" could be. This is a background worker
        # with no user-facing latency cost either way — the rest of the UI stays responsive
        # throughout — so a generous ceiling here is free.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 8.0  # generous ceiling even under heavy contention; converges in well under this normally
        last_max: int | None = None
        stable_since: float | None = None
        while loop.time() < deadline:
            await asyncio.sleep(0.02)
            thread.scroll_end(animate=False)
            now = loop.time()
            current_max = thread.max_scroll_y
            settled = current_max == last_max and thread.is_vertical_scroll_end
            last_max = current_max
            if not settled:
                stable_since = None
            elif stable_since is None:
                stable_since = now
            elif now - stable_since >= 0.3:
                break

    async def _mount_message(self, thread: VerticalScroll, m: dict) -> bool:
        mid = m.get("id")
        if mid is not None:
            if mid in self._seen_ids:
                return False
            self._seen_ids.add(mid)
        human = m.get("sender") == "human"
        who = "you" if human else self.pm_name
        color = HUMAN_COLOR if human else self.pm_color
        # A message must never render as a bare sender label (#85): fall back to subject, then to
        # an explicit placeholder, so an unexpected empty body is visibly a message, not nothing.
        body = m.get("body") or m.get("subject") or "*(empty message)*"
        markdown = Markdown()
        row = Vertical(
            Static(f"[bold {color}]{escape(who)}[/]"),
            markdown,
            classes="chat-message",
        )
        await thread.mount(row)
        # Markdown parses and mounts its own block widgets (headings, paragraphs, tables, ...)
        # asynchronously from `_on_mount`, not synchronously during construction/mount — passing
        # `body` to the constructor and trusting that internal call left `_settle_scroll` racing
        # against content that didn't exist yet (#85). `update()` is Textual's own documented
        # await-to-ensure-mounted signal; calling it here ourselves, on an initially-empty Markdown,
        # makes this message's content unconditionally real before the caller moves on.
        await markdown.update(body)
        return True

    def _refresh_working(self) -> None:
        working = self._pm_running and self._last_sender == "human"
        if working:
            suffix = f"… {escape(self._pm_activity)}" if self._pm_activity else "…"
            text = f"[{DIM_COLOR}]{escape(self.pm_name)} is working{suffix}[/]"
        else:
            text = ""
        if text == self._working_text:
            return
        self._working_text = text
        self.query_one("#chat-working", Static).update(text)

    def on_composer_submitted(self, message: Composer.Submitted) -> None:
        message.stop()
        text = message.text.strip()
        if not text or self._sending:
            return
        if not self.pm_id:
            self.notify("No PM to chat with yet", severity="warning", timeout=5)
            return
        self._sending = True
        self.run_worker(self._send(text), exclusive=False)

    async def _send(self, text: str) -> None:
        composer = self.query_one("#chat-composer", Composer)
        try:
            await self.client.call("chat", timeout=5.0, agent=self.pm_id, text=text)
        except Exception:
            self.notify("Not sent — engine offline; your message is still in the box",
                        severity="error", timeout=5)
            # Restore only if the box is still empty: the human may already be typing something
            # else, and stomping on that would just trade one kind of message loss for another.
            if not composer.text.strip():
                composer.text = text
        else:
            composer.clear()
        finally:
            self._sending = False
