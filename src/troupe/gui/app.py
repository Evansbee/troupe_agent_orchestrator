"""troupe desktop app: window, layout shell, top bar, team sidebar, human inbox, tabs."""

from __future__ import annotations

import math
import os
import time

import pyray as rl

from ..config import Config
from ..roles import get_role
from ..team import ago
from . import theme as T
from .core import UI, Rect, alpha, mix
from .data import Data

TABS = ["Chat", "Pulse", "Board", "Mail", "Memory", "Docs", "Agent"]


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.data = Data(cfg)
        self.ui = UI()
        self.tab = "Chat"
        self.chat_with = next((a.id for a in cfg.agents if a.role == "pm"), cfg.agents[0].id)
        self.sel_agent = next((a.id for a in cfg.agents if a.role == "lead"), cfg.agents[0].id)
        self.sel_task: int | None = None
        self.sel_run: int | None = None
        self.run_view = "Transcript"
        self.mail_filter: str | None = None
        self.mem_filter: str | None = None
        self.doc_sel: str | None = None
        self.expanded: set[str] = set()
        self.toasts: list[tuple[float, str, tuple]] = []
        self.new_task_open = False
        self.shot_requested: str | None = None
        from .views import PulseState

        self.pulse = PulseState()

    # ── main loop ─────────────────────────────────────────────────────────
    def run(self) -> None:
        flags = (rl.ConfigFlags.FLAG_WINDOW_RESIZABLE | rl.ConfigFlags.FLAG_WINDOW_HIGHDPI
                 | rl.ConfigFlags.FLAG_MSAA_4X_HINT | rl.ConfigFlags.FLAG_VSYNC_HINT)
        rl.set_config_flags(flags)
        rl.set_trace_log_level(rl.TraceLogLevel.LOG_ERROR)
        rl.init_window(1560, 980, f"troupe — {self.cfg.project}")
        rl.set_window_min_size(1120, 720)
        rl.set_exit_key(0)
        self.ui.dpi = max(1.0, rl.get_window_scale_dpi().x)
        rl.set_target_fps(60)
        self.data.refresh(force=True)
        fps = 60
        frames = 0
        auto_shot = os.environ.get("TROUPE_SHOT")
        if os.environ.get("TROUPE_TAB") in TABS:
            self.tab = os.environ["TROUPE_TAB"]
        if os.environ.get("TROUPE_TASK", "").isdigit():
            self.sel_task = int(os.environ["TROUPE_TASK"])
        while not rl.window_should_close():
            self.data.refresh()
            self.handle_notifications()
            busy = self.data.running_count() > 0 or self.pulse.particles or self.toasts
            want = 60 if (self.ui.activity < 4 or busy) else 20
            if want != fps:
                fps = want
                rl.set_target_fps(fps)
            rl.begin_drawing()
            rl.clear_background(T.BG)
            self.ui.begin_frame()
            self.frame()
            self.ui.end_frame()
            rl.end_drawing()
            frames += 1
            if self.shot_requested and frames > 3:
                screenshot(self.shot_requested)
                self.toast(f"Saved {self.shot_requested}", T.GREEN)
                self.shot_requested = None
            if auto_shot and frames == int(os.environ.get("TROUPE_SHOT_FRAME", "90")):
                screenshot(auto_shot)
                break
        rl.close_window()

    def handle_notifications(self) -> None:
        d = self.data
        from .views import on_new_messages

        if d.new_messages:
            on_new_messages(self, d.new_messages)
            for m in d.new_messages:
                if m["recipient"] == "human" and m["kind"] == "chat" and m["sender"] != self.chat_with_visible():
                    self.toast(f"{d.name_of(m['sender'])}: {m['body'][:90]}", d.color_of(m["sender"]))
                    if not rl.is_window_focused():
                        d.notify(f"troupe · {d.name_of(m['sender'])}", m["body"])
            d.new_messages = []
        if d.new_chat_answers:
            for q in d.new_chat_answers:
                self.toast(f"Answered in chat ✓ · {q['question'][:70]}", T.GREEN)
            d.new_chat_answers = []
        if d.new_questions:
            for q in d.new_questions:
                self.toast(f"{d.name_of(q['asker'])} needs you: {q['question'][:90]}", d.color_of(q["asker"]))
                if not rl.is_window_focused():
                    d.notify(f"troupe · {d.name_of(q['asker'])} asks", q["question"])
            d.new_questions = []

    def chat_with_visible(self) -> str | None:
        return self.chat_with if self.tab == "Chat" else None

    def toast(self, text: str, color: tuple = T.ACCENT) -> None:
        self.toasts.append((time.time(), text, color))
        self.toasts = self.toasts[-4:]

    # ── layout ────────────────────────────────────────────────────────────
    def frame(self) -> None:
        ui = self.ui
        self.shortcuts()
        full = Rect(0, 0, ui.w, ui.h)
        top, rest = full.cut_top(T.TOP_H)
        body = rest.inset(T.GAP, 0)
        body.h -= T.GAP
        side, body = body.cut_left(T.SIDEBAR_W, T.GAP)
        inbox, center = body.cut_right(T.INBOX_W, T.GAP)
        self.draw_top(top)
        self.draw_sidebar(side)
        self.draw_inbox(inbox)
        self.draw_center(center)
        self.draw_toasts()
        from .views import draw_modals

        draw_modals(self)

    def shortcuts(self) -> None:
        ui = self.ui
        if ui.cmd and ui.focus is None:
            for i, name in enumerate(TABS):
                if rl.is_key_pressed(rl.KeyboardKey.KEY_ONE + i):
                    self.tab = name
        if ui.cmd and rl.is_key_pressed(rl.KeyboardKey.KEY_P) and ui.focus is None:
            self.data.set_paused(not self.data.paused)
        if rl.is_key_pressed(rl.KeyboardKey.KEY_ESCAPE):
            if self.sel_task is not None:
                self.sel_task = None
            elif self.new_task_open:
                self.new_task_open = False
        if rl.is_key_pressed(rl.KeyboardKey.KEY_F12):
            self.shot_requested = str(self.cfg.state_dir / f"screenshot-{int(time.time())}.png")

    # ── top bar ───────────────────────────────────────────────────────────
    def draw_top(self, r: Rect) -> None:
        ui, d = self.ui, self.data
        rl.draw_rectangle_gradient_v(0, 0, int(r.w), int(r.h), T.BG2, T.BG)
        ui.hline(0, r.b - 1, r.w, alpha(T.BORDER, 0.7))
        # logo: a little constellation mark
        cx, cy = r.x + 30, r.cy
        for i in range(3):
            ang = ui.t * 0.6 + i * 2.094
            ui.circle(cx + math.cos(ang) * 8, cy + math.sin(ang) * 8, 3.2,
                      [T.ACCENT, T.PINK, T.GREEN][i])
        ui.circle(cx, cy, 3.6, T.TEXT)
        x = r.x + 48
        x += ui.text(x, r.cy - 11, "troupe", 19, T.TEXT, "bold") + 12
        x += ui.text(x, r.cy - 8, self.cfg.project, 14, T.TEXT_DIM, "med") + 22
        # engine state pill
        if not d.engine_alive:
            label, col = "Engine offline", T.RED
        elif d.paused:
            label, col = "Paused", T.YELLOW
        elif d.kv.get("throttled"):
            label, col = "Throttled", T.ORANGE
        else:
            label, col = "Live", T.GREEN
        pr = Rect(x, r.cy - 13, ui.measure(label, 12, "med") + 34, 26)
        ui.rect(pr, alpha(col, 0.14), 13)
        pulse = 0.6 + 0.4 * math.sin(ui.t * 3) if label == "Live" else 1.0
        ui.glow(pr.x + 13, pr.cy, 8, col, 0.5 * pulse)
        ui.circle(pr.x + 13, pr.cy, 4, col)
        ui.text(pr.x + 24, pr.cy - 8, label, 12, col, "med")
        if ui.hover(pr) and d.kv.get("throttled"):
            ui.tip(str(d.kv.get("throttled")))
        x = pr.r + 12
        for filename in ("team.yaml", "troupe.toml"):
            error = d.kv.get(f"config_error.{filename}")
            if error:
                width = ui.pill(x, r.cy - 13, f"{filename} invalid", T.RED, 12, h=26)
                if ui.hover(Rect(x, r.cy - 13, width, 26)):
                    ui.tip(str(error))
                x += width + 8
        for backend in ("claude", "codex", "local"):
            limit = d.limit_label(backend)
            if limit:
                x += ui.pill(x, r.cy - 13, limit, T.ORANGE, 12, h=26) + 8
        x += 8
        stats = [(f"{d.running_count()}", "working"), (f"{d.runs_1h}/{self.cfg.budget.max_runs_per_hour}", "runs/h"),
                 (f"${d.cost_24h:.2f}", "24h est.")]
        open_n = sum(1 for t in d.tasks if t["status"] not in ("done", "cancelled"))
        stats.append((str(open_n), "open tasks"))
        for val, lab in stats:
            x += ui.text(x, r.cy - 9, val, 15, T.TEXT, "bold") + 5
            x += ui.text(x, r.cy - 7, lab, 12, T.TEXT_FAINT) + 18
        rlim = d.kv.get("claude_ratelimit") or {}
        wins = rlim.get("unifiedWindows") or {}
        for key, lab in (("five_hour", "5h"), ("seven_day", "7d")):
            w = wins.get(key)
            if not w:
                continue
            u = float(w.get("utilization") or 0)
            ui.text(x, r.cy - 7, f"claude {lab}", 11, T.TEXT_FAINT)
            bx = x + ui.measure(f"claude {lab}", 11) + 6
            bar = Rect(bx, r.cy - 3, 54, 6)
            ui.rect(bar, T.PANEL3, 3)
            ui.rect(Rect(bx, bar.y, max(6, 54 * min(1, u)), 6), T.GREEN if u < 0.6 else T.ORANGE if u < 0.85 else T.RED, 3)
            if ui.hover(Rect(x, r.cy - 10, bar.r - x, 20)):
                ui.tip(f"Claude {lab} window: {u * 100:.0f}% used")
            x = bar.r + 16
        # right buttons
        bx = r.r - T.GAP
        lbl = "Resume" if d.paused else "Pause"
        bw = ui.button_w(lbl) + 8
        bx -= bw
        if ui.button("pause", Rect(bx, r.cy - 16, bw, 32), lbl, "primary" if d.paused else "default",
                     tip="Pause autonomous work (chat still answered)  ⌘P"):
            d.set_paused(not d.paused)
        bw = ui.button_w("+ Task")
        bx -= bw + 8
        if ui.button("newtask", Rect(bx, r.cy - 16, bw, 32), "+ Task", tip="Add a task to the board"):
            self.new_task_open = True
            ui.focus = "nt_title"

    # ── team sidebar ──────────────────────────────────────────────────────
    def draw_sidebar(self, r: Rect) -> None:
        ui, d = self.ui, self.data
        ui.panel(r)
        head, body = r.cut_top(44)
        ui.text(head.x + 16, head.y + 16, "TEAM", 11, T.TEXT_FAINT, "bold")
        n_run = d.running_count()
        ui.text(head.r - 16 - ui.measure(f"{n_run} working", 11), head.y + 16, f"{n_run} working", 11,
                T.GREEN if n_run else T.TEXT_FAINT, "med")
        sc = ui.scroll_begin("sidebar", body.inset(6, 0))
        y = body.y - sc.offset
        for a in d.agents:
            card = Rect(body.x + 8, y, body.w - 16, 66)
            self.agent_card(a, card)
            y += 70
        ui.scroll_end(sc, y + sc.offset - body.y + 8)

    def agent_card(self, a: dict, r: Rect) -> None:
        ui, d = self.ui, self.data
        role = get_role(a["role"])
        col = (*role.color, 255)
        sel = self.tab == "Agent" and self.sel_agent == a["id"]
        hov = ui.hover(r)
        h = ui.ease(f"card:{a['id']}", 1.0 if (hov or sel) else 0.0, 16)
        if sel:
            ui.rect(r, alpha(col, 0.10), 10)
            ui.stroke(r, alpha(col, 0.35), 10)
        elif h > 0:
            ui.rect(r, alpha(T.HOVER, h), 10)
        running = a["state"] == "running"
        dim = not a["enabled"]
        ui.avatar(r.x + 26, r.y + 33, 17, col, d.initials_of(a["id"]), running, dim)
        tx = r.x + 52
        tw = r.w - 60
        name_col = T.TEXT_FAINT if dim else T.TEXT
        nw = ui.text(tx, r.y + 11, ui.ellipsize(d.name_of(a["id"], local=True), tw * 0.55, 14, "med"), 14, name_col, "med")
        model = f"{a['backend']}" + (f" · {a['model']}" if a["model"] else "")
        ui.text_fit(tx + nw + 8, r.y + 13, model, tw - nw - 34, 11, T.TEXT_FAINT)
        if running:
            line, lcol = a["activity"] or "working…", mix(col, T.TEXT, 0.35)
        elif dim:
            line, lcol = "disabled", T.TEXT_FAINT
        elif d.parked(a):
            line, lcol = "parked — owes work", T.ORANGE
        else:
            line, lcol = a["status"] or f"{role.title} · idle", T.TEXT_DIM
        ui.text_fit(tx, r.y + 33, line, tw, 12, lcol)
        tasks = d.open_tasks_of(a["id"])
        meta = f"{len(tasks)} task{'s' if len(tasks) != 1 else ''}" if tasks else ""
        if a["last_run_at"]:
            meta += ("  ·  " if meta else "") + f"ran {ago(a['last_run_at'])}"
        ui.text_fit(tx, r.y + 49, meta, tw, 11, T.TEXT_FAINT)
        unread = d.chat_unread.get(a["id"], 0)
        if unread:
            ui.badge(r.r - 14, r.y + 16, unread, T.ACCENT)
        if hov:
            ui.hand()
            ui.tip(f"{role.title} — {role.blurb}")
        if ui.click(r):
            self.sel_agent = a["id"]
            self.sel_run = None
            self.tab = "Agent"
        if ui.hover(r) and ui.right_clicked:
            self.chat_with = a["id"]
            self.tab = "Chat"

    # ── human inbox ───────────────────────────────────────────────────────
    def draw_inbox(self, r: Rect) -> None:
        ui, d = self.ui, self.data
        ui.panel(r)
        head, body = r.cut_top(52)
        ui.text(head.x + 18, head.y + 17, "Needs you", 16, T.TEXT, "bold")
        n = len(d.questions)
        if n:
            ui.badge(head.x + 18 + ui.measure("Needs you", 16, "bold") + 16, head.y + 26, n, T.PINK)
        ui.hline(r.x + 1, head.b - 1, r.w - 2, T.BORDER)
        if not d.questions:
            cy = body.y + body.h * 0.36
            ui.glow(body.cx, cy, 60, T.GREEN, 0.18)
            ui.ring(body.cx, cy, 22, 24.5, alpha(T.GREEN, 0.9))
            ui.line(body.cx - 9, cy + 1, body.cx - 2, cy + 8, T.GREEN, 3)
            ui.line(body.cx - 2, cy + 8, body.cx + 11, cy - 7, T.GREEN, 3)
            msg = "All clear."
            ui.text(body.cx - ui.measure(msg, 15, "med") / 2, cy + 40, msg, 15, T.TEXT, "med")
            sub = "Questions and ideas from the team land here."
            ui.text(body.cx - ui.measure(sub, 12) / 2, cy + 62, sub, 12, T.TEXT_FAINT)
            return
        from .views import question_card, question_card_height

        sc = ui.scroll_begin("inbox", body.inset(0, 4))
        y = body.y + 8 - sc.offset
        w = body.w - 24
        for q in d.questions:
            h = question_card_height(self, q, w)
            if y + h > body.y - 20 and y < body.b + 20:
                question_card(self, q, Rect(body.x + 12, y, w, h))
            y += h + 10
        ui.scroll_end(sc, y + sc.offset - body.y + 4)

    # ── center ────────────────────────────────────────────────────────────
    def draw_center(self, r: Rect) -> None:
        ui, d = self.ui, self.data
        ui.panel(r)
        bar, body = r.cut_top(46)
        x = bar.x + 10
        unread_total = sum(d.chat_unread.values())
        for i, name in enumerate(TABS):
            label = name if name != "Agent" else d.name_of(self.sel_agent)
            has_badge = name == "Chat" and unread_total
            w = ui.measure(label, 13, "med") + 28 + (22 if has_badge else 0)
            tr = Rect(x, bar.y + 6, w, 34)
            active = self.tab == name
            hov = ui.hover(tr)
            if hov and not active:
                ui.rect(tr.inset(2, 3), T.HOVER, 8)
                ui.hand()
            col = T.TEXT if active else (T.TEXT if hov else T.TEXT_DIM)
            ui.text(tr.x + 14, tr.y + 9, label, 13, col, "med")
            if has_badge:
                ui.badge(tr.x + 14 + ui.measure(label, 13, "med") + 16, tr.cy, unread_total, T.ACCENT)
            if name == "Board":
                nrev = sum(1 for t in d.tasks if t["status"] == "review")
                if nrev:
                    ui.dot(tr.r - 6, tr.y + 8, T.ORANGE, 3.5)
            if active:
                ux = ui.ease("tab_x", tr.x + 10, 18)
                uw = ui.ease("tab_w", tr.w - 20 - (22 if has_badge else 0), 18)
                ui.rect(Rect(ux, bar.b - 3, uw, 3), T.ACCENT, 1.5)
            if ui.click(tr):
                self.tab = name
            if ui.hover(tr) and name in TABS[:7]:
                ui.tip(f"{name}  ⌘{i + 1}")
            x += w + 2
        ui.hline(r.x + 1, bar.b, r.w - 2, T.BORDER)
        from . import views

        content = Rect(body.x, body.y + 1, body.w, body.h - 1)
        {"Chat": views.chat_view, "Pulse": views.pulse_view, "Board": views.board_view, "Mail": views.mail_view,
         "Memory": views.memory_view, "Docs": views.docs_view, "Agent": views.agent_view}[self.tab](self, content)

    # ── toasts ────────────────────────────────────────────────────────────
    def draw_toasts(self) -> None:
        ui = self.ui
        now = time.time()
        self.toasts = [t for t in self.toasts if now - t[0] < 6]
        y = ui.h - 20
        for t0, text, col in reversed(self.toasts):
            age = now - t0
            a = min(1.0, age * 5) * min(1.0, (6 - age) * 2)
            w = min(460, ui.measure(text, 13) + 44)
            r = Rect(ui.w - T.INBOX_W - T.GAP * 2 - w - 10, y - 44 + (1 - a) * 16, w, 40)
            ui.rect(r, alpha(T.TOOLTIP, a), 10)
            ui.stroke(r, alpha(col, 0.5 * a), 10)
            ui.circle(r.x + 16, r.cy, 4, alpha(col, a))
            ui.text_fit(r.x + 28, r.cy - 9, text, w - 40, 13, alpha(T.TEXT, a))
            y -= 50


def screenshot(target: str) -> None:
    """Save the framebuffer (works around raylib's HiDPI screenshot oversizing on macOS)."""
    img = rl.load_image_from_screen()
    rw, rh = rl.get_render_width(), rl.get_render_height()
    if img.width > rw or img.height > rh:
        rl.image_crop(img, (0, img.height - rh, rw, rh))
    rl.export_image(img, target)
    rl.unload_image(img)


def run_gui(cfg: Config) -> None:
    App(cfg).run()
