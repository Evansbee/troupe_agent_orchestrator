"""Pane registration API (documented for slices B-D, #67/#68/#69).

A pane is a `textual.widgets.Static` subclass constructed as `Pane(client)`:
    class NeedsYouPane(Pane):
        async def load(self) -> None:
            data = await self.client.call("questions", status="open")
            self._paint(data["items"])

        def on_troupe_event(self, event: dict) -> None:
            if event["event"].startswith("question."):
                self.app.call_later(self.load)

The app calls `load()` once on mount and again after every resync, and forwards every pushed
stream event to `on_troupe_event()` — filter on `event["event"]` for the topics you care about.
To add a pane: write the module, then add one `(name, YourPane)` line to `tui/app.py`'s `PANES`.
"""
from __future__ import annotations

from textual.widgets import Static


class Pane(Static):
    PANE_TITLE: str = ""  # shown as this pane's tab title in the 80x24 collapsed layout

    def __init__(self, client, **kwargs):
        super().__init__(**kwargs)
        self.client = client

    async def load(self) -> None:
        """Fetch and render this pane's snapshot. Called on mount and after a resync."""

    def on_troupe_event(self, event: dict) -> None:
        """Handle one pushed {"event": str, "seq": int, "data": dict}. Override to react live."""
