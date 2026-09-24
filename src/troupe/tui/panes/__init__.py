"""Pane registration API (documented for slices B-D, #67/#68/#69).

A pane is a `textual.widgets.Static` subclass constructed as `Pane(client)`:
    class NeedsYouPane(Pane):
        async def _load(self) -> None:
            data = await self.client.call("questions", status="open")
            self._paint(data["items"])

        def on_troupe_event(self, event: dict) -> None:
            if event["event"].startswith("question."):
                self.app.call_later(self.load)

The app calls `load()` once on mount and again after every resync, and forwards every pushed
stream event to `on_troupe_event()` — filter on `event["event"]` for the topics you care about.
To add a pane: write the module, then add one `(name, YourPane)` line to `tui/app.py`'s `PANES`.

Write your fetch-and-render logic in `_load()`, not `load()` (#108): `load()` is the entry point
app.py actually calls (on mount, and gathered together with every other pane's on a resync), and
it wraps `_load()` in a try/except -- a dropped connection, an oversized response, a slow engine
under load, whatever -- so that one pane's failure renders inline ("couldn't load: ...") instead of
taking the rest of the TUI down with it (app.py's gather has no return_exceptions=True; one
uncaught exception used to cancel every other pane's load and then the app itself)."""
from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Static

from .. import colors as C


class Pane(Static):
    PANE_TITLE: str = ""  # shown as this pane's tab title in the 80x24 collapsed layout
    can_focus = True
    BINDINGS = [Binding("r", "retry", "Retry", show=False)]

    def __init__(self, client, **kwargs):
        super().__init__(**kwargs)
        self.client = client

    async def load(self) -> None:
        try:
            await self._load()
        except Exception as e:
            self.update(Text(f"couldn't load: {e or type(e).__name__} (r to retry)", style=C.RED))

    async def _load(self) -> None:
        """Fetch and render this pane's snapshot. Called (via load()) on mount and after a resync."""

    async def action_retry(self) -> None:
        await self.load()

    def on_troupe_event(self, event: dict) -> None:
        """Handle one pushed {"event": str, "seq": int, "data": dict}. Override to react live."""
