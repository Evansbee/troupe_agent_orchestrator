"""State glyph vocabulary shared by every pane (design/tui.md): one glyph+color+label triple per
state, never color alone (REQ-TUI-011). Reused by Team, Tasks and Comms so the vocabulary stays
one thing, not one per pane."""
from __future__ import annotations

from . import colors as C

WAIT_GLYPH = {
    "human": ("?", C.YELLOW, "waiting: you"),
    "review": ("→", C.ACCENT, "waiting: review"),
    "dependency": ("✕", C.TEXT_DIM, "blocked: dependency"),
    "blocked": ("✕", C.RED, "blocked"),
    "providers": ("‡", C.RED, "limited: all providers"),
    "rate_limit": ("‡", C.ORANGE, "limited"),
    "slot": ("…", C.TEXT_DIM, "queued"),
    "parked": ("!", C.ORANGE, "parked"),
}


def agent_state_glyph(agent: dict) -> tuple[str, str, str]:
    """(glyph, color, label) for one agent row from the `agents`/`snapshot` API shape."""
    role_col = C.role_color(agent["role"])
    if agent["state"] == "running":
        return "●", role_col, agent.get("activity") or "working"
    waiting = agent.get("waiting_on")
    if not waiting:
        return "○", C.TEXT_DIM, "idle"
    kind = waiting.get("kind", "")
    glyph, color, default_label = WAIT_GLYPH.get(kind, ("○", C.TEXT_DIM, "idle"))
    if kind == "review":
        targets = waiting.get("targets") or []
        label = f"waiting: {', '.join(targets)}" if targets else default_label
    elif kind == "slot":
        pos = waiting.get("queue_position")
        glyph = f"…{pos}" if pos else glyph
        label = f"queued #{pos}" if pos else default_label
    elif kind in ("blocked", "dependency", "providers", "rate_limit"):
        label = waiting.get("detail") or default_label
    else:
        label = default_label
    return glyph, color, label


TASK_GLYPH = {
    "backlog": ("○", C.TEXT_FAINT, "backlog"),
    "ready": ("○", C.CYAN, "ready"),
    "in_progress": ("●", C.ACCENT, "in progress"),
    "review": ("→", C.ORANGE, "review"),
    "blocked": ("✕", C.RED, "blocked"),
    "approved": ("✓", C.GREEN, "approved"),
    "done": ("✓", C.GREEN, "done"),
    "cancelled": ("✗", C.TEXT_FAINT, "cancelled"),
}


def task_status_glyph(status: str) -> tuple[str, str, str]:
    return TASK_GLYPH.get(status, ("○", C.TEXT_DIM, status))
