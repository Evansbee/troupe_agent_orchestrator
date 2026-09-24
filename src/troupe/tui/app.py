"""The per-project Textual TUI shell (REQ-TUI-001/002/003/010/011/020/030)."""
from __future__ import annotations

import asyncio
import os
import signal

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Label, TabbedContent, TabPane

from .. import config as config_mod
from .client import TuiClient
from .lifecycle import ensure_engine, restart_engine, stop_owned_engine
from .panes.chat import ChatPane, Composer
from .panes.header import HeaderPane
from .panes.needs_you import NeedsYouPane
from .panes.tasks import TasksPane
from .panes.team import TeamPane

# Pane registries — see panes/__init__.py for the Pane API. LEFT_PANES sit under the header on
# the left (Team/Tasks); RIGHT_PANES sit on the right (Needs-you, then Chat, then Comms once #69
# lands — one line each to add here, per design/tui.md's pane-priority order).
LEFT_PANES: list[type] = [TeamPane, TasksPane]
RIGHT_PANES: list[type] = [NeedsYouPane, ChatPane]

# REQ-TUI-011: below this width or height, panes collapse from side-by-side to tabs.
COMPACT_WIDTH = 80
COMPACT_HEIGHT = 24


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: 60; height: auto; border: round $warning; padding: 1 2; background: $panel; }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.prompt)
            yield Label("y = yes, any other key = no", classes="hint")

    def on_key(self, event) -> None:
        self.dismiss(event.key == "y")


class TroupeApp(App):
    CSS = """
    #body { height: 1fr; }
    #body-row { height: 1fr; }
    #left { width: 34%; height: 1fr; }
    #right { width: 1fr; height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        Binding("s", "stop_everything", "Stop"),
        Binding("r", "restart_engine_action", "Restart"),
        Binding("/", "focus_chat", "Chat"),
        Binding("tab", "focus_next", "Next pane", show=False),
        Binding("shift+tab", "focus_previous", "Prev pane", show=False),
    ]

    def __init__(self, cfg: config_mod.Config, *, owns_engine: bool):
        super().__init__()
        self.cfg = cfg
        self.owns_engine = owns_engine
        self.client = TuiClient(cfg.root)
        self.title = cfg.project
        self._events_task: asyncio.Task | None = None
        self._panes: list = []  # left column: Team, Tasks
        self._right_panes: list = []  # right column: Needs-you, (Comms/Chat once #68/#69 land)
        self._header: HeaderPane | None = None
        self._compact: bool | None = None  # unknown until first layout, forcing an initial build

    @property
    def _all_panes(self) -> list:
        """design/tui.md's pane priority for the collapsed 80x24 tab order: Needs-you first (when
        present), then the left column (Team, Tasks), then the rest of the right column in its own
        stacking order (Chat now; Comms would slot in before Chat once #69 lands, since the right
        column's own order is Needs-you, Comms, Chat top-to-bottom)."""
        if not self._right_panes:
            return list(self._panes)
        return [self._right_panes[0], *self._panes, *self._right_panes[1:]]

    def compose(self) -> ComposeResult:
        self._header = HeaderPane(self.client, self.cfg.project)
        yield self._header
        self._panes = [cls(self.client) for cls in LEFT_PANES]
        self._right_panes = [cls(self.client) for cls in RIGHT_PANES]
        yield Container(id="body")
        yield Footer()

    def _is_compact(self, size=None) -> bool:
        size = size if size is not None else self.size
        return size.width <= COMPACT_WIDTH or size.height <= COMPACT_HEIGHT

    async def _layout_body(self, size=None) -> None:
        """REQ-TUI-011: side-by-side panes normally, tabs at 80x24. Same pane instances (and
        their loaded state) move between layouts — nothing gets re-fetched on a resize.

        `size` is the terminal size to lay out for. On a live resize, `self.size` during the
        `Resize` event still holds the *previous* size (Textual updates it after dispatching the
        event), so on_resize passes `event.size` explicitly instead of trusting `self.size` — the
        one place besides on_mount that calls this without an explicit size, where `self.size`
        is already current."""
        compact = self._is_compact(size)
        if compact == self._compact:
            return
        composer_was_focused = isinstance(self.focused, Composer)
        self._compact = compact
        body = self.query_one("#body", Container)
        await body.remove_children()
        if compact:
            tabs = TabbedContent()
            await body.mount(tabs)
            for pane in self._all_panes:
                title = getattr(pane, "PANE_TITLE", "") or pane.__class__.__name__
                await tabs.add_pane(TabPane(title, pane, id=f"tab-{pane.__class__.__name__}"))
        else:
            left = Vertical(*self._panes, id="left")
            right = Vertical(*self._right_panes, id="right")
            await body.mount(Horizontal(left, right, id="body-row"))
        # A layout rebuild (resize) tears down and remounts every pane, dropping whatever had
        # focus — restore it to somewhere sensible rather than leaving focus on nothing, which
        # would route plain typing straight into app-level bindings again (#83).
        if composer_was_focused:
            self._focus_chat_composer()

    def _chat_pane(self) -> ChatPane | None:
        return next((p for p in self._right_panes if isinstance(p, ChatPane)), None)

    def _focus_chat_composer(self) -> None:
        """REQ-TUI-020: `/` (and startup) focuses the PM chat composer. In 80x24 tabs mode the
        composer lives in a hidden tab, so switch to it first — focusing an off-screen widget
        would otherwise silently do nothing useful for the human."""
        chat = self._chat_pane()
        if chat is None:
            return
        tabs = self.query(TabbedContent)
        if tabs:
            tabs.first().active = f"tab-{ChatPane.__name__}"
        composer = chat.query("#chat-composer")
        if composer:
            self.set_focus(composer.first())

    def action_focus_chat(self) -> None:
        self._focus_chat_composer()

    async def on_resize(self, event) -> None:
        await self._layout_body(event.size)

    async def on_mount(self) -> None:
        self._install_signal_handlers()
        await self._layout_body()
        self._focus_chat_composer()
        await self._connect_and_load()
        self._events_task = asyncio.create_task(self._pump_events())
        self.set_interval(2.0, self._refresh_connection_state)
        shot = os.environ.get("TROUPE_SHOT")
        if shot:
            self._snapshot_and_exit(shot)

    async def on_unmount(self) -> None:
        """Always close the client connection on app exit — an open connection left behind (in
        tests, a fixture server's asyncio.start_unix_server) never sees EOF and hangs forever
        waiting for it to close, since Server.wait_closed() waits for every live handler."""
        if self._events_task is not None:
            self._events_task.cancel()
        await self.client.close()

    async def _connect_and_load(self) -> None:
        try:
            await self.client.connect()
        except (OSError, asyncio.TimeoutError, ConnectionError):
            if self._header:
                self._header.set_offline()
            return
        await asyncio.gather(self._header.load(), *(p.load() for p in self._all_panes))

    def _refresh_connection_state(self) -> None:
        if self._header and not self.client.connected:
            self._header.set_offline()

    async def _pump_events(self) -> None:
        async for event in self.client.events():
            if self._header:
                self._header.on_troupe_event(event)
            for pane in self._all_panes:
                pane.on_troupe_event(event)

    @work(exclusive=True)
    async def _snapshot_and_exit(self, path: str) -> None:
        await asyncio.sleep(0.2)  # let the initial load()s render
        self.save_screenshot(path)
        self.exit()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGHUP, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.ensure_future(self._on_terminate()))
            except (NotImplementedError, ValueError):
                pass  # not every platform/context supports this (e.g. non-main thread in tests)

    async def _on_terminate(self) -> None:
        """SIGHUP/SIGTERM: stop everything without asking if we own the engine; either way exit."""
        if self.owns_engine:
            await stop_owned_engine(self.cfg)
        self.exit()

    @work
    async def action_quit_app(self) -> None:
        running_runs = 0
        if self.client.connected:
            try:
                state = await self.client.call("engine", timeout=3.0)
                running_runs = state.get("running_runs", 0)
            except Exception:
                running_runs = 0
        if running_runs and self.owns_engine:
            ok = await self.push_screen_wait(
                ConfirmScreen(f"{running_runs} agents are working — stop them and quit? y/N"))
            if not ok:
                return
        if self.owns_engine:
            await stop_owned_engine(self.cfg)
        self.exit()

    @work
    async def action_stop_everything(self) -> None:
        ok = await self.push_screen_wait(ConfirmScreen("Stop everything (kill switch)? y/N"))
        if not ok:
            return
        if self.client.connected:
            try:
                await self.client.call("stop_now", timeout=5.0)
            except Exception:
                pass

    async def action_restart_engine_action(self) -> None:
        if self.client.connected:
            return
        if await restart_engine(self.cfg):
            self.owns_engine = True
            await self._connect_and_load()

    async def action_quit(self) -> None:  # Textual's own default binding calls this name
        await self.action_quit_app()


def run_tui(cfg: config_mod.Config, *, owns_engine: bool) -> None:
    app = TroupeApp(cfg, owns_engine=owns_engine)
    app.run()
