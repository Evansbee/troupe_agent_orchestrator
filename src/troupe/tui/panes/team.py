"""Team pane (REQ-TUI-010): one line per agent — handle, state, activity, mail count."""
from __future__ import annotations

from rich.table import Table
from rich.text import Text

from .. import colors as C
from ..glyphs import agent_state_glyph
from . import Pane


class TeamPane(Pane):
    DEFAULT_CSS = """
    TeamPane { border: round $panel-darken-1; border-title-align: left; height: auto; }
    """
    border_title = "TEAM"
    PANE_TITLE = "Team"

    def __init__(self, client, **kwargs):
        super().__init__(client, **kwargs)
        self._agents: list[dict] = []

    async def load(self) -> None:
        result = await self.client.call("agents")
        self._agents = result["items"]
        self._paint()

    def on_troupe_event(self, event: dict) -> None:
        if event["event"].startswith(("agent.", "run.")):
            self.app.call_later(self.load)

    def _paint(self) -> None:
        table = Table.grid(padding=(0, 1))
        table.add_column("glyph", width=2)
        table.add_column("handle", width=14)
        table.add_column("activity", ratio=1)
        table.add_column("mail", width=4, justify="right")
        for a in self._agents:
            glyph, color, label = agent_state_glyph(a)
            handle = Text(a["handle"], style=C.role_color(a["role"]))
            activity = Text(label, style=C.TEXT_DIM if a["state"] != "running" else C.TEXT)
            mail = a.get("mail_queued", 0)
            mail_text = Text(str(mail) if mail else "", style=C.YELLOW)
            table.add_row(Text(glyph, style=color), handle, activity, mail_text)
        if not self._agents:
            table.add_row(Text("no agents", style=C.TEXT_FAINT))
        self.update(table)
