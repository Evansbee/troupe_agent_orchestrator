"""A small immediate-mode UI toolkit on raylib: fonts, text layout, input, widgets, markdown."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import pyray as rl
from raylib import ffi

from . import theme as T

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FACES = {
    "ui": "Inter-Regular.ttf",
    "med": "Inter-Medium.ttf",
    "bold": "Inter-SemiBold.ttf",
    "mono": "JetBrainsMono-Regular.ttf",
    "monob": "JetBrainsMono-Bold.ttf",
}
_RANGES = [(32, 127), (160, 384), (0x2010, 0x2060), (0x2190, 0x2200), (0x2200, 0x2270), (0x2300, 0x2330),
           (0x2500, 0x2580), (0x25A0, 0x2600), (0x2713, 0x2719)]
CODEPOINTS = [c for a, b in _RANGES for c in range(a, b)]
CODESET = set(CODEPOINTS)
_REPLACE = {"\t": "    ", "’": "'", "‘": "'", "“": '"', "”": '"'}

# Global UI zoom: scales the whole logical coordinate system (fonts and layout metrics alike), not
# individual font sizes — see UI.set_zoom.
ZOOM_DEFAULT = 1.15
ZOOM_MIN = 0.8
ZOOM_MAX = 1.6
ZOOM_STEP = 0.05


def clean(s: str) -> str:
    """Drop characters we have no glyphs for (emoji etc.)."""
    if s.isascii():
        return s.replace("\t", "    ")
    out = []
    for ch in s:
        if ch in _REPLACE:
            out.append(_REPLACE[ch])
        elif ord(ch) in CODESET or ch == "\n":
            out.append(ch)
        elif ord(ch) in (0xFE0F, 0x200D):
            continue
    return "".join(out)


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def r(self) -> float:
        return self.x + self.w

    @property
    def b(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def inset(self, dx: float, dy: float | None = None) -> "Rect":
        dy = dx if dy is None else dy
        return Rect(self.x + dx, self.y + dy, max(0, self.w - 2 * dx), max(0, self.h - 2 * dy))

    def cut_top(self, h: float, gap: float = 0) -> tuple["Rect", "Rect"]:
        return Rect(self.x, self.y, self.w, h), Rect(self.x, self.y + h + gap, self.w, max(0, self.h - h - gap))

    def cut_bottom(self, h: float, gap: float = 0) -> tuple["Rect", "Rect"]:
        return Rect(self.x, self.b - h, self.w, h), Rect(self.x, self.y, self.w, max(0, self.h - h - gap))

    def cut_left(self, w: float, gap: float = 0) -> tuple["Rect", "Rect"]:
        return Rect(self.x, self.y, w, self.h), Rect(self.x + w + gap, self.y, max(0, self.w - w - gap), self.h)

    def cut_right(self, w: float, gap: float = 0) -> tuple["Rect", "Rect"]:
        return Rect(self.r - w, self.y, w, self.h), Rect(self.x, self.y, max(0, self.w - w - gap), self.h)

    def contains(self, p: tuple[float, float]) -> bool:
        return self.x <= p[0] < self.r and self.y <= p[1] < self.b

    def intersect(self, o: "Rect") -> "Rect":
        x, y = max(self.x, o.x), max(self.y, o.y)
        return Rect(x, y, max(0, min(self.r, o.r) - x), max(0, min(self.b, o.b) - y))

    def t(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)


@dataclass
class ScrollState:
    offset: float = 0.0
    target: float = 0.0
    content: float = 0.0
    view: float = 0.0
    at_bottom: bool = True
    stick: bool = False
    rect: Rect | None = None
    dragging: bool = False


@dataclass
class InputState:
    text: str = ""
    cursor: int = 0
    scroll: float = 0.0
    history: list[str] = field(default_factory=list)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def alpha(c: tuple, a: float) -> tuple:
    return (c[0], c[1], c[2], int(max(0, min(1, a)) * (c[3] if len(c) > 3 else 255)))


def mix(c1: tuple, c2: tuple, t: float) -> tuple:
    a1 = c1[3] if len(c1) > 3 else 255
    a2 = c2[3] if len(c2) > 3 else 255
    return (int(lerp(c1[0], c2[0], t)), int(lerp(c1[1], c2[1], t)), int(lerp(c1[2], c2[2], t)), int(lerp(a1, a2, t)))


class UI:
    def __init__(self) -> None:
        self.fonts: dict[tuple[str, int], object] = {}
        self.measure_cache: dict[tuple, float] = {}
        self.wrap_cache: dict[tuple, list[str]] = {}
        self.md_cache: dict[tuple, tuple[list, float]] = {}
        self.scrolls: dict[str, ScrollState] = {}
        self.inputs: dict[str, InputState] = {}
        self.anim: dict[str, float] = {}
        self.clip_stack: list[Rect] = []
        self.overlays: list = []
        self.focus: str | None = None
        self.modal: str | None = None
        self.layer: str | None = None
        self.mouse = (0.0, 0.0)
        self.clicked = False
        self.right_clicked = False
        self.down = False
        self.released = False
        self.wheel = 0.0
        self.dt = 1 / 60
        self.t = 0.0
        self.chars: list[str] = []
        self.dpi = 1.0
        self.zoom = ZOOM_DEFAULT
        self.cursor = rl.MouseCursor.MOUSE_CURSOR_DEFAULT
        self.click_consumed = False
        self.tooltip: tuple[str, float, float] | None = None
        self.hover_id: str | None = None
        self.w = 0
        self.h = 0
        self.cmd = False
        self.shift = False
        self.activity = 0.0  # seconds since last input — lets the app idle at lower fps

    # ── frame ─────────────────────────────────────────────────────────────
    def begin_frame(self) -> None:
        m = rl.get_mouse_position()
        new_mouse = (m.x / self.zoom, m.y / self.zoom)
        self.dt = min(rl.get_frame_time(), 0.1)
        self.t = rl.get_time()
        # Logical canvas = screen / zoom: layout code (Rects, theme metrics) works in these smaller
        # logical units; draw primitives below scale everything back up by zoom at render time.
        self.w, self.h = rl.get_screen_width() / self.zoom, rl.get_screen_height() / self.zoom
        self.clicked = rl.is_mouse_button_pressed(rl.MouseButton.MOUSE_BUTTON_LEFT)
        self.right_clicked = rl.is_mouse_button_pressed(rl.MouseButton.MOUSE_BUTTON_RIGHT)
        self.down = rl.is_mouse_button_down(rl.MouseButton.MOUSE_BUTTON_LEFT)
        self.released = rl.is_mouse_button_released(rl.MouseButton.MOUSE_BUTTON_LEFT)
        self.wheel = rl.get_mouse_wheel_move()
        self.cmd = rl.is_key_down(rl.KeyboardKey.KEY_LEFT_SUPER) or rl.is_key_down(rl.KeyboardKey.KEY_RIGHT_SUPER) \
            or rl.is_key_down(rl.KeyboardKey.KEY_LEFT_CONTROL)
        self.shift = rl.is_key_down(rl.KeyboardKey.KEY_LEFT_SHIFT) or rl.is_key_down(rl.KeyboardKey.KEY_RIGHT_SHIFT)
        self.chars = []
        while (c := rl.get_char_pressed()) > 0:
            self.chars.append(chr(c))
        moved = new_mouse != self.mouse
        self.mouse = new_mouse
        if moved or self.clicked or self.wheel or self.chars or rl.get_key_pressed():
            self.activity = 0.0
        else:
            self.activity += self.dt
        self.cursor = rl.MouseCursor.MOUSE_CURSOR_DEFAULT
        self.click_consumed = False
        self.overlays = []
        self.tooltip = None
        if len(self.measure_cache) > 20000:
            self.measure_cache.clear()
        if len(self.wrap_cache) > 4000:
            self.wrap_cache.clear()
        if len(self.md_cache) > 600:
            self.md_cache.clear()

    def end_frame(self) -> None:
        for fn in self.overlays:
            fn()
        if self.tooltip:
            self._draw_tooltip(*self.tooltip)
        rl.set_mouse_cursor(self.cursor)

    def overlay(self, fn) -> None:
        self.overlays.append(fn)

    def key(self, k: int, repeat: bool = True) -> bool:
        return rl.is_key_pressed(k) or (repeat and rl.is_key_pressed_repeat(k))

    def ease(self, key: str, target: float, speed: float = 14.0) -> float:
        v = self.anim.get(key, target)
        v = lerp(v, target, min(1.0, self.dt * speed))
        if abs(v - target) < 0.001:
            v = target
        self.anim[key] = v
        return v

    def set_zoom(self, zoom: float) -> bool:
        """Clamp/snap to the 0.05 grid and swap in fresh font atlases at the new rasterization size.

        Old atlases are unloaded rather than left cached, since they'll never be hit again at this
        zoom (the cache key is the rasterized px, which changes with zoom) and would otherwise leak
        GPU memory across repeated zoom changes.
        """
        zoom = round(min(ZOOM_MAX, max(ZOOM_MIN, zoom)) / ZOOM_STEP) * ZOOM_STEP
        zoom = round(zoom, 2)
        if zoom == self.zoom:
            return False
        self.zoom = zoom
        for f in self.fonts.values():
            rl.unload_font(f)
        self.fonts.clear()
        return True

    # ── fonts & text ──────────────────────────────────────────────────────
    def font(self, face: str, size: float):
        # Rasterize at size × dpi × zoom so a zoomed-in glyph is real texture detail, not a blown-up
        # bitmap; draw calls then ask for fontSize=size*zoom (see text()), matching 1:1 at this px.
        px = int(round(size * self.dpi * self.zoom))
        key = (face, px)
        f = self.fonts.get(key)
        if f is None:
            if not hasattr(self, "_cps"):
                self._cps = ffi.new("int[]", CODEPOINTS)
            f = rl.load_font_ex(str(FONT_DIR / FACES[face]), px, ffi.cast("int *", self._cps), len(CODEPOINTS))
            rl.set_texture_filter(f.texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
            self.fonts[key] = f
        return f

    def measure(self, s: str, size: float = 14, face: str = "ui") -> float:
        """Width in *logical* (zoom-independent) units, so layout math never has to think about zoom."""
        key = (s, size, face)
        v = self.measure_cache.get(key)
        if v is None:
            v = rl.measure_text_ex(self.font(face, size), s, size * self.zoom, 0).x / self.zoom if s else 0.0
            self.measure_cache[key] = v
        return v

    def text(self, x: float, y: float, s: str, size: float = 14, color: tuple = T.TEXT, face: str = "ui") -> float:
        if not s:
            return 0.0
        s = clean(s)
        scale = self.dpi * self.zoom
        px = round(x * scale) / self.dpi
        py = round(y * scale) / self.dpi
        rl.draw_text_ex(self.font(face, size), s, (px, py), size * self.zoom, 0, color)
        return self.measure(s, size, face)

    def text_fit(self, x: float, y: float, s: str, maxw: float, size: float = 14, color: tuple = T.TEXT,
                 face: str = "ui") -> float:
        return self.text(x, y, self.ellipsize(s, maxw, size, face), size, color, face)

    def text_center(self, r: Rect, s: str, size: float = 14, color: tuple = T.TEXT, face: str = "ui") -> None:
        s = self.ellipsize(s, r.w, size, face)
        w = self.measure(clean(s), size, face)
        self.text(r.cx - w / 2, r.cy - size * 0.62, s, size, color, face)

    def ellipsize(self, s: str, maxw: float, size: float = 14, face: str = "ui") -> str:
        s = clean(" ".join(s.split()))
        if self.measure(s, size, face) <= maxw:
            return s
        lo, hi = 0, len(s)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.measure(s[:mid] + "…", size, face) <= maxw:
                lo = mid
            else:
                hi = mid - 1
        return s[:lo].rstrip() + "…"

    def wrap(self, s: str, width: float, size: float = 14, face: str = "ui") -> list[str]:
        key = (s, int(width), size, face)
        cached = self.wrap_cache.get(key)
        if cached is not None:
            return cached
        lines: list[str] = []
        for para in clean(s).split("\n"):
            if not para.strip():
                lines.append("")
                continue
            cur = ""
            for word in re.findall(r"\S+\s*", para):
                cand = cur + word
                if self.measure(cand.rstrip(), size, face) <= width or not cur:
                    if not cur and self.measure(word.rstrip(), size, face) > width:
                        # hard-break very long words
                        chunk = ""
                        for ch in word:
                            if self.measure(chunk + ch, size, face) > width and chunk:
                                lines.append(chunk)
                                chunk = ""
                            chunk += ch
                        cur = chunk
                    else:
                        cur = cand
                else:
                    lines.append(cur.rstrip())
                    cur = word
            lines.append(cur.rstrip())
        self.wrap_cache[key] = lines
        return lines

    def text_block(self, x: float, y: float, s: str, width: float, size: float = 14, color: tuple = T.TEXT,
                   face: str = "ui", lh: float = 1.45, max_lines: int = 0) -> float:
        lines = self.wrap(s, width, size, face)
        if max_lines and len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.ellipsize(lines[-1] + " …", width, size, face)
        clip = self.clip_stack[-1] if self.clip_stack else None
        step = size * lh
        for i, line in enumerate(lines):
            ly = y + i * step
            if clip and (ly > clip.b or ly + step < clip.y):
                continue
            self.text(x, ly, line, size, color, face)
        return len(lines) * step

    def text_height(self, s: str, width: float, size: float = 14, face: str = "ui", lh: float = 1.45,
                    max_lines: int = 0) -> float:
        n = len(self.wrap(s, width, size, face))
        if max_lines:
            n = min(n, max_lines)
        return n * size * lh

    # ── shapes ────────────────────────────────────────────────────────────
    # Every shape primitive takes LOGICAL coordinates (same numbers as before zoom existed) and scales
    # them up by self.zoom right before the raylib call. Callers, theme metrics (TOP_H, SIDEBAR_W, GAP,
    # RADIUS, ...) and higher-level widgets never need to know zoom exists.
    def _zr(self, r: Rect) -> Rect:
        z = self.zoom
        return Rect(r.x * z, r.y * z, r.w * z, r.h * z)

    def rect(self, r: Rect, color: tuple, radius: float = 0) -> None:
        if r.w <= 0 or r.h <= 0:
            return
        zr = self._zr(r)
        if radius <= 0:
            rl.draw_rectangle_rec(zr.t(), color)
        else:
            zrad = radius * self.zoom
            rl.draw_rectangle_rounded(zr.t(), min(1.0, zrad * 2 / max(1, min(zr.w, zr.h))), 12, color)

    def stroke(self, r: Rect, color: tuple, radius: float = 0, thick: float = 1.0) -> None:
        if r.w <= 0 or r.h <= 0:
            return
        zr = self._zr(r)
        zrad = radius * self.zoom
        rl.draw_rectangle_rounded_lines_ex(zr.t(), min(1.0, zrad * 2 / max(1, min(zr.w, zr.h))), 12,
                                           thick * self.zoom, color)

    def gradient_v(self, r: Rect, top: tuple, bottom: tuple) -> None:
        zr = self._zr(r)
        rl.draw_rectangle_gradient_v(int(zr.x), int(zr.y), int(zr.w), int(zr.h), top, bottom)

    def panel(self, r: Rect, color: tuple = T.PANEL, radius: float = T.RADIUS, border: tuple | None = T.BORDER) -> None:
        self.rect(r, color, radius)
        if border:
            self.stroke(r, border, radius, 1.0)

    def circle(self, x: float, y: float, rad: float, color: tuple) -> None:
        z = self.zoom
        rl.draw_circle_v((x * z, y * z), rad * z, color)

    def ring(self, x: float, y: float, r_in: float, r_out: float, color: tuple, a0: float = 0, a1: float = 360) -> None:
        z = self.zoom
        rl.draw_ring((x * z, y * z), r_in * z, r_out * z, a0, a1, 48, color)

    def glow(self, x: float, y: float, rad: float, color: tuple, strength: float = 0.5) -> None:
        z = self.zoom
        rl.draw_circle_gradient((x * z, y * z), rad * z, alpha(color, strength), alpha(color, 0))

    def line(self, x1: float, y1: float, x2: float, y2: float, color: tuple, thick: float = 1.0) -> None:
        z = self.zoom
        rl.draw_line_ex((x1 * z, y1 * z), (x2 * z, y2 * z), thick * z, color)

    def hline(self, x: float, y: float, w: float, color: tuple = T.BORDER) -> None:
        self.rect(Rect(x, y, w, 1), color)

    def dot(self, x: float, y: float, color: tuple, rad: float = 4) -> None:
        self.circle(x, y, rad, color)

    # ── clipping & hit-testing ────────────────────────────────────────────
    # clip_stack itself stays in LOGICAL units (text_block/markdown compare against it directly);
    # only the actual scissor-mode call needs the zoomed, physical-coordinate rect.
    def push_clip(self, r: Rect) -> None:
        if self.clip_stack:
            r = r.intersect(self.clip_stack[-1])
        self.clip_stack.append(r)
        zr = self._zr(r)
        rl.begin_scissor_mode(int(zr.x), int(zr.y), int(max(0, zr.w)), int(max(0, zr.h)))

    def pop_clip(self) -> None:
        self.clip_stack.pop()
        rl.end_scissor_mode()
        if self.clip_stack:
            zr = self._zr(self.clip_stack[-1])
            rl.begin_scissor_mode(int(zr.x), int(zr.y), int(max(0, zr.w)), int(max(0, zr.h)))

    def blocked(self) -> bool:
        return self.modal is not None and self.layer != self.modal

    def hover(self, r: Rect) -> bool:
        if self.blocked():
            return False
        if self.clip_stack and not self.clip_stack[-1].contains(self.mouse):
            return False
        return r.contains(self.mouse)

    def click(self, r: Rect) -> bool:
        if self.clicked and not self.click_consumed and self.hover(r):
            self.click_consumed = True
            return True
        return False

    def hand(self) -> None:
        self.cursor = rl.MouseCursor.MOUSE_CURSOR_POINTING_HAND

    def tip(self, s: str) -> None:
        self.tooltip = (s, self.mouse[0], self.mouse[1])

    def _draw_tooltip(self, s: str, x: float, y: float) -> None:
        lines = self.wrap(s, 320, 12)
        w = max(self.measure(line, 12) for line in lines) + 16
        h = len(lines) * 17 + 10
        x = min(x + 14, self.w - w - 6)
        y = min(y + 18, self.h - h - 6)
        r = Rect(x, y, w, h)
        self.rect(r, T.TOOLTIP, 6)
        self.stroke(r, T.BORDER_HI, 6)
        for i, line in enumerate(lines):
            self.text(x + 8, y + 5 + i * 17, line, 12, T.TEXT)

    # ── widgets ───────────────────────────────────────────────────────────
    def button(self, id: str, r: Rect, label: str, kind: str = "default", size: float = 13,
               disabled: bool = False, tip: str = "", color: tuple | None = None) -> bool:
        hov = self.hover(r) and not disabled
        h = self.ease(f"btn:{id}", 1.0 if hov else 0.0, 18)
        pressed = hov and self.down
        if kind == "primary":
            base = color or T.ACCENT
            bg = mix(base, (255, 255, 255, 255), 0.12 * h) if not pressed else mix(base, (0, 0, 0, 255), 0.15)
            fg = T.ON_ACCENT
            border = None
        elif kind == "ghost":
            bg = alpha(T.HOVER, h)
            fg = mix(T.TEXT_DIM, T.TEXT, h)
            border = None
        elif kind == "danger":
            bg = mix(T.PANEL2, alpha(T.RED, 0.35), h)
            fg = T.RED
            border = alpha(T.RED, 0.45)
        else:
            bg = mix(T.PANEL2, T.PANEL3, h)
            fg = T.TEXT
            border = mix(T.BORDER, T.BORDER_HI, h)
        if disabled:
            bg, fg = T.PANEL2, T.TEXT_FAINT
        self.rect(r, bg, 7)
        if border:
            self.stroke(r, border, 7)
        self.text_center(r, label, size, fg, "med")
        if hov:
            self.hand()
            if tip:
                self.tip(tip)
        return not disabled and self.click(r)

    def button_w(self, label: str, size: float = 13, pad: float = 14) -> float:
        return self.measure(clean(label), size, "med") + pad * 2

    def pill(self, x: float, y: float, label: str, color: tuple, size: float = 11, filled: bool = False,
             h: float = 20) -> float:
        w = self.measure(clean(label), size, "med") + 16
        r = Rect(x, y, w, h)
        if filled:
            self.rect(r, color, h / 2)
            self.text(x + 8, y + (h - size) / 2 - 1, label, size, T.ON_ACCENT, "med")
        else:
            self.rect(r, alpha(color, 0.16), h / 2)
            self.text(x + 8, y + (h - size) / 2 - 1, label, size, color, "med")
        return w

    def chip(self, id: str, x: float, y: float, label: str, active: bool, color: tuple = T.ACCENT,
             size: float = 12) -> tuple[bool, float]:
        w = self.measure(clean(label), size, "med") + 20
        r = Rect(x, y, w, 26)
        hov = self.hover(r)
        if active:
            self.rect(r, alpha(color, 0.22), 13)
            self.stroke(r, alpha(color, 0.6), 13)
            fg = T.TEXT
        else:
            self.rect(r, T.PANEL3 if hov else T.PANEL2, 13)
            fg = T.TEXT if hov else T.TEXT_DIM
        self.text(x + 10, y + (26 - size) / 2 - 1, label, size, fg, "med")
        if hov:
            self.hand()
        return self.click(r), w

    def badge(self, x: float, y: float, n: int, color: tuple = T.RED) -> None:
        s = str(n) if n < 100 else "99+"
        w = max(18, self.measure(s, 10, "bold") + 10)
        r = Rect(x - w / 2, y - 9, w, 18)
        self.rect(r, color, 9)
        self.text_center(r, s, 10, (255, 255, 255, 255), "bold")

    def avatar(self, x: float, y: float, rad: float, color: tuple, initials: str, running: bool = False,
               dim: bool = False) -> None:
        if running:
            pulse = 0.5 + 0.5 * math.sin(self.t * 3.2)
            self.glow(x, y, rad * (1.9 + 0.25 * pulse), color, 0.35 + 0.15 * pulse)
        c = mix(color, T.BG, 0.55) if dim else color
        self.circle(x, y, rad, mix(c, T.BG, 0.72))
        self.ring(x, y, rad - 1.5, rad, c)
        if running:
            a0 = (self.t * 240) % 360
            self.ring(x, y, rad + 2.5, rad + 4.0, c, a0, a0 + 110)
        size = max(9, rad * 0.62)
        self.text_center(Rect(x - rad, y - rad, rad * 2, rad * 2), initials, size, c, "bold")

    # ── scrolling ─────────────────────────────────────────────────────────
    def scroll_begin(self, id: str, r: Rect, stick_bottom: bool = False) -> ScrollState:
        sc = self.scrolls.setdefault(id, ScrollState(stick=stick_bottom))
        sc.rect = r
        sc.view = r.h
        if self.hover(r) and self.wheel:
            sc.target -= self.wheel * 48
        max_off = max(0.0, sc.content - sc.view)
        if sc.stick and sc.at_bottom:
            sc.target = max_off
        sc.target = max(0.0, min(sc.target, max_off))
        sc.offset = lerp(sc.offset, sc.target, min(1.0, self.dt * 16))
        if abs(sc.offset - sc.target) < 0.5:
            sc.offset = sc.target
        self.push_clip(r)
        return sc

    def scroll_end(self, sc: ScrollState, content_h: float) -> None:
        self.pop_clip()
        sc.content = content_h
        max_off = max(0.0, content_h - sc.view)
        if sc.target > max_off:
            sc.target = max_off
        sc.at_bottom = sc.target >= max_off - 4
        r = sc.rect
        if r and content_h > sc.view + 1:
            track = Rect(r.r - 6, r.y + 3, 4, r.h - 6)
            frac = sc.view / content_h
            th = max(28, track.h * frac)
            ty = track.y + (track.h - th) * (sc.offset / max_off if max_off else 0)
            thumb = Rect(track.x, ty, 4, th)
            hov = self.hover(Rect(track.x - 6, track.y, 16, track.h))
            if hov and self.clicked:
                sc.dragging = True
                self.click_consumed = True
            if not self.down:
                sc.dragging = False
            if sc.dragging:
                rel = (self.mouse[1] - track.y - th / 2) / max(1, track.h - th)
                sc.target = sc.offset = max(0.0, min(1.0, rel)) * max_off
            show = self.ease(f"sb:{id(sc)}", 1.0 if (self.hover(r) or sc.dragging) else 0.35, 8)
            self.rect(thumb, alpha(T.TEXT_FAINT, 0.9 * show), 2)

    def scroll_to_bottom(self, id: str) -> None:
        sc = self.scrolls.get(id)
        if sc:
            sc.at_bottom = True
            sc.target = max(0.0, sc.content - sc.view)

    # ── text input ────────────────────────────────────────────────────────
    def input_height(self, id: str, width: float, size: float = 14, max_lines: int = 8, pad: float = 12) -> float:
        st = self.inputs.setdefault(id, InputState())
        n = len(self._wrap_spans(st.text, width - 2 * pad, size))
        return min(max_lines, max(1, n)) * size * 1.45 + 2 * pad

    def text_input(self, id: str, r: Rect, placeholder: str = "", size: float = 14, multiline: bool = True,
                   pad: float = 12, submit_on_enter: bool = True) -> str | None:
        """Returns submitted text on Enter (Shift+Enter inserts a newline)."""
        st = self.inputs.setdefault(id, InputState())
        focused = self.focus == id
        hov = self.hover(r)
        if hov:
            self.cursor = rl.MouseCursor.MOUSE_CURSOR_IBEAM
            if self.clicked:
                self.focus = id
                self.click_consumed = True
                focused = True
        elif self.clicked and focused and not self.click_consumed:
            self.focus = None
            focused = False
        submitted = None
        if focused and not self.blocked():
            for ch in self.chars:
                st.text = st.text[:st.cursor] + ch + st.text[st.cursor:]
                st.cursor += 1
            K = rl.KeyboardKey
            if self.cmd and self.key(K.KEY_V, False):
                clip = rl.get_clipboard_text() or ""
                clip = clip if multiline else clip.replace("\n", " ")
                st.text = st.text[:st.cursor] + clip + st.text[st.cursor:]
                st.cursor += len(clip)
            if self.cmd and self.key(K.KEY_A, False):
                st.cursor = len(st.text)
            if self.key(K.KEY_BACKSPACE):
                if self.cmd:
                    st.text, st.cursor = st.text[st.cursor:], 0
                elif st.cursor > 0:
                    if rl.is_key_down(K.KEY_LEFT_ALT):
                        j = len(st.text[:st.cursor].rstrip().rsplit(" ", 1)[0]) if " " in st.text[:st.cursor].rstrip() else 0
                        st.text, st.cursor = st.text[:j] + st.text[st.cursor:], j
                    else:
                        st.text = st.text[:st.cursor - 1] + st.text[st.cursor:]
                        st.cursor -= 1
            if self.key(K.KEY_DELETE) and st.cursor < len(st.text):
                st.text = st.text[:st.cursor] + st.text[st.cursor + 1:]
            if self.key(K.KEY_LEFT):
                st.cursor = 0 if self.cmd else max(0, st.cursor - 1)
            if self.key(K.KEY_RIGHT):
                st.cursor = len(st.text) if self.cmd else min(len(st.text), st.cursor + 1)
            if self.key(K.KEY_HOME):
                st.cursor = 0
            if self.key(K.KEY_END):
                st.cursor = len(st.text)
            if self.key(K.KEY_UP, False) and not st.text and st.history:
                st.text = st.history[-1]
                st.cursor = len(st.text)
            if self.key(K.KEY_ENTER) or self.key(K.KEY_KP_ENTER):
                if (self.shift or not submit_on_enter) and multiline:
                    st.text = st.text[:st.cursor] + "\n" + st.text[st.cursor:]
                    st.cursor += 1
                elif st.text.strip():
                    submitted = st.text.strip()
                    st.history.append(submitted)
                    st.text, st.cursor = "", 0
            if self.key(K.KEY_ESCAPE, False):
                self.focus = None
        # draw
        fa = self.ease(f"in:{id}", 1.0 if focused else 0.0, 16)
        self.rect(r, mix(T.INPUT, T.INPUT_FOCUS, fa), 9)
        self.stroke(r, mix(T.BORDER, alpha(T.ACCENT, 0.8), fa), 9, 1.0 + 0.5 * fa)
        inner = r.inset(pad, pad)
        self.push_clip(r.inset(2, 2))
        step = size * 1.45
        if not st.text:
            self.text(inner.x, inner.y, placeholder, size, T.TEXT_FAINT)
        spans = self._wrap_spans(st.text, inner.w, size)
        cur = min(st.cursor, len(st.text))
        cur_line = len(spans) - 1
        for i, (a, b) in enumerate(spans):
            if a <= cur < b or (cur == b and (i + 1 == len(spans) or spans[i + 1][0] > b)):
                cur_line = i
                break
        a, _b = spans[cur_line]
        cx = inner.x + self.measure(clean(st.text[a:cur]), size)
        cy = inner.y + cur_line * step
        lines = [st.text[a:b].rstrip("\n") for a, b in spans]
        visible = max(1, int(inner.h // step + 0.01))
        if cur_line - st.scroll >= visible:
            st.scroll = cur_line - visible + 1
        if cur_line < st.scroll:
            st.scroll = cur_line
        oy = -st.scroll * step
        for i, line in enumerate(lines):
            self.text(inner.x, inner.y + i * step + oy, line, size, T.TEXT)
        if focused and (int(self.t * 1.8) % 2 == 0 or self.activity < 0.6):
            self.rect(Rect(cx, cy + oy + 1, 1.6, size * 1.2), T.ACCENT)
        self.pop_clip()
        return submitted

    def _wrap_spans(self, text: str, width: float, size: float) -> list[tuple[int, int]]:
        """Wrap raw text into (start, end) index spans so the cursor maps exactly onto lines."""
        spans: list[tuple[int, int]] = []
        off = 0
        for para in text.split("\n"):
            if not para:
                spans.append((off, off))
            else:
                ls = off
                for m in re.finditer(r"\S+\s*|\s+", para):
                    ws, we = off + m.start(), off + m.end()
                    if ws > ls and self.measure(clean(text[ls:we].rstrip()), size) > width:
                        spans.append((ls, ws))
                        ls = ws
                spans.append((ls, off + len(para)))
            off += len(para) + 1
        return spans or [(0, 0)]

    def input_text(self, id: str) -> str:
        return self.inputs.setdefault(id, InputState()).text

    def set_input(self, id: str, text: str) -> None:
        st = self.inputs.setdefault(id, InputState())
        st.text, st.cursor = text, len(text)

    # ── markdown ──────────────────────────────────────────────────────────
    INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\([^)\n]+\)|_[^_\n]+_)")

    def _spans(self, s: str, base_face: str = "ui") -> list[tuple[str, str]]:
        out = []
        for part in self.INLINE.split(s):
            if not part:
                continue
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                out.append((part[2:-2], "bold"))
            elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                out.append((part[1:-1], "code"))
            elif part.startswith("[") and "](" in part:
                out.append((part[1:part.index("](")], "link"))
            elif part.startswith("_") and part.endswith("_") and len(part) > 2:
                out.append((part[1:-1], "em"))
            else:
                out.append((part, base_face))
        return out

    def _layout_spans(self, spans: list[tuple[str, str]], x0: float, width: float, size: float,
                      color: tuple, lh: float, ops: list, y: float) -> float:
        """Greedy-wrap styled spans. Returns height consumed."""
        faces = {"ui": "ui", "bold": "bold", "code": "mono", "link": "med", "em": "ui", "med": "med"}
        cols = {"link": T.ACCENT, "code": T.CODE_TEXT, "em": T.TEXT_DIM}
        step = size * lh
        x = x0
        line = 0
        for text, style in spans:
            face = faces.get(style, "ui")
            fsize = size * 0.92 if style == "code" else size
            col = cols.get(style, color)
            for word in re.findall(r"\S+\s*|\s+", clean(text)):
                w = self.measure(word, fsize, face)
                wt = self.measure(word.rstrip(), fsize, face)
                if x + wt > x0 + width and x > x0:
                    line += 1
                    x = x0
                    word = word.lstrip()
                    if not word:
                        continue
                    w = self.measure(word, fsize, face)
                    wt = self.measure(word.rstrip(), fsize, face)
                if wt > width:  # very long token: hard-break
                    chunk = ""
                    for ch in word:
                        if self.measure(chunk + ch, fsize, face) > x0 + width - x and chunk:
                            ops.append(("t", x, y + line * step + (size - fsize) * 0.6, chunk, fsize, face, col, style))
                            line += 1
                            x = x0
                            chunk = ""
                        chunk += ch
                    word, w = chunk, self.measure(chunk, fsize, face)
                ops.append(("t", x, y + line * step + (size - fsize) * 0.6, word, fsize, face, col, style))
                x += w
        return (line + 1) * step

    def md_layout(self, md: str, width: float, size: float = 14, color: tuple = T.TEXT) -> tuple[list, float]:
        key = (md, int(width), size, color)
        hit = self.md_cache.get(key)
        if hit:
            return hit
        ops: list = []
        y = 0.0
        lines = md.replace("\r", "").split("\n")
        i = 0
        para: list[str] = []

        def flush() -> None:
            nonlocal y, para
            if para:
                y += self._layout_spans(self._spans(" ".join(s.strip() for s in para)), 0, width, size, color, 1.5, ops, y)
                y += size * 0.55
                para = []

        while i < len(lines):
            ln = lines[i]
            st = ln.strip()
            if st.startswith("```"):
                flush()
                code = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code.append(lines[i])
                    i += 1
                i += 1
                cs = size * 0.86
                wrapped = []
                for c in code:
                    wrapped += self.wrap(c, width - 24, cs, "mono") or [""]
                h = len(wrapped) * cs * 1.5 + 16
                ops.append(("r", 0, y, width, h, T.CODE_BG, 6))
                for k, c in enumerate(wrapped):
                    ops.append(("t", 12, y + 8 + k * cs * 1.5, c, cs, "mono", T.CODE_TEXT, "code"))
                y += h + size * 0.6
                continue
            m = re.match(r"^(#{1,4})\s+(.*)", st)
            if m:
                flush()
                level = len(m.group(1))
                hs = {1: size * 1.45, 2: size * 1.25, 3: size * 1.1, 4: size}[level]
                y += size * (0.5 if ops else 0)
                y += self._layout_spans([(m.group(2).replace("**", ""), "bold")], 0, width, hs, T.TEXT, 1.35, ops, y)
                if level <= 2:
                    ops.append(("r", 0, y + 2, width, 1, T.BORDER, 0))
                    y += 8
                y += size * 0.3
                i += 1
                continue
            m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?(.*)", ln)
            if m:
                flush()
                indent = min(4, len(m.group(1).replace("\t", "  ")) // 2)
                bx = 6 + indent * 18
                marker = m.group(2)
                check = m.group(3)
                if check:
                    done = "x" in check.lower()
                    ops.append(("box", bx - 2, y + size * 0.28, size * 0.85, done))
                elif marker[0].isdigit():
                    ops.append(("t", bx - 4, y, marker, size, "med", T.TEXT_DIM, "ui"))
                else:
                    ops.append(("c", bx + 2, y + size * 0.72, 2.6, T.TEXT_DIM))
                tx = bx + (size * 1.3 if not marker[0].isdigit() else size * 1.5)
                y += self._layout_spans(self._spans(m.group(4)), tx, width - tx, size, color, 1.5, ops, y)
                y += size * 0.2
                i += 1
                continue
            if st.startswith(">"):
                flush()
                y0 = y
                h = self._layout_spans(self._spans(st.lstrip("> ")), 14, width - 14, size, T.TEXT_DIM, 1.5, ops, y)
                ops.append(("r", 0, y0 + 2, 3, h - 4, T.BORDER_HI, 1))
                y += h + size * 0.3
                i += 1
                continue
            if re.match(r"^(-{3,}|\*{3,}|_{3,})$", st):
                flush()
                ops.append(("r", 0, y + size * 0.5, width, 1, T.BORDER, 0))
                y += size * 1.1
                i += 1
                continue
            if st.startswith("|") and st.endswith("|"):
                flush()
                if re.match(r"^\|[\s:|-]+\|$", st):
                    i += 1
                    continue
                cells = [c.strip() for c in st.strip("|").split("|")]
                cw = width / max(1, len(cells))
                hmax = 0.0
                for k, c in enumerate(cells):
                    hmax = max(hmax, self._layout_spans(self._spans(c), k * cw + 6, cw - 12, size * 0.92, color, 1.4, ops, y + 4))
                ops.append(("r", 0, y + hmax + 7, width, 1, T.BORDER, 0))
                y += hmax + 9
                i += 1
                continue
            if not st:
                flush()
                i += 1
                continue
            para.append(ln)
            i += 1
        flush()
        if ops:
            y -= size * 0.5
        res = (ops, max(y, size * 1.4))
        self.md_cache[key] = res
        return res

    def markdown(self, x: float, y: float, md: str, width: float, size: float = 14, color: tuple = T.TEXT) -> float:
        ops, h = self.md_layout(md, width, size, color)
        clip = self.clip_stack[-1] if self.clip_stack else None
        for op in ops:
            kind = op[0]
            if kind == "t":
                _, ox, oy, s, fs, face, col, style = op
                if clip and (y + oy > clip.b or y + oy + fs * 1.6 < clip.y):
                    continue
                if style == "code" and s.strip():
                    tw = self.measure(s.rstrip(), fs, face)
                    self.rect(Rect(x + ox - 2, y + oy - 1, tw + 4, fs * 1.35), T.CODE_BG, 3)
                self.text(x + ox, y + oy, s, fs, col, face)
            elif kind == "r":
                _, ox, oy, w, hh, col, rad = op
                if clip and (y + oy > clip.b or y + oy + hh < clip.y):
                    continue
                self.rect(Rect(x + ox, y + oy, w, hh), col, rad)
            elif kind == "c":
                _, ox, oy, rad, col = op
                self.circle(x + ox, y + oy, rad, col)
            elif kind == "box":
                _, ox, oy, sz, done = op
                br = Rect(x + ox, y + oy, sz, sz)
                if done:
                    self.rect(br, T.GREEN, 3)
                    self.line(br.x + 3, br.cy, br.x + sz * 0.42, br.b - 3, T.BG, 2)
                    self.line(br.x + sz * 0.42, br.b - 3, br.r - 3, br.y + 3, T.BG, 2)
                else:
                    self.stroke(br, T.TEXT_DIM, 3, 1.3)
        return h
