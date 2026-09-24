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
app.py and on_troupe_event actually call, and it funnels through a ReloadCoalescer (below) that
wraps `_load()` in a try/except -- a dropped connection, an oversized response, a slow engine under
load, whatever -- so that one pane's failure can never take the rest of the TUI down with it
(app.py's gather has no return_exceptions=True; one uncaught exception used to cancel every other
pane's load and then the app itself).

QA's #108 soak review: a burst of live events (e.g. many task.* events during a startup dispatch
storm) used to schedule one full reload per event, stacking up overlapping in-flight client.call()s
until they piled up past the connection's 5s timeout -- and a failed reload wiped out the pane's
last-good content with a bare error, even though the data just fetched a moment earlier was still
perfectly fine to keep showing. ReloadCoalescer (below) collapses a burst into at most one in-flight
call plus one trailing one; Pane.load() preserves the last successful render on a *refresh* failure
(a small border_subtitle note instead), reserving the full "couldn't load" replacement for when
there's nothing good to show yet, and AutoRetrier retries on its own with backoff instead of making
the human press "r". ChatPane/FeedPane/NeedsYouPane duck-type this same load()/_load() shape (they
aren't Pane subclasses -- see their own modules) using the same two helpers directly."""
from __future__ import annotations

import asyncio

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Static

from .. import colors as C

AUTO_RETRY_MIN = 1.0
AUTO_RETRY_MAX = 30.0


class ReloadCoalescer:
    """At most one call to `fn` in flight, plus at most one trailing call queued behind it,
    debounced ~300ms -- a burst of N triggers while busy collapses into one more run, not N."""
    DEBOUNCE = 0.3

    def __init__(self, fn):
        self._fn = fn
        self._busy = False
        self._pending = False

    async def trigger(self) -> None:
        if self._busy:
            self._pending = True
            return
        self._busy = True
        try:
            await self._fn()
            while self._pending:
                self._pending = False
                await asyncio.sleep(self.DEBOUNCE)
                await self._fn()
        finally:
            self._busy = False


class AutoRetrier:
    """Schedules `load_fn()` again after an exponential backoff (1s, 2s, 4s, ... capped at 30s),
    resetting once a load succeeds -- a pane recovers on its own; the human never has to press "r"
    just to wait out a transient blip."""

    def __init__(self, load_fn):
        self._load_fn = load_fn
        self._backoff = 0.0
        self._task: asyncio.Task | None = None

    def schedule(self) -> None:
        if self._task is not None and not self._task.done():
            return  # one already scheduled -- no need to pile more up
        self._backoff = min(self._backoff * 2, AUTO_RETRY_MAX) if self._backoff else AUTO_RETRY_MIN

        async def retry_later() -> None:
            await asyncio.sleep(self._backoff)
            await self._load_fn()

        self._task = asyncio.ensure_future(retry_later())

    def reset(self) -> None:
        self._backoff = 0.0


class Pane(Static):
    PANE_TITLE: str = ""  # shown as this pane's tab title in the 80x24 collapsed layout
    can_focus = True
    BINDINGS = [Binding("r", "retry", "Retry", show=False)]

    def __init__(self, client, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        self._loaded_ok = False
        self._coalescer = ReloadCoalescer(self._attempt_load)
        self._retrier = AutoRetrier(self.load)

    async def load(self) -> None:
        await self._coalescer.trigger()

    async def _attempt_load(self) -> None:
        try:
            await self._load()
        except Exception as e:
            reason = str(e) or type(e).__name__
            if self._loaded_ok:
                # A refresh failed, but what's on screen is still good -- QA: overwriting a working
                # table with a bare error on every transient blip (a timeout under load, a brief
                # disconnect) was worse than the bug it was meant to report.
                self.border_subtitle = f"couldn't refresh: {reason} (r)"
            else:
                self.update(Text(f"couldn't load: {reason} (r to retry)", style=C.RED))
            self._retrier.schedule()
            return
        self._loaded_ok = True
        self._retrier.reset()
        self.border_subtitle = ""

    async def _load(self) -> None:
        """Fetch and render this pane's snapshot. Called (via load()) on mount and after a resync."""

    async def action_retry(self) -> None:
        await self.load()

    def on_troupe_event(self, event: dict) -> None:
        """Handle one pushed {"event": str, "seq": int, "data": dict}. Override to react live."""
