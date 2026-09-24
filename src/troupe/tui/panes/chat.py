"""TUI slice C (#68): chat with the project's PM. specs/45-tui.md REQ-TUI-021, design/tui.md "Chat pane".

Built against the shared pane interface (#66's note): ``Pane(client)`` with ``async load()`` and
``on_troupe_event(event)``. The PM is always the default and only partner in this MVP cut
(REQ-COM-029); ``:chat <handle>`` for other agents is out of scope here.
"""
from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Markdown, Static, TextArea

from .. import colors as C

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

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="chat-thread")
        yield Static("", id="chat-working")
        yield Composer(
            id="chat-composer", tab_behavior="focus", soft_wrap=True, show_line_numbers=False,
            placeholder=f"Message {self.pm_name}…  Enter to send · Shift+Enter for a new line",
        )

    # ── shared pane interface ────────────────────────────────────────────
    # load() lets client errors (offline engine, timeout) propagate — the orchestrator awaits every
    # pane's load() together and is better placed to show one "Engine offline" state than five panes
    # each reporting it separately. _send() below is a fire-and-forget user action with nowhere else
    # to surface a failure, so it catches and notifies locally instead.
    async def load(self) -> None:
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
            self._last_sender = m.get("sender")
        thread.scroll_end(animate=False)
        self._pm_running = pm.get("state") == "running"
        self._pm_activity = pm.get("activity") or ""
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
        if mounted and was_at_bottom:
            thread.scroll_end(animate=False)
        self._last_sender = m.get("sender")
        self._refresh_working()

    async def _mount_message(self, thread: VerticalScroll, m: dict) -> bool:
        mid = m.get("id")
        if mid is not None:
            if mid in self._seen_ids:
                return False
            self._seen_ids.add(mid)
        human = m.get("sender") == "human"
        who = "you" if human else self.pm_name
        color = HUMAN_COLOR if human else self.pm_color
        row = Vertical(
            Static(f"[bold {color}]{escape(who)}[/]"),
            Markdown(m.get("body", "")),
            classes="chat-message",
        )
        await thread.mount(row)
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
