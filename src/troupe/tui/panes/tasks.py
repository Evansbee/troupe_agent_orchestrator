"""Tasks pane (REQ-TUI-010): in-flight tasks grouped by stage, human requests first."""
from __future__ import annotations

from rich.table import Table
from rich.text import Text

from .. import colors as C
from ..glyphs import task_status_glyph
from . import Pane

STAGES = ("in_progress", "review", "blocked", "ready")


class TasksPane(Pane):
    DEFAULT_CSS = """
    TasksPane { border: round $panel-darken-1; border-title-align: left; height: 1fr; }
    TasksPane:focus { border: round $accent; }
    """
    border_title = "TASKS"
    PANE_TITLE = "Tasks"

    def __init__(self, client, **kwargs):
        super().__init__(client, **kwargs)
        self._tasks: list[dict] = []

    async def _load(self) -> None:
        result = await self.client.call("tasks", status=list(STAGES))
        self._tasks = result["items"]
        self._paint()

    def on_troupe_event(self, event: dict) -> None:
        if event["event"].startswith("task."):
            self.app.call_later(self.load)

    def _paint(self) -> None:
        ordered = sorted(
            self._tasks,
            key=lambda t: (STAGES.index(t["status"]) if t["status"] in STAGES else len(STAGES),
                           0 if t["flags"]["human_request"] else 1, t["priority"], t["id"]),
        )
        table = Table.grid(padding=(0, 1))
        table.add_column("glyph", width=2)
        table.add_column("id", width=5)
        table.add_column("title", ratio=1)
        table.add_column("assignee", width=12)
        for t in ordered:
            glyph, color, _label = task_status_glyph(t["status"])
            star = "★ " if t["flags"]["human_request"] else ""
            title = Text(star + t["title"], style=C.TEXT)
            assignee = Text(t.get("assignee") or "", style=C.TEXT_DIM)
            table.add_row(Text(glyph, style=color), Text(f"#{t['id']}", style=C.TEXT_FAINT), title, assignee)
        if not ordered:
            table.add_row("", "", Text("nothing in flight", style=C.TEXT_FAINT), "")
        self._table = table  # kept for tests: Static wraps whatever update() is given, so this is
        # the one place the actual Table (and its column layout) stays inspectable.
        self.update(table)
