"""Comms feed pane (#69): agent-to-agent mail and decisions, newest at the bottom, filterable by
agent, FYI vs action, and decisions-only (REQ-TUI-010's Comms bullet).

Not a `Pane(Static)` like Team/Tasks: "sticky to the bottom" (REQ-GUI-017) is exactly what
`RichLog` is for (append-only, auto-scrolls unless the human has scrolled up), so this composes one
instead of re-rendering a whole Static each time. It still satisfies the Pane duck-type app.py
needs — `__init__(client)`, `PANE_TITLE`, `async load()`, `on_troupe_event(event)` — just as a
`Container` rather than a `Static` subclass.
"""
from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import RichLog

from .. import colors as C

MAX_ITEMS = 300  # kept in memory per source; RichLog itself is unbounded scrollback


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _short(s: str, n: int = 100) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class FeedPane(Container):
    PANE_TITLE = "Comms"
    border_title = "COMMS"
    can_focus = True

    DEFAULT_CSS = """
    FeedPane { border: round $panel-darken-1; border-title-align: left; height: 1fr; }
    FeedPane:focus { border: round $accent; }
    FeedPane > RichLog { height: 1fr; background: transparent; }
    """

    BINDINGS = [
        Binding("f", "cycle_fyi", "FYI filter", show=True),
        Binding("d", "toggle_decisions", "Decisions only", show=True),
        Binding("[", "prev_agent", "Prev agent filter", show=False),
        Binding("]", "next_agent", "Next agent filter", show=False),
        Binding("r", "retry", "Retry", show=False),
    ]

    def __init__(self, client: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.client = client
        self._agents_by_id: dict[str, dict] = {}
        self._messages: list[dict] = []
        self._decisions: list[dict] = []
        self._agent_filter: str | None = None  # None = all
        self._fyi_filter: bool | None = None  # None = all, True = FYI only, False = action only
        self._decisions_only = False

    def compose(self) -> ComposeResult:
        yield RichLog(id="feed-log", auto_scroll=True, markup=False, wrap=True, max_lines=2000)

    # ── loading + live updates ───────────────────────────────────────────
    async def load(self) -> None:
        # #108: see chat.py's load() for why this can no longer let errors propagate.
        try:
            agents = await self.client.call("agents")
            self._agents_by_id = {a["id"]: a for a in agents["items"]}
            messages = await self.client.call("messages", kind="msg", limit=MAX_ITEMS)
            self._messages = list(reversed(messages["items"]))  # API returns newest-first
            decisions = await self.client.call("memories", kind="decision", limit=MAX_ITEMS)
            self._decisions = list(reversed(decisions["items"]))
        except Exception as e:
            self.query_one(RichLog).clear()
            self.query_one(RichLog).write(f"couldn't load: {e or type(e).__name__} (r to retry)")
            return
        self._repaint()

    async def action_retry(self) -> None:
        await self.load()

    def on_troupe_event(self, event: dict) -> None:
        name = event["event"]
        if name == "message.new":
            m = event["data"]["message"]
            if m["kind"] == "msg":
                self._messages = (self._messages + [m])[-MAX_ITEMS:]
                self._repaint()
        elif name in ("memory.new", "memory.changed"):
            mem = event["data"]["memory"]
            if mem.get("kind") == "decision" and not mem.get("deleted"):
                self._decisions = [d for d in self._decisions if d["id"] != mem["id"]] + [mem]
                self._decisions = self._decisions[-MAX_ITEMS:]
                self._repaint()
        elif name.startswith("agent."):
            self.app.call_later(self._refresh_agents)

    async def _refresh_agents(self) -> None:
        try:
            agents = await self.client.call("agents")
        except Exception:
            return  # a transient failure here just means stale handles until the next event/retry
        self._agents_by_id = {a["id"]: a for a in agents["items"]}
        self._repaint()

    # ── identity ─────────────────────────────────────────────────────────
    def _handle(self, agent_id: str) -> tuple[str, str]:
        a = self._agents_by_id.get(agent_id)
        if a:
            return a["handle"], C.role_color(a["role"])
        if agent_id == "human":
            return "you", C.TEXT
        return agent_id, C.TEXT_DIM

    def _agent_ids(self) -> list[str]:
        ids = {m["sender"] for m in self._messages} | {m["recipient"] for m in self._messages}
        ids |= {d["agent"] for d in self._decisions}
        ids.discard("human")
        return sorted(ids)

    # ── filters ──────────────────────────────────────────────────────────
    def action_cycle_fyi(self) -> None:
        self._fyi_filter = {None: True, True: False, False: None}[self._fyi_filter]
        self._repaint()

    def action_toggle_decisions(self) -> None:
        self._decisions_only = not self._decisions_only
        self._repaint()

    def action_prev_agent(self) -> None:
        self._step_agent_filter(-1)

    def action_next_agent(self) -> None:
        self._step_agent_filter(1)

    def _step_agent_filter(self, step: int) -> None:
        choices: list[str | None] = [None, *self._agent_ids()]
        i = choices.index(self._agent_filter) if self._agent_filter in choices else 0
        self._agent_filter = choices[(i + step) % len(choices)]
        self._repaint()

    def _matches(self, kind: str, item: dict) -> bool:
        if kind == "message":
            if self._decisions_only:
                return False
            if self._agent_filter and self._agent_filter not in (item["sender"], item["recipient"]):
                return False
            if self._fyi_filter is True and not item.get("fyi"):
                return False
            if self._fyi_filter is False and item.get("fyi"):
                return False
            return True
        if self._agent_filter and self._agent_filter != item.get("agent"):
            return False
        return True

    # ── rendering ────────────────────────────────────────────────────────
    def _message_line(self, m: dict) -> Text:
        sender_text, sender_color = self._handle(m["sender"])
        recipient_text, recipient_color = self._handle(m["recipient"])
        line = Text()
        line.append(_clock(m["ts"]) + "  ", style=C.TEXT_FAINT)
        line.append(sender_text, style=sender_color)
        line.append(" → ")
        line.append(recipient_text, style=recipient_color)
        if m.get("fyi"):
            line.append("  FYI", style=C.TEXT_FAINT)
        line.append("  " + _short(m.get("subject") or m.get("body", "")))
        return line

    def _decision_line(self, d: dict) -> Text:
        agent_text, agent_color = self._handle(d["agent"])
        line = Text()
        line.append(_clock(d["ts"]) + "  ", style=C.TEXT_FAINT)
        line.append(agent_text, style=agent_color)
        line.append("  ")
        line.append("DECISION", style=f"bold {C.ACCENT}")
        line.append("  " + _short(d["title"]))
        return line

    def _update_border_title(self) -> None:
        parts = []
        if self._agent_filter:
            handle, _ = self._handle(self._agent_filter)
            parts.append(f"agent:{handle}")
        if self._fyi_filter is True:
            parts.append("FYI only")
        elif self._fyi_filter is False:
            parts.append("action only")
        if self._decisions_only:
            parts.append("decisions only")
        self.border_title = "COMMS" + (" · " + " · ".join(parts) if parts else "")

    def _repaint(self) -> None:
        log = self.query_one("#feed-log", RichLog)
        log.clear()
        items = [(m["ts"], "message", m) for m in self._messages if self._matches("message", m)]
        items += [(d["ts"], "decision", d) for d in self._decisions if self._matches("decision", d)]
        items.sort(key=lambda row: row[0])
        for _, kind, item in items:
            log.write(self._message_line(item) if kind == "message" else self._decision_line(item))
        if not items:
            log.write(Text("Nothing yet.", style=C.TEXT_FAINT))
        self._update_border_title()
