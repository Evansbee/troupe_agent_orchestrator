"""The tab views: Chat, Pulse, Board, Mail, Memory, Docs, Agent — plus question cards and modals."""

from __future__ import annotations

import math
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pyray as rl

from ..roles import get_role
from ..team import ago
from . import theme as T
from .core import Rect, alpha, mix

if TYPE_CHECKING:
    from .app import App


def clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _copy(app: "App", text: str) -> None:
    """Copy `text` and toast. Also used to drain `ui.copied_text` after a `ui.markdown()` call, since
    a code-block's own copy button is handled inside markdown()'s draw loop, not exposed as a return
    value — clearing it here (whether we just set it or are draining it) keeps it from being picked
    up a second time by some other markdown() call later in the same frame."""
    app.ui.copy(text)
    app.toast("Copied", T.GREEN)
    app.ui.copied_text = None


def _drain_copy_toast(app: "App") -> None:
    """Call right after ui.markdown(): shows the toast if its code-block copy button was just clicked
    (the clipboard write already happened inside markdown()'s own draw loop)."""
    if app.ui.copied_text:
        app.toast("Copied", T.GREEN)
        app.ui.copied_text = None


# ════════════════════════════════════════════════════════════════════════════
# Question cards (right-hand "Needs you" inbox)
# ════════════════════════════════════════════════════════════════════════════
def _q_layout(app: "App", q: dict, w: float, r: Rect | None = None) -> float:
    ui, d = app.ui, app.data
    pad = 14
    iw = w - 2 * pad
    x0 = (r.x if r else 0) + pad
    y = (r.y if r else 0) + pad
    col = d.color_of(q["asker"])
    if r:
        hov = ui.hover(r)
        ui.rect(r, T.PANEL2, 10)
        ui.stroke(r, alpha(col, 0.28), 10)
        ui.rect(Rect(r.x, r.y + 10, 3, r.h - 20), col, 1.5)
        ui.avatar(x0 + 10, y + 10, 10, col, d.initials_of(q["asker"]))
        nx = x0 + 26
        nx += ui.text(nx, y + 2, d.name_of(q["asker"]), 13, col, "med") + 5
        verb = "has an idea" if q["kind"] == "idea" else "asks"
        ui.text(nx, y + 3, verb, 12, T.TEXT_FAINT)
        ts = ago(q["ts"])
        ui.text(r.r - pad - 22 - ui.measure(ts, 11), y + 4, ts, 11, T.TEXT_FAINT)
        xr = Rect(r.r - pad - 16, y, 18, 18)
        if ui.hover(xr):
            ui.rect(xr, T.HOVER, 5)
            ui.hand()
            ui.tip("Dismiss (the agent will use its judgment)")
        ui.text(xr.x + 4, xr.y + 1, "×", 15, T.TEXT_DIM, "med")
        if ui.click(xr):
            d.dismiss(q["id"])
        if ui.copy_button(Rect(xr.x - 54, y, 46, 20), hov, "Copy question"):
            parts = [q["question"]]
            if q["context"]:
                parts.append(q["context"])
            _copy(app, "\n\n".join(parts))
    y += 28
    # question
    qsize = 14
    if r:
        y += ui.text_block(x0, y, q["question"], iw, qsize, T.TEXT, "bold", 1.4)
    else:
        y += ui.text_height(q["question"], iw, qsize, "bold", 1.4)
    # context
    if q["context"]:
        y += 6
        key = f"qctx:{q['id']}"
        full = key in app.expanded
        lines = ui.wrap(q["context"], iw, 12.5)
        limit = 0 if full else 6
        if r:
            y += ui.text_block(x0, y, q["context"], iw, 12.5, T.TEXT_DIM, "ui", 1.45, limit)
        else:
            y += ui.text_height(q["context"], iw, 12.5, "ui", 1.45, limit)
        if len(lines) > 6:
            label = "Show less" if full else "Show more"
            lr = Rect(x0, y + 2, ui.measure(label, 12, "med"), 16)
            if r:
                ui.text(lr.x, lr.y, label, 12, T.ACCENT, "med")
                if ui.hover(lr):
                    ui.hand()
                if ui.click(lr):
                    app.expanded.symmetric_difference_update({key})
            y += 20
    # options
    opts = q["options"] or []
    if opts:
        y += 10
        ox = x0
        for i, opt in enumerate(opts):
            bw = min(iw, ui.button_w(opt, 12.5, 12))
            if ox + bw > x0 + iw and ox > x0:
                ox = x0
                y += 36
            if r:
                kind = "primary" if i == 0 else "default"
                if ui.button(f"q{q['id']}o{i}", Rect(ox, y, bw, 30), opt, kind, 12.5):
                    extra = ui.input_text(f"qa{q['id']}").strip()
                    d.answer(q["id"], opt + (f" — {extra}" if extra else ""))
            ox += bw + 6
        y += 30
    # free-form reply
    y += 10
    iid = f"qa{q['id']}"
    ih = ui.input_height(iid, iw, 13, 5, 10)
    if r:
        sub = ui.text_input(iid, Rect(x0, y, iw, ih), "Reply in your own words…" if opts else "Type your answer…",
                            13, pad=10)
        if sub:
            d.answer(q["id"], sub)
    y += ih + pad
    return y - (r.y if r else 0)


def question_card_height(app: "App", q: dict, w: float) -> float:
    return _q_layout(app, q, w)


def question_card(app: "App", q: dict, r: Rect) -> None:
    _q_layout(app, q, r.w, r)


# ════════════════════════════════════════════════════════════════════════════
# Chat
# ════════════════════════════════════════════════════════════════════════════
SUGGESTIONS = {
    "pm": ["I want to build…", "Here's the problem I'm trying to solve:", "What do you need from me to get started?"],
    "spec": ["Walk me through the current spec", "What's still ambiguous?", "Let's nail down the details of…"],
    "lead": ["What's the plan?", "What's blocked right now?", "Prioritize … next"],
    "designer": ["Show me the design direction", "I like the style of…"],
}


def chat_view(app: "App", r: Rect) -> None:
    ui, d = app.ui, app.data
    left, main = r.cut_left(222)
    ui.rect(Rect(left.r, left.y, 1, left.h), T.BORDER)
    # partner list
    order = sorted(d.agents, key=lambda a: ({"pm": 0, "spec": 1, "lead": 2}.get(a["role"], 3), a["id"]))
    ui.text(left.x + 16, left.y + 14, "TALK TO", 11, T.TEXT_FAINT, "bold")
    y = left.y + 36
    for a in order:
        rr = Rect(left.x + 8, y, left.w - 16, 52)
        sel = a["id"] == app.chat_with
        hov = ui.hover(rr)
        col = d.color_of(a["id"])
        if sel:
            ui.rect(rr, alpha(col, 0.12), 9)
        elif hov:
            ui.rect(rr, T.HOVER, 9)
            ui.hand()
        ui.avatar(rr.x + 20, rr.cy, 13, col, d.initials_of(a["id"]), a["state"] == "running")
        ui.text_fit(rr.x + 42, rr.y + 9, a["name"], rr.w - 70, 13, T.TEXT if sel or hov else T.TEXT_DIM, "med")
        ui.text_fit(rr.x + 42, rr.y + 28, get_role(a["role"]).title, rr.w - 70, 11, T.TEXT_FAINT)
        n = d.chat_unread.get(a["id"], 0)
        if n:
            ui.badge(rr.r - 16, rr.cy, n, T.ACCENT)
        if ui.click(rr):
            app.chat_with = a["id"]
            ui.focus = f"chat:{a['id']}"
        y += 54
    # conversation
    agent_id = app.chat_with
    a = d.agent_by_id.get(agent_id)
    if not a:
        return
    d.mark_chat_read(agent_id)
    col = d.color_of(agent_id)
    head, rest = main.cut_top(62)
    ui.avatar(head.x + 34, head.cy, 17, col, d.initials_of(agent_id), a["state"] == "running")
    ui.text(head.x + 62, head.y + 13, a["name"], 16, T.TEXT, "bold")
    role = get_role(a["role"])
    sub = role.title + (" · working" if a["state"] == "running" else " · idle") + f" · {a['backend']}" \
        + (f"/{a['model']}" if a["model"] else "")
    ui.text(head.x + 62, head.y + 35, sub, 12, T.TEXT_DIM)
    ui.hline(main.x, head.b, main.w, T.BORDER)
    iid = f"chat:{agent_id}"
    if ui.focus is None and ui.chars and not ui.cmd and app.sel_task is None and not app.new_task_open:
        ui.focus = iid
    in_w = rest.w - 48 - 90
    ih = ui.input_height(iid, in_w, 14, 8)
    compose, convo = rest.cut_bottom(ih + 28)
    # thread
    thread = _cached_thread(app, agent_id)
    sc = ui.scroll_begin(f"chat-scroll:{agent_id}", convo, stick_bottom=True)
    y = convo.y + 18 - sc.offset
    maxw = min(720.0, convo.w * 0.78)
    if not thread:
        y = _chat_empty(app, a, convo, y)
    for m in thread:
        y = _bubble(app, m, convo, y, maxw) + 14
    waiting = (a["state"] == "running" and (not thread or thread[-1]["sender"] == "human")) or (
        bool(d.limit_label(a["backend"])) and bool(thread) and thread[-1]["sender"] == "human")
    if waiting:
        y = _typing(app, a, convo, y, maxw) + 14
    ui.scroll_end(sc, y + sc.offset - convo.y + 8)
    # composer
    cr = Rect(compose.x + 24, compose.y + 8, in_w, ih)
    sub_text = ui.text_input(iid, cr, f"Message {a['name']}…   Enter to send · Shift+Enter for a new line", 14)
    br = Rect(cr.r + 10, cr.b - 40, 80, 40)
    if ui.button(f"send:{agent_id}", br, "Send", "primary", 14) and ui.input_text(iid).strip():
        sub_text = ui.input_text(iid).strip()
        ui.set_input(iid, "")
    if sub_text:
        d.send_chat(agent_id, sub_text)
        app._thread = None
        ui.scroll_to_bottom(f"chat-scroll:{agent_id}")


def _cached_thread(app: "App", agent_id: str) -> list[dict]:
    key = (agent_id, app.data.last)
    cache = getattr(app, "_thread", None)
    if cache and cache[0] == key:
        return cache[1]
    rows = app.data.chat_thread(agent_id)
    app._thread = (key, rows)
    return rows


def _chat_empty(app: "App", a: dict, area: Rect, y: float) -> float:
    ui = app.ui
    role = get_role(a["role"])
    col = app.data.color_of(a["id"])
    cy = area.y + area.h * 0.28
    ui.glow(area.cx, cy, 70, col, 0.18)
    ui.avatar(area.cx, cy, 28, col, app.data.initials_of(a["id"]))
    t = f"Start a conversation with {a['name']}"
    ui.text(area.cx - ui.measure(t, 17, "bold") / 2, cy + 44, t, 17, T.TEXT, "bold")
    ui.text(area.cx - min(520, ui.measure(role.blurb, 13)) / 2, cy + 72, ui.ellipsize(role.blurb, 520, 13), 13,
            T.TEXT_DIM)
    sx = area.cx
    sugg = SUGGESTIONS.get(a["role"], [])
    total = sum(ui.measure(s, 12, "med") + 20 + 8 for s in sugg)
    sx = area.cx - total / 2
    for s in sugg:
        clicked, w = ui.chip(f"sug:{s}", sx, cy + 104, s, False)
        if clicked:
            ui.set_input(f"chat:{a['id']}", s + (" " if s.endswith("…") is False else ""))
            if s.endswith("…"):
                ui.set_input(f"chat:{a['id']}", s[:-1])
            ui.focus = f"chat:{a['id']}"
        sx += w + 8
    return cy + 150


def _bubble(app: "App", m: dict, area: Rect, y: float, maxw: float) -> float:
    ui, d = app.ui, app.data
    human = m["sender"] == "human"
    body = m["body"]
    subject = m["subject"] if m["kind"] != "chat" else ""
    pad = 14
    inner_w = maxw - 2 * pad
    md_h = ui.md_layout(body, inner_w, 14)[1]
    name = "You" if human else d.name_of(m["sender"])
    # name + timestamp + the hover copy button, so a short reply from a long-named agent never
    # shrink-wraps narrower than its own header (which would otherwise collide with the button)
    header_w = ui.measure(name, 12, "bold") + 8 + ui.measure(clock(m["ts"]), 11) + 16 + 46
    # shrink-wrap short single-line messages
    if "\n" not in body and ui.measure(body, 14) < inner_w - 4:
        inner_w = max(ui.measure(body, 14) + 2, 160 if subject else 60,
                      ui.measure(subject, 12, "med") if subject else 0, header_w)
        md_h = ui.md_layout(body, inner_w, 14)[1]
    head_h = 20
    sub_h = 20 if subject else 0
    h = head_h + sub_h + md_h + pad * 2 - 4
    w = inner_w + 2 * pad
    x = area.r - 24 - w if human else area.x + 24
    br = Rect(x, y, w, h)
    if br.b < area.y - 50 or br.y > area.b + 50:
        return br.b
    col = d.color_of(m["sender"])
    hov = ui.hover(br)
    if human:
        ui.rect(br, alpha(T.ACCENT, 0.16), 14)
        ui.stroke(br, alpha(T.ACCENT, 0.35), 14)
    else:
        ui.rect(br, T.PANEL2, 14)
        ui.stroke(br, T.BORDER, 14)
    nx = br.x + pad
    nx += ui.text(nx, br.y + 10, name, 12, T.TEXT if human else col, "bold") + 8
    ui.text(nx, br.y + 11, clock(m["ts"]), 11, T.TEXT_FAINT)
    if ui.copy_button(Rect(br.r - 58, br.y + 8, 46, 20), hov, "Copy message"):
        _copy(app, body)
    if hov and ui.cmd and rl.is_key_pressed(rl.KeyboardKey.KEY_C):
        _copy(app, body)
    yy = br.y + 10 + head_h
    if subject:
        ui.text_fit(br.x + pad, yy, subject, inner_w, 12, T.TEXT_DIM, "med")
        yy += sub_h
    ui.markdown(br.x + pad, yy, body, inner_w, 14)
    _drain_copy_toast(app)
    return br.b


def _typing(app: "App", a: dict, area: Rect, y: float, maxw: float) -> float:
    ui, d = app.ui, app.data
    col = d.color_of(a["id"])
    limit = d.limit_label(a["backend"])
    act = limit or a["activity"] or "thinking…"
    w = min(maxw, max(200, ui.measure(act, 12) + 80))
    br = Rect(area.x + 24, y, w, 52)
    ui.rect(br, T.PANEL2, 14)
    ui.stroke(br, alpha(col, 0.35), 14)
    for i in range(3):
        ph = ui.t * 5 - i * 0.7
        ui.circle(br.x + 20 + i * 12, br.y + 18 + math.sin(ph) * 2.5, 3.2, alpha(col, 0.5 + 0.5 * max(0, math.sin(ph))))
    ui.text_fit(br.x + 58, br.y + 10, f"{a['name']} is waiting" if limit else f"{a['name']} is working", w - 70, 12, col, "med")
    ui.text_fit(br.x + 16, br.y + 30, act, w - 30, 12, T.TEXT_DIM)
    return br.b


# ════════════════════════════════════════════════════════════════════════════
# Pulse: the live network of the team
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class PulseState:
    particles: list = field(default_factory=list)  # (t0, src, dst, color)
    heat: dict = field(default_factory=dict)  # (a, b) -> last ts
    bumps: dict = field(default_factory=dict)  # node -> t
    feed_filter: str = "all"


def on_new_messages(app: "App", msgs: list[dict]) -> None:
    p = app.pulse
    now = time.time()
    for m in msgs:
        if m["sender"] == "system":
            continue
        p.particles.append((now, m["sender"], m["recipient"], app.data.color_of(m["sender"])))
        p.heat[tuple(sorted((m["sender"], m["recipient"])))] = now
    p.particles = p.particles[-60:]


def _node_positions(app: "App", r: Rect) -> dict[str, tuple[float, float]]:
    agents = app.data.agents
    pos = {"human": (r.cx, r.cy)}
    n = max(1, len(agents))
    rx, ry = r.w * 0.38, r.h * 0.36
    for i, a in enumerate(agents):
        ang = -math.pi / 2 + i * 2 * math.pi / n
        pos[a["id"]] = (r.cx + math.cos(ang) * rx, r.cy + math.sin(ang) * ry)
    return pos


def _bez(p0, p1, t, bend=0.18):
    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    cx, cy = mx - dy * bend, my + dx * bend
    u = 1 - t
    return (u * u * p0[0] + 2 * u * t * cx + t * t * p1[0], u * u * p0[1] + 2 * u * t * cy + t * t * p1[1])


def pulse_view(app: "App", r: Rect) -> None:
    ui, d, p = app.ui, app.data, app.pulse
    graph, feed = r.cut_top(r.h * 0.6)
    g = graph.inset(30, 24)
    pos = _node_positions(app, Rect(g.x, g.y + 6, g.w, g.h - 30))
    now = time.time()
    # subtle backdrop
    ui.glow(pos["human"][0], pos["human"][1], min(g.w, g.h) * 0.55, T.ACCENT, 0.06)
    # spokes + heat
    for a in d.agents:
        ax, ay = pos[a["id"]]
        ui.line(ax, ay, pos["human"][0], pos["human"][1], alpha(T.BORDER, 0.5), 1)
    for (s, t2), ts in list(p.heat.items()):
        age = now - ts
        if age > 600 or s not in pos or t2 not in pos:
            continue
        a_ = max(0.0, 1 - age / 600)
        col = mix(d.color_of(s), d.color_of(t2), 0.5)
        steps = 24
        pts = [_bez(pos[s], pos[t2], k / steps) for k in range(steps + 1)]
        for k in range(steps):
            ui.line(pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1], alpha(col, 0.12 + 0.35 * a_), 1.5 + a_)
    # particles
    alive = []
    for t0, s, dst, col in p.particles:
        targets = [x["id"] for x in d.agents if x["id"] != s] if dst == "team" else [dst]
        age = now - t0
        dur = 1.4
        if age > dur + 0.3:
            for tg in targets:
                p.bumps[tg] = t0 + dur
            continue
        alive.append((t0, s, dst, col))
        for tg in targets:
            if s not in pos or tg not in pos:
                continue
            for k in range(8):
                tt = max(0.0, min(1.0, (age - k * 0.035) / dur))
                tt = tt * tt * (3 - 2 * tt)
                x, y = _bez(pos[s], pos[tg], tt)
                if k == 0:
                    ui.glow(x, y, 16, col, 0.55)
                    ui.circle(x, y, 4.5, mix(col, T.TEXT, 0.3))
                else:
                    ui.circle(x, y, 3.6 * (1 - k / 9), alpha(col, 0.55 * (1 - k / 8)))
    p.particles = alive
    # nodes
    nq = len(d.questions)
    hx, hy = pos["human"]
    bump = max(0.0, 1 - (now - p.bumps.get("human", 0)) / 0.5)
    ui.glow(hx, hy, 56 + 10 * bump, T.HUMAN, 0.10 + 0.1 * bump)
    ui.circle(hx, hy, 30 + 3 * bump, T.PANEL3)
    ui.ring(hx, hy, 28 + 3 * bump, 30 + 3 * bump, T.HUMAN)
    ui.text_center(Rect(hx - 30, hy - 30, 60, 60), "YOU", 13, T.HUMAN, "bold")
    if nq:
        ui.badge(hx + 24, hy - 24, nq, T.PINK)
    for a in d.agents:
        x, y = pos[a["id"]]
        col = d.color_of(a["id"])
        bump = max(0.0, 1 - (now - p.bumps.get(a["id"], 0)) / 0.5)
        rad = 25 + 5 * bump
        ui.avatar(x, y, rad, col, d.initials_of(a["id"]), a["state"] == "running", not a["enabled"])
        name = a["name"]
        ui.text(x - ui.measure(name, 13, "med") / 2, y + rad + 8, name, 13, T.TEXT, "med")
        line = a["activity"] if a["state"] == "running" else (a["status"] or get_role(a["role"]).title)
        line = ui.ellipsize(line or "", 190, 11)
        ui.text(x - ui.measure(line, 11) / 2, y + rad + 27, line, 11, mix(col, T.TEXT_DIM, 0.5) if a["state"] == "running" else T.TEXT_FAINT)
        nr = Rect(x - rad, y - rad, rad * 2, rad * 2)
        if ui.hover(nr):
            ui.hand()
            ui.tip(f"{a['name']} — {get_role(a['role']).blurb}\nclick: details · right-click: chat")
        if ui.click(nr):
            app.sel_agent, app.tab = a["id"], "Agent"
        if ui.hover(nr) and ui.right_clicked:
            app.chat_with, app.tab = a["id"], "Chat"
    # legend
    ui.text(graph.x + 20, graph.y + 16, "LIVE", 11, T.TEXT_FAINT, "bold")
    ui.text(graph.x + 54, graph.y + 16, "messages fly between teammates as they happen", 11, T.TEXT_FAINT)
    # feed
    ui.hline(feed.x, feed.y, feed.w, T.BORDER)
    fh, fb = feed.cut_top(44)
    ui.text(fh.x + 20, fh.y + 15, "ACTIVITY", 11, T.TEXT_FAINT, "bold")
    fx = fh.x + 100
    kinds = [("all", "All"), ("message", "Messages"), ("task", "Tasks"), ("run", "Runs"), ("question", "Questions"),
             ("memory", "Decisions")]
    for key, label in kinds:
        clicked, w = ui.chip(f"feed:{key}", fx, fh.y + 9, label, p.feed_filter == key)
        if clicked:
            p.feed_filter = key
        fx += w + 6
    evs = d.events if p.feed_filter == "all" else [e for e in d.events if e["kind"] == p.feed_filter
                                                   or (p.feed_filter == "question" and e["kind"] == "answer")]
    sc = ui.scroll_begin("feed", fb)
    y = fb.y + 4 - sc.offset
    for e in evs:
        if y > fb.b + 20:
            y += 26
            continue
        if y > fb.y - 30:
            col = d.color_of(e["agent"])
            ui.text(fb.x + 20, y + 4, clock(e["ts"]), 11.5, T.TEXT_FAINT, "mono")
            ui.circle(fb.x + 92, y + 11, 3.5, col)
            tcol = T.RED if e["kind"] == "error" else (T.TEXT if e["significant"] else T.TEXT_DIM)
            ui.text_fit(fb.x + 104, y + 3, e["text"], fb.w - 128, 13, tcol)
        y += 26
    ui.scroll_end(sc, y + sc.offset - fb.y + 6)


# ════════════════════════════════════════════════════════════════════════════
# Board
# ════════════════════════════════════════════════════════════════════════════
COLUMNS = [("Backlog", ("backlog",)), ("Ready", ("ready",)), ("In progress", ("in_progress", "blocked")),
           ("Review", ("review", "approved")), ("Done", ("done",))]


def _task_card(app: "App", t: dict, x: float, y: float, w: float, draw: bool) -> float:
    ui, d = app.ui, app.data
    pad = 11
    iw = w - 2 * pad
    h = pad + 18
    th = ui.text_height(t["title"], iw, 13, "med", 1.4, 3)
    h += th + 8 + 18 + pad
    check = t.get("merge_check", "")
    if check:
        h += 24
    if not draw:
        return h
    r = Rect(x, y, w, h)
    hov = ui.hover(r)
    ui.rect(r, T.PANEL3 if hov else T.PANEL2, 9)
    ui.stroke(r, T.BORDER_HI if hov else T.BORDER, 9)
    scol = T.STATUS_COLORS.get(t["status"], T.TEXT_DIM)
    if t["status"] in ("blocked", "approved"):
        ui.rect(Rect(r.x, r.y + 8, 3, r.h - 16), scol, 1.5)
    cx = x + pad
    cx += ui.text(cx, y + pad, f"#{t['id']}", 11.5, T.TEXT_FAINT, "mono") + 8
    pc = T.PRIORITY_COLORS.get(t["priority"], T.TEXT_DIM)
    cx += ui.pill(cx, y + pad - 3, T.PRIORITY_LABELS.get(t["priority"], "P?"), pc, 10, h=18) + 5
    if t["status"] in ("blocked", "approved"):
        ui.pill(cx, y + pad - 3, t["status"], scol, 10, h=18)
    elif t["role"] != "builder":
        ui.pill(cx, y + pad - 3, t["role"], T.TEXT_DIM, 10, h=18)
    ty = y + pad + 20
    ui.text_block(x + pad, ty, t["title"], iw, 13, T.TEXT, "med", 1.4, 3)
    fy = ty + th + 8
    if check:
        ui.pill(x + pad, fy, check, T.ORANGE if check == "checking…" else T.RED, 10, h=18)
        fy += 24
    who = t["assignee"]
    if who:
        col = d.color_of(who)
        ui.avatar(x + pad + 8, fy + 8, 8, col, d.initials_of(who)[:2], d.agent_by_id.get(who, {}).get("state") == "running")
        ui.text_fit(x + pad + 22, fy + 1, d.name_of(who), iw * 0.6, 11.5, T.TEXT_DIM)
    else:
        ui.text(x + pad, fy + 1, "unassigned", 11.5, T.TEXT_FAINT)
    age = ago(t["updated"])
    ui.text(r.r - pad - ui.measure(age, 11), fy + 2, age, 11, T.TEXT_FAINT)
    if hov:
        ui.hand()
    if ui.click(r):
        app.sel_task = t["id"]
    return h


def board_view(app: "App", r: Rect) -> None:
    ui, d = app.ui, app.data
    area = r.inset(12, 12)
    ncol = len(COLUMNS)
    cw = (area.w - (ncol - 1) * 10) / ncol
    for i, (name, statuses) in enumerate(COLUMNS):
        col_r = Rect(area.x + i * (cw + 10), area.y, cw, area.h)
        ui.rect(col_r, T.BG2, 10)
        tasks = [t for t in d.tasks if t["status"] in statuses]
        if name == "Done":
            tasks = sorted(tasks, key=lambda t: -t["updated"])[:60]
        scol = T.STATUS_COLORS.get(statuses[0], T.TEXT_DIM)
        ui.circle(col_r.x + 16, col_r.y + 20, 4, scol)
        ui.text(col_r.x + 28, col_r.y + 12, name, 13, T.TEXT, "bold")
        ui.text(col_r.x + 34 + ui.measure(name, 13, "bold"), col_r.y + 13, str(len(tasks)), 12, T.TEXT_FAINT, "med")
        body = Rect(col_r.x, col_r.y + 40, col_r.w, col_r.h - 44)
        sc = ui.scroll_begin(f"col:{name}", body)
        y = body.y - sc.offset
        for t in tasks:
            h = _task_card(app, t, body.x + 8, y, body.w - 16, False)
            if y + h > body.y - 10 and y < body.b + 10:
                _task_card(app, t, body.x + 8, y, body.w - 16, True)
            y += h + 8
        if not tasks:
            ui.text(body.x + 16, body.y + 6, "—", 13, T.TEXT_FAINT)
        ui.scroll_end(sc, y + sc.offset - body.y + 8)


# ════════════════════════════════════════════════════════════════════════════
# Mail
# ════════════════════════════════════════════════════════════════════════════
def mail_view(app: "App", r: Rect) -> None:
    ui, d = app.ui, app.data
    bar, body = r.cut_top(48)
    x = bar.x + 16
    clicked, w = ui.chip("mf:all", x, bar.y + 11, "All", app.mail_filter is None)
    if clicked:
        app.mail_filter = None
    x += w + 6
    for a in d.agents:
        clicked, w = ui.chip(f"mf:{a['id']}", x, bar.y + 11, a["name"], app.mail_filter == a["id"], d.color_of(a["id"]))
        if clicked:
            app.mail_filter = a["id"]
        x += w + 6
        if x > bar.r - 80:
            break
    ui.hline(r.x, bar.b, r.w, T.BORDER)
    msgs = [m for m in d.messages if m["kind"] != "chat"]
    if app.mail_filter:
        msgs = [m for m in msgs if app.mail_filter in (m["sender"], m["recipient"])]
    sc = ui.scroll_begin("mail", body)
    y = body.y + 8 - sc.offset
    w = body.w - 32
    for m in msgs:
        key = f"mail:{m['id']}"
        open_ = key in app.expanded
        if open_:
            bh = ui.md_layout(m["body"], w - 60, 13.5)[1]
        else:
            bh = ui.text_height(m["body"], w - 60, 12.5, "ui", 1.45, 2)
        h = 30 + bh + 16
        rr = Rect(body.x + 16, y, w, h)
        if rr.b > body.y - 10 and rr.y < body.b + 10:
            hov = ui.hover(rr)
            ui.rect(rr, T.PANEL2 if (hov or open_) else T.PANEL, 10)
            if hov or open_:
                ui.stroke(rr, T.BORDER, 10)
            sc_col, rc_col = d.color_of(m["sender"]), d.color_of(m["recipient"])
            ui.avatar(rr.x + 22, rr.y + 22, 11, sc_col, d.initials_of(m["sender"])[:2])
            tx = rr.x + 44
            tx += ui.text(tx, rr.y + 11, d.name_of(m["sender"]), 13, sc_col, "bold") + 6
            tx += ui.text(tx, rr.y + 11, "→", 13, T.TEXT_FAINT) + 6
            tx += ui.text(tx, rr.y + 11, d.name_of(m["recipient"]), 13, rc_col, "med") + 10
            subj = m["subject"] or ""
            if m["task_id"]:
                subj = f"#{m['task_id']} · " + subj
            ts = ago(m["ts"])
            ui.text_fit(tx, rr.y + 11, subj, rr.r - tx - 90, 13, T.TEXT, "med")
            ui.text(rr.r - 16 - ui.measure(ts, 11), rr.y + 12, ts, 11, T.TEXT_FAINT)
            if not m["read_at"] and m["recipient"] != "human":
                ui.pill(rr.r - 80 - ui.measure(ts, 11), rr.y + 9, "unread", T.ACCENT, 10, h=18)
            if ui.copy_button(Rect(rr.r - 54, rr.y + 6, 46, 20), hov, "Copy message"):
                _copy(app, (m["subject"] + "\n\n" + m["body"]) if m["subject"] else m["body"])
            if open_:
                ui.markdown(rr.x + 44, rr.y + 34, m["body"], w - 60, 13.5)
                _drain_copy_toast(app)
            else:
                ui.text_block(rr.x + 44, rr.y + 34, m["body"], w - 60, 12.5, T.TEXT_DIM, "ui", 1.45, 2)
            if hov:
                ui.hand()
            if ui.click(rr):
                app.expanded.symmetric_difference_update({key})
        y += h + 6
    if not msgs:
        ui.text(body.x + 24, body.y + 20, "No mail yet — agents' messages to each other show up here.", 13, T.TEXT_FAINT)
    ui.scroll_end(sc, y + sc.offset - body.y + 8)


# ════════════════════════════════════════════════════════════════════════════
# Memory
# ════════════════════════════════════════════════════════════════════════════
KIND_COLORS = {"decision": T.ACCENT, "note": T.TEXT_DIM, "fact": T.CYAN, "idea": T.PINK, "preference": T.YELLOW}


def memory_view(app: "App", r: Rect) -> None:
    ui, d = app.ui, app.data
    bar, body = r.cut_top(48)
    x = bar.x + 16
    for key, label in [(None, "All"), ("decision", "Decisions"), ("preference", "Preferences"), ("fact", "Facts"),
                       ("idea", "Ideas"), ("note", "Notes")]:
        clicked, w = ui.chip(f"mem:{key}", x, bar.y + 11, label, app.mem_filter == key,
                             KIND_COLORS.get(key or "decision", T.ACCENT))
        if clicked:
            app.mem_filter = key
        x += w + 6
    ui.hline(r.x, bar.b, r.w, T.BORDER)
    mems = [m for m in d.memories if app.mem_filter is None or m["kind"] == app.mem_filter]
    sc = ui.scroll_begin("memory", body)
    y = body.y + 10 - sc.offset
    w = body.w - 32
    for m in mems:
        content_h = ui.md_layout(m["content"], w - 32, 13.5)[1] if m["content"] else 0
        why_h = ui.text_height("Why: " + m["rationale"], w - 32, 12.5) if m["rationale"] else 0
        h = 16 + 24 + ui.text_height(m["title"], w - 32, 14.5, "bold") + (content_h + 6 if content_h else 0) \
            + (why_h + 6 if why_h else 0) + 14
        rr = Rect(body.x + 16, y, w, h)
        if rr.b > body.y - 10 and rr.y < body.b + 10:
            hov = ui.hover(rr)
            ui.rect(rr, T.PANEL2, 10)
            kc = KIND_COLORS.get(m["kind"], T.TEXT_DIM)
            ui.rect(Rect(rr.x, rr.y + 10, 3, rr.h - 20), kc, 1.5)
            px = rr.x + 16
            px += ui.pill(px, rr.y + 12, m["kind"], kc, 10.5) + 8
            if m["scope"] == "private":
                px += ui.pill(px, rr.y + 12, "private", T.TEXT_FAINT, 10.5) + 8
            col = d.color_of(m["agent"])
            px += ui.text(px, rr.y + 14, d.name_of(m["agent"]), 12, col, "med") + 8
            ui.text(px, rr.y + 15, ago(m["ts"]), 11, T.TEXT_FAINT)
            if ui.copy_button(Rect(rr.r - 54, rr.y + 8, 46, 20), hov, "Copy memory"):
                parts = [m["title"]]
                if m["content"]:
                    parts.append(m["content"])
                if m["rationale"]:
                    parts.append("Why: " + m["rationale"])
                _copy(app, "\n\n".join(parts))
            yy = rr.y + 40
            yy += ui.text_block(rr.x + 16, yy, m["title"], w - 32, 14.5, T.TEXT, "bold")
            if m["content"]:
                yy += 6
                yy += ui.markdown(rr.x + 16, yy, m["content"], w - 32, 13.5, T.TEXT_DIM)
                _drain_copy_toast(app)
            if m["rationale"]:
                yy += 6
                ui.text_block(rr.x + 16, yy, "Why: " + m["rationale"], w - 32, 12.5, mix(kc, T.TEXT_DIM, 0.5))
        y += h + 8
    if not mems:
        ui.text(body.x + 24, body.y + 20, "No memories yet — decisions, preferences and ideas the team records show up here.",
                13, T.TEXT_FAINT)
    ui.scroll_end(sc, y + sc.offset - body.y + 8)


# ════════════════════════════════════════════════════════════════════════════
# Docs
# ════════════════════════════════════════════════════════════════════════════
_doc_cache: dict[str, tuple[float, str]] = {}


def _read_doc(p: Path) -> tuple[float, str]:
    try:
        mt = p.stat().st_mtime
    except OSError:
        return 0.0, "(missing)"
    hit = _doc_cache.get(str(p))
    if hit and hit[0] == mt:
        return hit
    text = p.read_text(errors="replace")
    _doc_cache[str(p)] = (mt, text)
    return mt, text


def docs_view(app: "App", r: Rect) -> None:
    ui, d = app.ui, app.data
    left, main = r.cut_left(250)
    ui.rect(Rect(left.r, left.y, 1, left.h), T.BORDER)
    docs = d.docs
    root = app.cfg.root
    if docs and (app.doc_sel is None or not any(str(p) == app.doc_sel for p in docs)):
        app.doc_sel = str(docs[0])
    sc = ui.scroll_begin("doclist", left)
    y = left.y + 12 - sc.offset
    last_dir = None
    for p in docs:
        rel = p.relative_to(root)
        folder = str(rel.parent) if str(rel.parent) != "." else ""
        if folder != last_dir:
            last_dir = folder
            y += 6
            ui.text(left.x + 16, y, (folder or "project").upper(), 10.5, T.TEXT_FAINT, "bold")
            y += 22
        rr = Rect(left.x + 8, y, left.w - 16, 30)
        sel = str(p) == app.doc_sel
        if sel:
            ui.rect(rr, alpha(T.ACCENT, 0.14), 7)
        elif ui.hover(rr):
            ui.rect(rr, T.HOVER, 7)
            ui.hand()
        ui.text_fit(rr.x + 12, rr.y + 7, p.name, rr.w - 24, 13, T.TEXT if sel else T.TEXT_DIM, "med" if sel else "ui")
        if ui.click(rr):
            app.doc_sel = str(p)
        y += 32
    if not docs:
        ui.text_block(left.x + 16, left.y + 16, "No docs yet. Specs (specs/), design (design/) and docs/ appear here.",
                      left.w - 32, 12.5, T.TEXT_FAINT)
    ui.scroll_end(sc, y + sc.offset - left.y + 10)
    if not app.doc_sel:
        return
    p = Path(app.doc_sel)
    mt, text = _read_doc(p)
    head, body = main.cut_top(52)
    ui.text(head.x + 24, head.y + 17, str(p.relative_to(root)), 14, T.TEXT, "bold")
    ui.text(head.x + 30 + ui.measure(str(p.relative_to(root)), 14, "bold"), head.y + 19, f"edited {ago(mt)}", 12,
            T.TEXT_FAINT)
    bw = ui.button_w("Open")
    bx = head.r - bw - 16
    if ui.button("docopen", Rect(bx, head.y + 11, bw, 30), "Open", tip="Open in your default editor"):
        subprocess.Popen(["open", str(p)])
    cw = ui.button_w("Copy doc")
    bx -= cw + 8
    if ui.button("doccopy", Rect(bx, head.y + 11, cw, 30), "Copy doc", tip="Copy the whole document"):
        _copy(app, text)
    ui.hline(main.x, head.b, main.w, T.BORDER)
    sc = ui.scroll_begin(f"doc:{p}", body)
    width = min(860.0, body.w - 64)
    h = ui.markdown(body.x + (body.w - width) / 2, body.y + 20 - sc.offset, text, width, 14.5)
    _drain_copy_toast(app)
    ui.scroll_end(sc, h + 60)


# ════════════════════════════════════════════════════════════════════════════
# Agent detail
# ════════════════════════════════════════════════════════════════════════════
_lines_cache: dict[int, list[dict]] = {}


def _run_lines(app: "App", run_id: int, live: bool) -> list[dict]:
    rows = _lines_cache.setdefault(run_id, [])
    if live or not rows:
        after = rows[-1]["id"] if rows else 0
        rows += app.data.store.run_lines(run_id, after=after)
    return rows


def agent_view(app: "App", r: Rect) -> None:
    ui, d = app.ui, app.data
    a = d.agent_by_id.get(app.sel_agent)
    if not a:
        return
    role = get_role(a["role"])
    col = d.color_of(a["id"])
    head, body = r.cut_top(108)
    ui.glow(head.x + 60, head.cy, 90, col, 0.12)
    ui.avatar(head.x + 60, head.cy, 30, col, d.initials_of(a["id"]), a["state"] == "running", not a["enabled"])
    tx = head.x + 110
    nx = tx + ui.text(tx, head.y + 20, a["name"], 21, T.TEXT, "bold") + 12
    nx += ui.pill(nx, head.y + 24, role.title, col, 11) + 6
    nx += ui.pill(nx, head.y + 24, a["backend"] + (f" · {a['model']}" if a["model"] else ""), T.TEXT_DIM, 11) + 6
    state = "working" if a["state"] == "running" else ("disabled" if not a["enabled"] else "idle")
    ui.pill(nx, head.y + 24, state, T.GREEN if state == "working" else T.TEXT_FAINT, 11)
    ui.text_fit(tx, head.y + 52, role.blurb, head.w - 560, 13, T.TEXT_DIM)
    stats = f"{a['runs']} runs · ${a['cost']:.2f} · {a['tokens'] / 1000:.0f}k tokens · session {a['session_runs']} runs" \
            f" · last ran {ago(a['last_run_at'])}"
    ui.text_fit(tx, head.y + 76, stats, head.w - 560, 12, T.TEXT_FAINT)
    # controls
    bx = head.r - 16
    buttons = [("chat", "Chat", "primary"), ("wake", "Wake now", "default")]
    if a["state"] == "running":
        buttons.append(("stop", "Stop", "danger"))
    buttons.append(("toggle", "Disable" if a["enabled"] else "Enable", "default"))
    buttons.append(("reset", "New session", "ghost"))
    for key, label, kind in reversed(buttons):
        bw = ui.button_w(label)
        bx -= bw
        tips = {"wake": "Run a check-in now", "reset": "Forget conversation history (memory is kept)",
                "stop": "Kill the current run", "toggle": "Enable/disable this agent"}
        if ui.button(f"ag:{key}", Rect(bx, head.y + 20, bw, 32), label, kind, tip=tips.get(key, "")):
            if key == "chat":
                app.chat_with, app.tab = a["id"], "Chat"
            elif key == "wake":
                d.command("poke", a["id"])
                app.toast(f"Waking {a['name']}…", col)
            elif key == "stop":
                d.command("stop", a["id"])
            elif key == "toggle":
                d.command("disable" if a["enabled"] else "enable", a["id"])
            elif key == "reset":
                d.command("reset_session", a["id"])
                app.toast(f"{a['name']} will start a fresh session next run", col)
        bx -= 8
    ui.hline(r.x, head.b, r.w, T.BORDER)
    left, right = body.cut_left(body.w * 0.63)
    ui.rect(Rect(left.r, left.y, 1, left.h), T.BORDER)
    # runs strip
    runs = d.store.runs(a["id"], limit=14) if (ui.t % 1 < 0.05 or not hasattr(app, "_runs")) else app._runs
    app._runs = runs
    if runs and a["id"] != getattr(app, "_runs_agent", None):
        app._runs_agent = a["id"]
        app.sel_run = None
    rsel = app.sel_run or (runs[0]["id"] if runs else None)
    strip, tr = left.cut_top(46)
    x = strip.x + 14
    for run in runs:
        label = f"#{run['id']} {run['reason']}"
        c = {"ok": T.GREEN, "running": T.ACCENT, "failed": T.RED, "stopped": T.YELLOW}.get(run["status"], T.TEXT_FAINT)
        clicked, w = ui.chip(f"run:{run['id']}", x, strip.y + 10, label, run["id"] == rsel, c, 11.5)
        if ui.hover(Rect(x, strip.y + 10, w, 26)):
            ui.tip(f"{run['status']} · {ago(run['started'])} · ${run['cost']:.3f}\n{(run['summary'] or '')[:300]}")
        if clicked:
            app.sel_run = run["id"]
        x += w + 6
        if x > strip.r - 120:
            break
    ui.hline(left.x, strip.b, left.w, T.BORDER)
    if rsel:
        live = any(run["id"] == rsel and run["status"] == "running" for run in runs)
        lines = _run_lines(app, rsel, live)
        _transcript(app, lines, tr, f"tr:{rsel}")
    else:
        ui.text(tr.x + 20, tr.y + 20, "No runs yet.", 13, T.TEXT_FAINT)
    _agent_side(app, a, right)


def _transcript(app: "App", lines: list[dict], r: Rect, sid: str) -> None:
    ui = app.ui
    sc = ui.scroll_begin(sid, r, stick_bottom=True)
    y = r.y + 12 - sc.offset
    w = r.w - 48
    for ln in lines:
        k, text = ln["kind"], ln["text"]
        if k == "text":
            h = ui.md_layout(text, w, 13.5)[1]
            if y + h > r.y - 20 and y < r.b + 20:
                block = Rect(r.x + 24, y, w, h)
                if ui.copy_button(Rect(block.r - 54, block.y - 2, 46, 20), ui.hover(block), "Copy"):
                    _copy(app, text)
                ui.markdown(block.x, block.y, text, w, 13.5)
                _drain_copy_toast(app)
            y += h + 10
        elif k == "tool":
            h = 24
            if y + h > r.y and y < r.b:
                tr = Rect(r.x + 20, y, min(w, ui.measure(text, 12, "mono") + 36), 22)
                ui.rect(tr, alpha(T.ACCENT, 0.10), 6)
                ui.text(tr.x + 8, y + 4, "›", 13, T.ACCENT, "monob")
                ui.text_fit(tr.x + 22, y + 4, text, w - 30, 12, mix(T.ACCENT, T.TEXT, 0.55), "mono")
                if ui.copy_button(Rect(tr.r + 6, y, 46, 20), ui.hover(tr), "Copy"):
                    _copy(app, text)
            y += h + 4
        elif k in ("result", "error", "info"):
            col = {"result": T.TEXT_FAINT, "error": T.RED, "info": T.YELLOW}[k]
            h = ui.text_height(text, w - 16, 11.5, "mono", 1.4, 3)
            if y + h > r.y and y < r.b:
                block = Rect(r.x + 36, y, w - 16, h)
                if ui.copy_button(Rect(block.r - 54, block.y - 2, 46, 20), ui.hover(block), "Copy"):
                    _copy(app, text)
                ui.text_block(block.x, block.y, text, w - 16, 11.5, col, "mono", 1.4, 3)
            y += h + 8
    if not lines:
        ui.text(r.x + 24, r.y + 16, "Waiting for output…", 13, T.TEXT_FAINT)
    ui.scroll_end(sc, y + sc.offset - r.y + 12)


def _agent_side(app: "App", a: dict, r: Rect) -> None:
    ui, d = app.ui, app.data
    sc = ui.scroll_begin(f"side:{a['id']}", r)
    x, w = r.x + 18, r.w - 36
    y = r.y + 16 - sc.offset
    tasks = d.open_tasks_of(a["id"])
    ui.text(x, y, "TASKS", 11, T.TEXT_FAINT, "bold")
    y += 22
    for t in tasks:
        y += _task_card(app, t, x, y, w, True) + 8
    if not tasks:
        ui.text(x, y, "No open tasks.", 12.5, T.TEXT_FAINT)
        y += 24
    y += 12
    ui.text(x, y, "MEMORY", 11, T.TEXT_FAINT, "bold")
    y += 22
    mems = [m for m in d.memories if m["agent"] == a["id"]][:12]
    for m in mems:
        kc = KIND_COLORS.get(m["kind"], T.TEXT_DIM)
        ui.circle(x + 4, y + 8, 3, kc)
        y += ui.text_block(x + 14, y, m["title"], w - 14, 12.5, T.TEXT, "med", 1.4)
        if m["rationale"]:
            y += ui.text_block(x + 14, y, m["rationale"], w - 14, 11.5, T.TEXT_FAINT, "ui", 1.4, 2)
        y += 8
    if not mems:
        ui.text(x, y, "Nothing recorded yet.", 12.5, T.TEXT_FAINT)
        y += 24
    y += 12
    ui.text(x, y, "RECENT MAIL", 11, T.TEXT_FAINT, "bold")
    y += 22
    mail = [m for m in d.messages if a["id"] in (m["sender"], m["recipient"]) and m["kind"] != "chat"][:10]
    for m in mail:
        other = m["recipient"] if m["sender"] == a["id"] else m["sender"]
        arrow = "→" if m["sender"] == a["id"] else "←"
        ui.text(x, y, f"{arrow} {d.name_of(other)}", 12, d.color_of(other), "med")
        ui.text(x + w - ui.measure(ago(m["ts"]), 11), y + 1, ago(m["ts"]), 11, T.TEXT_FAINT)
        y += 18
        y += ui.text_block(x, y, m["subject"] or m["body"], w, 12, T.TEXT_DIM, "ui", 1.4, 2) + 8
    if not mail:
        ui.text(x, y, "No mail.", 12.5, T.TEXT_FAINT)
        y += 24
    ui.scroll_end(sc, y + sc.offset - r.y + 16)


# ════════════════════════════════════════════════════════════════════════════
# Modals: task detail, new task
# ════════════════════════════════════════════════════════════════════════════
def _scrim(app: "App", key: str) -> None:
    ui = app.ui
    ui.rect(Rect(0, 0, ui.w, ui.h), (4, 5, 9, 170))


def draw_modals(app: "App") -> None:
    ui = app.ui
    if app.sel_task is not None:
        ui.modal = ui.layer = "task"
        _scrim(app, "task")
        _task_modal(app)
        ui.layer = None
    elif app.new_task_open:
        ui.modal = ui.layer = "newtask"
        _scrim(app, "newtask")
        _new_task_modal(app)
        ui.layer = None
    else:
        ui.modal = None


def _task_modal(app: "App") -> None:
    ui, d = app.ui, app.data
    t = next((x for x in d.tasks if x["id"] == app.sel_task), None)
    if not t:
        app.sel_task = None
        return
    w, h = min(820, ui.w - 120), min(ui.h - 100, 860)
    r = Rect((ui.w - w) / 2, (ui.h - h) / 2, w, h)
    ui.panel(r, T.PANEL, 14, T.BORDER_HI)
    if ui.clicked and not r.contains(ui.mouse):
        app.sel_task = None
        ui.click_consumed = True
        return
    head, rest = r.cut_top(70)
    ui.text(head.x + 24, head.y + 16, f"#{t['id']}", 13, T.TEXT_FAINT, "mono")
    ui.text_fit(head.x + 24, head.y + 36, t["title"], head.w - 140, 18, T.TEXT, "bold")
    scol = T.STATUS_COLORS.get(t["status"], T.TEXT_DIM)
    ui.pill(head.x + 70, head.y + 14, t["status"].replace("_", " "), scol, 11)
    if ui.button("tm:close", Rect(head.r - 50, head.y + 16, 34, 30), "×", "ghost", 18):
        app.sel_task = None
    foot, body = rest.cut_bottom(64)
    ui.hline(r.x, foot.y, r.w, T.BORDER)
    sc = ui.scroll_begin(f"taskm:{t['id']}", body)
    x, bw = body.x + 24, body.w - 48
    y = body.y + 8 - sc.offset
    meta = [("Priority", T.PRIORITY_LABELS.get(t["priority"], "?")), ("Role", t["role"]),
            ("Assignee", d.name_of(t["assignee"]) if t["assignee"] else "—"),
            ("Reviewer", d.name_of(t["reviewer"]) if t["reviewer"] else "—"),
            ("Created", f"{d.name_of(t['created_by'])}, {ago(t['created'])}"),
            ("Branch", t["branch"] or "—")]
    if t["depends_on"]:
        meta.append(("Depends on", ", ".join(f"#{x_}" for x_ in t["depends_on"])))
    if t["territory"]:
        meta.append(("Territory", t["territory"]))
    colw = bw / 3
    for i, (k, v) in enumerate(meta):
        mx = x + (i % 3) * colw
        my = y + (i // 3) * 44
        ui.text(mx, my, k.upper(), 10, T.TEXT_FAINT, "bold")
        ui.text_fit(mx, my + 16, v, colw - 12, 13, T.TEXT, "med")
    y += ((len(meta) + 2) // 3) * 44 + 8

    def section(title: str, md: str, color: tuple = T.TEXT) -> None:
        nonlocal y
        if not md:
            return
        ui.text(x, y, title.upper(), 10.5, T.TEXT_FAINT, "bold")
        y += 20
        y += ui.markdown(x, y, md, bw, 13.5, color) + 18
        _drain_copy_toast(app)

    section("Description", t["description"])
    section("Acceptance criteria", t["acceptance"])
    section("Builder summary", t["result"])
    section("Review notes", t["review_notes"], T.TEXT_DIM)
    notes = d.store.task_notes(t["id"]) if ui.t % 1 < 0.05 or not hasattr(app, "_tnotes") or app._tnotes[0] != t["id"] \
        else app._tnotes[1]
    app._tnotes = (t["id"], notes)
    if notes:
        ui.text(x, y, "NOTES", 10.5, T.TEXT_FAINT, "bold")
        y += 20
        for n in notes:
            ui.text(x, y, f"{d.name_of(n['agent'])} · {ago(n['ts'])}", 11.5, d.color_of(n["agent"]), "med")
            y += 18
            y += ui.markdown(x, y, n["text"], bw, 13, T.TEXT_DIM) + 10
            _drain_copy_toast(app)
    y += 6
    nid = f"tnote:{t['id']}"
    nh = ui.input_height(nid, bw, 13, 5, 10)
    sub = ui.text_input(nid, Rect(x, y, bw, nh), "Add a note (the assignee gets it as mail)…", 13, pad=10)
    if sub:
        d.store.task_note(t["id"], "human", sub)
        if t["assignee"]:
            d.store.send("human", t["assignee"], sub, subject=f"Note on #{t['id']}", task_id=t["id"])
        else:
            d.store.event("human", "task", f"You noted on #{t['id']}: {sub[:100]}", ref=f"task:{t['id']}")
        app._tnotes = (t["id"], d.store.task_notes(t["id"]))
    y += nh + 16
    ui.scroll_end(sc, y + sc.offset - body.y)
    # actions
    fx = foot.x + 24
    acts = []
    if t["status"] in ("backlog", "blocked"):
        acts.append(("ready", "Approve → Ready", "primary"))
    if t["status"] in ("ready", "in_progress"):
        acts.append(("block", "Block", "default"))
    if t["status"] == "review":
        acts.append(("approve", "Approve & merge", "primary"))
    if t["status"] in ("done", "cancelled"):
        acts.append(("reopen", "Reopen", "default"))
    acts += [("up", "Priority ↑", "default"), ("down", "Priority ↓", "default")]
    if t["status"] not in ("done", "cancelled"):
        acts.append(("cancel", "Cancel task", "danger"))
    for key, label, kind in acts:
        bw2 = ui.button_w(label)
        if ui.button(f"tm:{key}", Rect(fx, foot.y + 14, bw2, 34), label, kind):
            if key == "ready":
                d.set_task(t["id"], status="ready", attempts=0, next_attempt_at=0)
            elif key == "block":
                d.set_task(t["id"], status="blocked")
            elif key == "approve":
                d.store.task_note(t["id"], "human", "Approved by the human.")
                d.set_task(t["id"], status="approved")
            elif key == "reopen":
                d.set_task(t["id"], status="ready", attempts=0)
            elif key == "up":
                d.set_task(t["id"], priority=max(0, t["priority"] - 1))
            elif key == "down":
                d.set_task(t["id"], priority=min(3, t["priority"] + 1))
            elif key == "cancel":
                d.set_task(t["id"], status="cancelled")
        fx += bw2 + 8


def _new_task_modal(app: "App") -> None:
    ui, d = app.ui, app.data
    w, h = min(680, ui.w - 120), 470
    r = Rect((ui.w - w) / 2, (ui.h - h) / 2, w, h)
    ui.panel(r, T.PANEL, 14, T.BORDER_HI)
    if ui.clicked and not r.contains(ui.mouse):
        app.new_task_open = False
        ui.click_consumed = True
        return
    x, iw = r.x + 24, r.w - 48
    ui.text(x, r.y + 22, "New task", 18, T.TEXT, "bold")
    ui.text(x, r.y + 48, "Goes straight to Ready; the orchestrator dispatches it to a free agent of that role.", 12.5,
            T.TEXT_DIM)
    y = r.y + 80
    ui.text(x, y, "TITLE", 10.5, T.TEXT_FAINT, "bold")
    ui.text_input("nt_title", Rect(x, y + 18, iw, 42), "What needs doing?", 14, multiline=False)
    y += 76
    ui.text(x, y, "BRIEF", 10.5, T.TEXT_FAINT, "bold")
    ui.text_input("nt_desc", Rect(x, y + 18, iw, 130), "Goal, context, acceptance criteria…", 13.5,
                  submit_on_enter=False)
    y += 166
    ui.text(x, y, "ROLE", 10.5, T.TEXT_FAINT, "bold")
    cx = x + 60
    role = getattr(app, "_nt_role", "builder")
    for rk in ("builder", "designer", "spec", "qa", "lead", "pm"):
        clicked, cw = ui.chip(f"nt_role:{rk}", cx, y - 6, rk, role == rk)
        if clicked:
            app._nt_role = rk
        cx += cw + 6
    y += 34
    ui.text(x, y, "PRIORITY", 10.5, T.TEXT_FAINT, "bold")
    cx = x + 60
    pri = getattr(app, "_nt_pri", 2)
    for p in range(4):
        clicked, cw = ui.chip(f"nt_pri:{p}", cx, y - 6, T.PRIORITY_LABELS[p], pri == p, T.PRIORITY_COLORS[p])
        if clicked:
            app._nt_pri = p
        cx += cw + 6
    title = ui.input_text("nt_title").strip()
    if ui.button("nt:cancel", Rect(r.r - 24 - 200, r.b - 54, 90, 36), "Cancel", "ghost"):
        app.new_task_open = False
    if ui.button("nt:create", Rect(r.r - 24 - 100, r.b - 54, 100, 36), "Create", "primary", disabled=not title):
        tid = d.add_task(title, ui.input_text("nt_desc").strip(), getattr(app, "_nt_role", "builder"),
                         getattr(app, "_nt_pri", 2))
        ui.set_input("nt_title", "")
        ui.set_input("nt_desc", "")
        app.new_task_open = False
        app.toast(f"Created task #{tid}", T.GREEN)
