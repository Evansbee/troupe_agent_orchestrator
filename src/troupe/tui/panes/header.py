"""Header pane (REQ-TUI-010): project, engine state, milestone progress, provider usage meters.
Shows a clear offline state instead of hanging when the engine is disconnected or dead — the
other half of REQ-TUI-002's "no hanging" alongside client.py's hard call timeouts."""
from __future__ import annotations

import time

from rich.text import Text

from .. import colors as C
from . import Pane

ENGINE_LABEL = {
    "live": ("engine live", C.GREEN),
    "paused": ("paused", C.YELLOW),
    "throttled": ("throttled", C.ORANGE),
    "reloading": ("reloading…", C.CYAN),
    "stopped": ("stopped", C.RED),
}


def usage_bar(pct: float, cap_pct: float | None) -> Text:
    color = C.RED if pct >= 90 else C.ORANGE if pct >= 70 else C.GREEN
    label = f"{pct:.0f}%"
    if cap_pct is not None and pct >= cap_pct:
        color = C.RED  # over the cap threshold — the "capped until HH:MM" badge gives the detail
    return Text(label, style=color)


class HeaderPane(Pane):
    DEFAULT_CSS = """
    HeaderPane { border: round $panel-darken-1; height: auto; padding: 0 1; }
    """

    def __init__(self, client, project: str, **kwargs):
        super().__init__(client, **kwargs)
        self.project = project
        self._engine: dict | None = None
        self._usage: dict | None = None
        self._milestones: list[dict] = []

    def on_mount(self) -> None:
        self._paint()  # widgets can't update() before they're mounted

    async def _load(self) -> None:
        self._engine = await self.client.call("engine")
        self._usage = await self.client.call("usage")
        self._milestones = (await self.client.call("milestones"))["items"]
        self._paint()

    def on_troupe_event(self, event: dict) -> None:
        if event["event"].startswith(("engine.", "usage.", "milestone.")):
            self.app.call_later(self.load)

    def set_offline(self, reason: str = "Engine offline") -> None:
        self._engine = None
        self._paint(offline_reason=reason)

    def _paint(self, offline_reason: str | None = None) -> None:
        text = Text()
        text.append(self.project, style="bold " + C.TEXT)
        text.append("  ·  ")
        if offline_reason is not None or self._engine is None:
            text.append(offline_reason or "Engine offline", style="bold " + C.RED)
        else:
            label, color = ENGINE_LABEL.get(self._engine["state"], (self._engine["state"], C.TEXT_DIM))
            text.append(label, style=color)
        active = next((m for m in self._milestones if m["status"] == "active"), None)
        if active:
            text.append("  ·  ")
            text.append(f"{active['name']} {active['done']}/{active['total']}", style=C.TEXT_DIM)
        if self._usage:
            claude = next((p for p in self._usage["providers"] if p["provider"] == "claude"), None)
            for window in (claude or {}).get("windows", []):
                text.append("  ·  ")
                text.append(f"claude {window['name']} ", style=C.TEXT_DIM)
                text.append_text(usage_bar(window["used_pct"], window.get("cap_pct")))
            if claude and claude.get("capped") and claude.get("limited_until"):
                text.append("  ·  ")
                until = time.strftime("%H:%M", time.localtime(claude["limited_until"]))
                text.append(f"capped until {until}", style="bold " + C.RED)
        self.update(text)
