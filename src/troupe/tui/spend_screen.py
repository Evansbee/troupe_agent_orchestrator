"""REQ-ENG-070: the spend view the TUI header's cost figure links to (`$`). A modal, not a docked
pane -- it's an occasional look-up, not something that needs a permanent slice of screen space.
Fetches through the "spend" RPC like every other TUI data source; never opens the DB directly."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from .. import spend

BY_CYCLE = ("agent", "task", "reason", "day")
SINCE_CYCLE = ("7d", "24h", "all")


class SpendScreen(ModalScreen):
    DEFAULT_CSS = """
    SpendScreen { align: center middle; }
    #spend-box { width: 90%; height: 80%; border: round $accent; padding: 1 2; background: $panel; }
    #spend-body { height: 1fr; overflow-y: auto; }
    """
    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("tab", "cycle_by", "Group by", show=True),
        Binding("s", "cycle_since", "Since", show=True),
    ]

    def __init__(self, client, namer=str) -> None:
        super().__init__()
        self.client = client
        self.namer = namer  # Widget.name is a read-only Textual property, so this can't be `self.name`
        self.by = "agent"
        self.since = "7d"

    def compose(self) -> ComposeResult:
        with Vertical(id="spend-box"):
            yield Static("Loading…", id="spend-body")
            yield Label("tab: group by · s: time range · esc: close", classes="hint")

    async def on_mount(self) -> None:
        await self.reload()

    async def action_cycle_by(self) -> None:
        self.by = BY_CYCLE[(BY_CYCLE.index(self.by) + 1) % len(BY_CYCLE)]
        await self.reload()

    async def action_cycle_since(self) -> None:
        self.since = SINCE_CYCLE[(SINCE_CYCLE.index(self.since) + 1) % len(SINCE_CYCLE)]
        await self.reload()

    async def reload(self) -> None:
        body = self.query_one("#spend-body", Static)
        try:
            result = await self.client.call("spend", since=self.since, by=self.by)
        except Exception as e:
            body.update(f"couldn't load spend: {str(e) or type(e).__name__}")
            return
        body.update(spend.format_report(result["total"], self.since, self.by, result["rows"], name=self.namer))
