# troupe GUI — design system

Source of truth for the visual language of `src/troupe/gui/`. This documents what the code in
`theme.py` / `core.py` / `views.py` / `app.py` actually does today — treat divergence between this
doc and the code as a bug in one of the two. See specs/40-gui.md for behavioral requirements.

## Voice

Midnight command center. Dark, dense, a little bit alive — glows and particles imply the team is
working even when you're not looking directly at it. Never flashy for its own sake: motion always
carries a signal (running, new, selected, stale).

## Color

### Base surfaces (`theme.py`)
| Token | RGB | Use |
|---|---|---|
| `BG` | 9,11,16 | window clear color |
| `BG2` | 12,15,21 | board column background, top-bar gradient end |
| `PANEL` | 16,19,27 | default panel fill (sidebar, inbox, center, modals) |
| `PANEL2` | 22,26,36 | card fill (resting), chat bubble (agent), unselected chip |
| `PANEL3` | 30,35,48 | card/chip fill (hover), code fences |
| `HOVER` | white @ 5% | generic hover wash over any surface |
| `INPUT` / `INPUT_FOCUS` | 13,16,23 / 15,18,27 | text input fill, resting/focused |
| `TOOLTIP` | 28,32,44 @ 98% | tooltip + toast fill |
| `BORDER` / `BORDER_HI` | 34,39,54 / 52,59,80 | hairlines; `_HI` on hover/focus/selected |

### Text
| Token | Use |
|---|---|
| `TEXT` | primary text, active tab, card titles |
| `TEXT_DIM` | secondary text, unselected tab, body copy in bubbles |
| `TEXT_FAINT` | metadata — timestamps, counts, placeholders, section labels |
| `ON_ACCENT` | text/icons drawn on a filled accent surface (always white) |

### Accent & semantic
| Token | RGB | Use |
|---|---|---|
| `ACCENT` | 124,140,255 (indigo) | primary actions, human chat bubbles, focus rings, active tab underline |
| `ACCENT2` | 167,139,250 (violet) | reserved — currently unused outside role palette overlap with designer role |
| `GREEN` | 52,211,153 | success / done / "Live" / working badges |
| `YELLOW` | 250,204,21 | paused / warning |
| `ORANGE` | 251,146,60 | throttled / review-needed / "parked, owes work" |
| `RED` | 248,113,113 | blocked / error / danger actions |
| `CYAN` | 56,189,248 | ready status |
| `PINK` | 244,114,182 | "needs you" badge, question cards |
| `CODE_BG` / `CODE_TEXT` | 24,28,40 / 196,205,255 | markdown code fences and inline code |
| `HUMAN` | 236,239,247 | the human's node/avatar (near-white, deliberately outside the role palette) |

Semantic colors are reused directly as status colors (`STATUS_COLORS`, `PRIORITY_COLORS` in
`theme.py`) — never invent a new hue for a new status; pick the closest existing meaning.

### Role colors (`roles.py`, not `theme.py`)
Each role owns one identity color, used everywhere that role appears (avatars, chat bubbles,
pills, graph nodes, sidebar). `data.color_of(agent_id)` resolves an agent to its role color;
`human` and `system` are special-cased to `T.HUMAN` / `T.TEXT_FAINT`.

| Role | Color |
|---|---|
| Team Lead | `(245,184,75)` amber |
| Product Manager | `(244,114,182)` pink |
| Spec Writer | `(56,189,248)` cyan |
| Designer | `(167,139,250)` violet |
| Builder | `(52,211,153)` green |
| QA / Tester | `(251,146,60)` orange |
| Gadfly | `(248,113,113)` red |
| Architect | `(191,148,82)` bronze | *(proposed — not yet in `roles.py`; see below)* |
| Researcher | `(192,106,224)` orchid | *(proposed — not yet in `roles.py`; see below)* |

**Architect and Researcher** (spec msg #192): `roles.py` doesn't define these roles yet, so nothing
assigns them a color — the Mac spec falls back to whatever the API reports until one exists. Proposing
bronze and orchid here now so both docs and the API have a real value to build against: bronze sits
apart from Lead's amber (deeper, more muted, not mistakable for it at a glance) and reads as
"structural/foundational," fitting the Architect's gatekeeping role; orchid fills the actual gap in the
wheel between PM's pink and Designer's violet rather than crowding either. Neither collides with an
existing role, `ACCENT`, or `ACCENT2`. Once these roles land in `roles.py`, use these values verbatim.

Role colors and semantic colors share hues (green = builder *and* success, orange = QA *and*
throttled, red = gadfly *and* error). This is intentional economy, not collision — the two systems
never appear ambiguously in the same glyph (a role color always sits on an avatar/name; a semantic
color always sits on a status pill/dot).

## Type

Faces (`core.py FACES`, loaded via `ui.font(face, size)`):
- `ui` — Inter Regular. Default body text.
- `med` — Inter Medium. Labels, buttons, names, anything that needs to stand slightly forward.
- `bold` — Inter SemiBold. Titles, headings, emphasis (`**bold**` in markdown).
- `mono` / `monob` — JetBrains Mono. IDs (`#12`), tool-call lines, code, transcript metadata.

Sizes in use today are ad hoc (anywhere from 10 to 21px, in ~0.5px steps, chosen per call site).
There is no `theme.py` type-scale constant yet — see polish task below. Until that lands, match the
nearest existing usage rather than picking a new number.

Since #23, every size below is the **logical, un-zoomed baseline** — a global zoom factor (default
115%, ⌘=/⌘-/⌘0) scales everything from these numbers at draw time. Design against this table as-is;
don't hand-adjust a size to compensate for zoom, that's exactly what the global factor is for.

| Role | ~Size | Face | Example |
|---|---|---|---|
| Section label (all-caps) | 10–11 | bold | "TEAM", "TASKS", "ROLE" |
| Metadata / timestamp | 11–12 | ui/mono | `ago()` strings, run ids |
| Body / label | 12.5–13.5 | ui/med | card titles, bubble text, buttons |
| Emphasized body | 14 | ui/med | chat composer, question text |
| Card/section title | 14.5–16 | bold | memory card title, tab labels |
| View heading | 17–19 | bold | chat empty-state headline, logo |
| Page heading | 21 | bold | agent-detail name |

Line height: body text wraps at `1.45×` size (`text_block` default); markdown paragraphs at `1.5×`;
headings tighter at `1.35×`.

## Spacing & radius

Layout metrics live in `theme.py`: `TOP_H=56`, `SIDEBAR_W=272`, `INBOX_W=360`, `GAP=10` (the gutter
between shell regions). Card/panel internal padding is consistently `12–16px`; list-row gaps are
`6–10px`.

Corner radius has one named token, `RADIUS=12`, but it's used almost nowhere directly — every widget
picks its own literal instead:

| Radius | Where |
|---|---|
| 3 | inline code background, checkbox in markdown |
| 6 | tooltips |
| 7 | buttons |
| 9 | chips, task/memory cards, board columns, text inputs |
| 10 | panels (sidebar/inbox/center), mail rows |
| 12 | `T.RADIUS` — declared, effectively unused |
| 13 | pills computed as `h/2` (fully rounded), active chip |
| 14 | chat bubbles, modals |
| `h/2` | pills, badges — always fully rounded |

Treat this table as the de facto scale (buttons=7, list-item=9, container=10, bubble/modal=14) until
the polish task below turns it into real tokens.

## Motion

Everything animates through `ui.ease(key, target, speed)` — an exponential approach
(`lerp(v, target, dt·speed)`), never a fixed-duration tween. Higher `speed` = snappier.

| Speed | Use |
|---|---|
| 8 | scrollbar thumb fade |
| 14 | button hover fill |
| 16 | card hover lift, panel-scroll offset |
| 18 | tab underline slide, sidebar card hover |

Ambient motion (not input-driven) is layered separately:
- **Working ring**: a rotating arc + soft pulse (`sin(t·3.2)`) around any avatar mid-run (`ui.avatar(..., running=True)`).
- **Pulse graph particles**: messages fly as easing dots along a quadratic bezier between sender and
  recipient over 1.4s, leaving a heat-glow on the edge that fades over 10 minutes.
- **Live pill pulse**: the top-bar "Live" dot breathes (`sin(t·3)`) — the only chrome-level idle animation.
- **Toasts**: fade+slide in/out over the first/last ~0.2–0.5s of a 6s lifetime.

The app throttles to 20fps when idle (`ui.activity` > 4s and nothing running/animating) and 60fps
otherwise — animations must stay legible at both.

## Components

- **Panel** (`ui.panel`) — filled rounded rect + 1px border. The base container for every region.
- **Button** (`ui.button`) — kinds `primary` (filled accent), `default` (panel-tinted), `ghost`
  (transparent → hover wash), `danger` (red-tinted). Hover eases a fill/border blend; label always
  centered.
- **Pill** (`ui.pill`) — static, fully-rounded label chip. `filled=True` for a solid-color badge
  (priority on modals), `filled=False` (default) for a 16%-alpha tint (status, role tags). Not
  interactive.
- **Chip** (`ui.chip`) — interactive, fully-rounded toggle/filter button with an `active` state
  (colored tint + stroke) vs resting `PANEL2`/`PANEL3` on hover. Used for filters (Mail, Memory,
  Pulse feed), suggestions (chat empty state), and inline pickers (new-task role/priority).
- **Badge** (`ui.badge`) — small filled numeric circle for unread/pending counts, overlaid on an
  avatar or label corner.
- **Avatar** (`ui.avatar`) — ringed circle with role-colored initials; grows a pulsing glow + rotating
  arc while `running`, desaturates toward `BG` when `dim` (disabled).
- **Card** — no single `ui.card()`; task cards (`views._task_card`) and memory cards
  (`views.memory_view`) share the pattern: `PANEL2`→`PANEL3` on hover, `BORDER`→`BORDER_HI`, a 3px
  colored accent bar on the left edge when status/kind matters.
- **Chat bubble** (`views._bubble`) — right-aligned accent-tinted for the human, left-aligned
  `PANEL2` for agents; shrink-wraps to content width for short single-line messages instead of
  filling the max bubble width.
- **Tooltip** (`ui.tip` / `_draw_tooltip`) — dark rounded box following the cursor, shown on hover;
  used for keyboard shortcuts, truncated text, and status detail.
- **Toast** (`app.draw_toasts`) — bottom-right stack above the inbox column, colored dot + text,
  auto-dismiss after 6s.
- **Scrollbar** (`ui.scroll_end`) — thin inset thumb, only visible on hover/drag, everywhere content
  scrolls.
- **Text input** (`ui.text_input`) — multiline-capable, Enter submits / Shift+Enter newlines, focus
  ring eases border+fill toward `ACCENT`.
- **Markdown** (`ui.markdown` / `md_layout`) — headings, lists (bulleted/numbered/checkbox), block
  quotes, code fences, tables, inline `**bold**`/`` `code` ``/`[link](...)`/`_em_`. This is the
  rendering substrate for chat, mail, memory, docs, and task detail bodies — any copy in the app can
  assume markdown is safe to use.
- **Modal** (`views._task_modal`, `_new_task_modal`) — centered panel over a scrim, dismiss on
  click-outside or Esc.

## Upcoming interactions (visual treatment)

Specced behavior for REQ-GUI-020/022/023/024/027, REQ-COM-024, and REQ-ENG-016. Each reuses existing
components/primitives — no new visual language.

### REQ-GUI-020 — board drag-and-drop
- Below the 4px move threshold it's a click (opens the task modal, unchanged). Past it, the grabbed
  card becomes a **ghost**: the same `_task_card` render, at `alpha 0.92`, following the cursor with
  a fixed grab-point offset, plus a soft `ui.glow` behind it in the card's status color so it reads
  as "lifted" off the board. The card's original slot in its column collapses (siblings reflow) —
  don't leave a dashed placeholder, that's extra chrome for no signal.
- The column currently under the cursor gets the same "active" treatment chips use: background tint
  `alpha(ACCENT, 0.06)` over the column rect (`T.BG2` base) plus a `1px` `alpha(ACCENT, 0.4)` inner
  stroke. Only one column highlights at a time.
- Esc cancels: ghost eases back to its origin card position (reuse `ui.ease`) and fades out; no status
  change, no event.
- On drop: log the event exactly as existing task events render in Pulse's activity feed ("human moved
  #6 Ready → In progress") — same `d.color_of`/mono-timestamp styling as other feed rows.

### REQ-GUI-022 — prompt inspector
- A two-way toggle, **Transcript | Prompt**, rendered as two `ui.chip`s immediately right of the run
  chips strip (`views.agent_view`, same row, right-aligned). Selection persists per session (a field on
  `App`, not per-run) — switching runs keeps you on whichever tab you were viewing.
- Prompt view reuses the transcript pane's rect. Two collapsible sections, **System prompt** and **Wake
  prompt**, each a header row (chevron `▸`/`▾` + label, `TEXT_FAINT` bold 10.5 — same treatment as
  Memory/Agent-side section labels) followed by a `CODE_BG`-filled monospace block (same look as
  markdown code fences: `radius 6`, `CODE_TEXT` @ `12.5–13`). Collapsed by default is fine for Wake
  prompt (usually short-lived context); System prompt defaults open.

### REQ-GUI-023 — copy affordance
- Every chat bubble, transcript text block, tool-result block, and doc code fence gets a small ghost
  icon button in its top-right corner, hidden until hover — identical interaction pattern to the "×"
  dismiss control in question cards (`views._q_layout`): `18×18` hit target, `T.HOVER` wash on hover,
  hand cursor, tooltip "Copy". Use a simple two-rectangle "copy" glyph (or the existing `⧉`-style
  monospace glyph if the font covers it) at `13–14px`, `TEXT_DIM` → `TEXT` on hover.
- On click: copy raw text/markdown to clipboard via `rl.set_clipboard_text`, and fire the existing toast
  system with `"Copied"` at `GREEN`. No new toast variant needed — same shape, just a short-lived,
  low-emphasis message.

### REQ-GUI-024 — search palette (⌘K)
- A modal, but **top-anchored** rather than centered (command-palette convention, distinct from the
  centered task/new-task modals so it doesn't feel like "opening a record") — panel starts ~15% down
  from the top, same `T.PANEL` / `BORDER_HI` / `radius 14` treatment as other modals, `560–640px` wide.
- A single `ui.text_input`-style field at the top (no placeholder box chrome beyond the input itself —
  no separate "search" label). Below it, grouped results: a `TEXT_FAINT` bold 10.5 group header (Tasks
  / Messages / Memories / Docs / Agents — same label style as Memory/Docs section headers), then up to
  5 rows per group. Row = icon-or-avatar + primary label + faint secondary metadata, same row anatomy
  as the sidebar/doc-list selected-row pattern (`alpha(ACCENT,0.14)` background on keyboard-selected
  row, `T.HOVER` on mouse-hover). ↑/↓ move the keyboard selection; Enter activates it; Esc or an
  outside click closes, matching the existing modal dismiss pattern.

### REQ-GUI-027 — window title
- Not a drawn surface — `rl.set_window_title`. Format: `troupe — <project>`, or `(N) troupe — <project>`
  when `N` questions are open (`len(d.questions)`, the same count driving the inbox badge). Update
  wherever `d.questions` changes, not every frame.

### REQ-COM-024 — Team room bubble color
- "Team" is a new first entry in the Chat partner list (same row treatment as any agent row, but its
  avatar is a small constellation glyph — reuse the top-bar logo mark at avatar scale — rather than
  initials, since it represents no single agent).
- Room bubbles are a **colored variant** of the existing agent chat bubble (`views._bubble`): instead
  of a flat `PANEL2` fill, use `alpha(sender_color, 0.14)` fill + `alpha(sender_color, 0.32)` stroke —
  the same formula already used for the human's `ACCENT`-tinted bubble, just keyed to whichever
  teammate spoke. Name label keeps rendering in the role color as it already does; this just extends
  that identity cue to the bubble body so a fast-scrolling room thread stays scannable by color, not
  just by re-reading names.
- `@mentions` inside room message text render as `ACCENT`-colored inline text (treat as an implicit
  markdown span, styled like the `link` inline style in `ui._spans`) — including unknown mentions,
  which still render colored even though they're inert.

### REQ-GUI-029 — engine pill service states
Extends the existing top-bar engine-state pill (today: Live / Paused / Throttled / Engine offline)
with the states the service model adds. Same shape throughout — `alpha(color, 0.14)` fill, colored dot,
label — only the color/label/action change:

| State | Color | Label | Action |
|---|---|---|---|
| Engine offline | `RED` | "Engine offline" | **Start team** button inline in the pill |
| Reloading | `YELLOW` | "Reloading… 2 runs draining" (live count) | — |
| Restarting (after a crash) | `YELLOW` | "Restarting…" | — |
| Crashed (crash loop) | `RED` | "Crashed — see engine.log" | **Start team** button inline in the pill |
| Stopped (kill switch, REQ-SAFE-010) | `RED`, no pulse — deliberately inert, not "alarm-flashing" | "Stopped" | **Resume** button inline in the pill, human-only |

Reloading/Restarting are transient and non-actionable (the engine is already handling it) — no button,
just a status label, same as today's "Throttled" pill. Offline, Crashed, and Stopped all need a way
back in, so each carries an inline primary-button treatment in the pill (matches `design/projects.md`'s
rail "Start" action for the same states, applied here to the *current* project's own pill).

**Stopped takes precedence over every other state** (REQ-GUI-029) — if the kill switch has fired, the
pill shows Stopped regardless of what else might be true underneath (reloading, throttled, etc.), since
nothing else matters until the human resumes.

**Stop everything** (REQ-SAFE-010, ⌘⇧.) is a separate, always-visible top-bar control — not a pill
state, a permanent button (small, `danger`-kind ghost button, far right of the top bar, always present
regardless of engine state) that fires the kill switch for the active project. Deliberately not hidden
inside a menu: this is a safety control, and safety controls that require hunting for them are safety
controls that don't get used in time.

### REQ-ENG-016 — rate-limited backend pill
- Reuses the existing top-bar engine-state pill component (`app.draw_top`'s Live/Paused/Throttled
  pill) verbatim — same shape, same `alpha(color, 0.14)` fill + colored dot + label — one instance per
  currently-limited backend, laid out in a row immediately after the engine-state pill. Color: `ORANGE`
  (matches "Throttled" convention already established for capacity pressure; reserve `RED` for
  failure/blocked states, not rate limiting). Label: `"<backend> limited · resets HH:MM"`. A pill
  disappears the frame its backend's limit clears — no exit animation needed, these are low-frequency.
- In Chat, a working bubble for an agent on a limited backend shows the same limited-until copy inline
  (`_typing`'s existing bubble, swap the activity line for `"Claude limited until 14:05"` in place of
  `"thinking…"`) rather than a separate banner — one state, shown where the user is already looking.

## Known gaps (feeds the polish backlog below)

- No real type-scale or radius-scale constants — both are "whatever the nearest call site used."
- `STATUS_COLORS["backlog"]` and `STATUS_COLORS["cancelled"]` are both `TEXT_FAINT` — visually
  identical despite meaning opposite things.
- Empty states are inconsistent in polish: Chat and the human inbox get an illustrated
  glow+icon+copy treatment; Mail, Memory, Docs, and empty Board columns get a single gray line.
