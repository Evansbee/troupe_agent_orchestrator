"""Colors and metrics. Midnight theme: deep ink background, soft panels, vivid role colors."""

BG = (9, 11, 16, 255)
BG2 = (12, 15, 21, 255)
PANEL = (16, 19, 27, 255)
PANEL2 = (22, 26, 36, 255)
PANEL3 = (30, 35, 48, 255)
HOVER = (255, 255, 255, 14)
INPUT = (13, 16, 23, 255)
INPUT_FOCUS = (15, 18, 27, 255)
TOOLTIP = (28, 32, 44, 250)
BORDER = (34, 39, 54, 255)
BORDER_HI = (52, 59, 80, 255)

TEXT = (231, 234, 243, 255)
TEXT_DIM = (148, 155, 176, 255)
TEXT_FAINT = (92, 99, 120, 255)
ON_ACCENT = (255, 255, 255, 255)

ACCENT = (124, 140, 255, 255)
ACCENT2 = (167, 139, 250, 255)
GREEN = (52, 211, 153, 255)
YELLOW = (250, 204, 21, 255)
ORANGE = (251, 146, 60, 255)
RED = (248, 113, 113, 255)
CYAN = (56, 189, 248, 255)
PINK = (244, 114, 182, 255)

CODE_BG = (24, 28, 40, 255)
CODE_TEXT = (196, 205, 255, 255)

HUMAN = (236, 239, 247, 255)

RADIUS = 12
TOP_H = 56
SIDEBAR_W = 272
INBOX_W = 360
GAP = 10

STATUS_COLORS = {
    "backlog": TEXT_FAINT,
    "ready": CYAN,
    "in_progress": ACCENT,
    "blocked": RED,
    "review": ORANGE,
    "approved": GREEN,
    "done": GREEN,
    "cancelled": TEXT_FAINT,
}
PRIORITY_COLORS = {0: RED, 1: ORANGE, 2: TEXT_DIM, 3: TEXT_FAINT}
PRIORITY_LABELS = {0: "P0", 1: "P1", 2: "P2", 3: "P3"}


def rgba(rgb: tuple, a: int = 255) -> tuple:
    return (rgb[0], rgb[1], rgb[2], a)
