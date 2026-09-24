"""Hex colors for the TUI, verbatim from design/system.md's palette (design/tui.md: "use the hex
values directly, no ANSI table needed" — Textual/Rich auto-downsamples truecolor for the terminal).
A separate module from gui/theme.py so the TUI package never has to import the raylib GUI."""
from ..roles import get_role


def hexc(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


TEXT = "#e7eaf3"
TEXT_DIM = "#949bb0"
TEXT_FAINT = "#5c6378"
BORDER = "#222736"

ACCENT = "#7c8cff"
GREEN = "#34d399"
YELLOW = "#facc15"
ORANGE = "#fb923c"
RED = "#f87171"
CYAN = "#38bdf8"


def role_color(role_key: str) -> str:
    return hexc(get_role(role_key).color)
