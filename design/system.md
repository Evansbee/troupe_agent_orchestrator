# troupe GUI — design system

Source of truth for the visual language of `src/troupe/gui/`. This documents what the code in
`theme.py` / `core.py` / `views.py` / `app.py` actually does today — treat divergence between this
doc and the code as a bug in one of the two. See specs/40-gui.md for behavioral requirements.

## Voice

Midnight command center. Dark, dense, a little bit alive — glows and particles imply the team is
working even when you're not looking directly at it. Never flashy for its own sake: motion always
carries a signal (running, new, selected, stale).

## Countdown copy: relative, never clock time (#105)

Human (13:55, 2026-09-24): usage/cap resets must say the time *until* reset, not the time *of* reset —
"in 4 hours", not "2:15pm". This is a system-wide copy rule, not GUI-only: every countdown anywhere in the
app — usage/rate-limit resets, pacing ("next check-in in 9m"), retry backoff — uses the same format,
whether it renders in the raylib GUI, the TUI, `troupe status`, or an OS notification.

Format, recomputed live (not frozen at render time):
- **under 1h:** minutes only — `in 12m`
- **1h–6h:** hours + minutes — `in 4h 20m`
- **6h+:** hours only, drop the minutes — `in 4h`

Same idea as the existing `ago()` helper (relative, not absolute) just pointed at the future — a
`countdown()`-style helper belongs next to `ago()` (`team.py`) so every surface computes it the same way
instead of each one formatting `reset_at - now` by hand. Task #105 (P2, backlog, after the TUI cutover)
wires this into the TUI/GUI/status/notifications; this section is the copy contract it implements against.
Every existing example string in this doc and `design/tui.md` that showed a clock-time reset (`14:05`) has
been updated to this format already, so there's nothing left to reconcile when #105 lands.

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
- **Question card** (`views._q_layout`) — the "Needs you" panel's base unit: avatar + asker name,
  question/idea text, option buttons, a free-text reply input, dismiss control. Everything the human
  is asked to weigh in on that isn't a full task/decision reuses this shell rather than inventing a
  new one.
- **Approval card** (REQ-SAFE-020/021, `#42`) — a question-card variant for protected-path diff
  approval: same shell (avatar/title/dismiss position), with the free-text reply and option buttons
  replaced by a diff summary (protected files, +/− counts), an "Open full diff" link, and Approve /
  Reject-with-note in place of generic options. Reusing the question-card shell here is deliberate,
  not incidental — a safety-critical approval should look like the same kind of thing the human
  already knows how to act on, not a novel, unfamiliar control.
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

## Needs-you cards per notifier kind (#97)

Today only `question`/`idea`/`safety` (backed by the `questions` table) render as cards in the Needs-you
panel; the other nine kinds `notify.py` already fires OS notifications for — `blocked`, `check_failed`,
`backoff`, `stalled`, `timeout`, `crash_loop`, `rate_limit`, `throttle`, `providers`, `concern`, `chat` —
just vanish, so the human gets notified, opens the app, and finds nothing. This section gives each kind a
card. No new component: every card below is the **question card shell** (`views._q_layout` /
`design/tui.md`'s Needs-you card) — avatar in the kind's color, name + verb line, one-line body, a button
row, `×` dismiss — driven off the notifier's pending map (key/kind/agent/text/since) instead of the
`questions` table, since these kinds have no options/context columns to read.

### Title rule
"`<agent> needs you`" (single item) / "`N things need you in <project>`" (multiple) stays exactly as today
for **any kind whose card has a real action** — that's every kind below except the three awareness-only
ones. `rate_limit`, `throttle`, and `providers` are informational only (nothing to decide, just a heads-up
that something's slower right now), so their title becomes **"`<project>: heads-up`"** instead — composed
in `notify.py`'s `tick()`, same batching/suppression/must-deliver rules, just a different string. Don't
reuse "needs you" for a card whose only button is "OK".

### Per-kind cards

| Kind | Title (name+verb line) | Body (one line) | Buttons | TUI (`a`+…) | Accent |
|---|---|---|---|---|---|
| `blocked` | "*&lt;assignee&gt;* is blocked" | task title, e.g. "#14 Fix backoff cap" | **Open task** (primary) · **Ask the PM** | `1` Open task · `2` Ask the PM | `RED` (matches the board's existing `blocked` status color) |
| `check_failed` | "*&lt;assignee&gt;*'s task failed its check" | task title | **Open task** (primary) | `1` Open task | `ORANGE` |
| `backoff` / `stalled` / `timeout` | "*&lt;agent&gt;* needs a nudge" | the notifier's own text (already agent-facing, e.g. "Repeated runs failed; retry backoff reached its cap") | **Open transcript** (primary) · **Wake now** · **Stop** (danger) | `1` Open transcript · `2` Wake now · `3` Stop | `ORANGE` |
| `crash_loop` | "*&lt;agent&gt;* keeps crashing" | same notifier text | **Open transcript** (primary) · **Wake now** · **Stop** (danger) | `1` Open transcript · `2` Wake now · `3` Stop | `RED` (worse than a plain backoff) |
| `rate_limit` | *(heads-up title, no verb line)* | "Claude limited, back in 4h" | **OK** | `1` OK | `ORANGE` |
| `throttle` | *(heads-up title)* | the throttle reason (existing text) | **OK** | `1` OK | `ORANGE` |
| `providers` | *(heads-up title)* | "All of &lt;agent&gt;'s providers are unavailable" (existing text) | **OK** | `1` OK | `RED` (strictly worse than one rate-limited provider, matches `pulse.md`'s severity ordering) |
| `concern` | "troupe needs you" (no reporter, no agent name — see below) | "An agent raised a concern. Run `troupe concerns` to read it." (existing text, unchanged) | **Got it** | `1` Got it | `RED` (matches the Concerns badge color already decided for #78) |
| `chat` | "*&lt;sender&gt;* sent a message" | first ~120 chars of the message | **Open chat** (primary) | `1` Open chat | sender's role color |

Buttons follow the existing kind convention: the primary navigation action is `primary`, a destructive one
(`Stop`) is `danger`, everything else `default` — same as `_q_layout`'s option buttons.

### Corrections to the task brief
- **`blocked`: "reply to assignee" → "Ask the PM".** REQ-COM-029 shipped with #65: the TUI's human chat
  is PM-only, and "the frozen raylib GUI's per-agent chat is no longer part of the model, and new clients
  don't offer it." A button that opens a direct chat with the assignee would contradict a decision that's
  already merged, so both surfaces route through the PM chat instead — same as every other human-to-agent
  path today.
- **`check_failed`: one button, not two.** There's no separate check-log viewer — `gates.check_failed`
  already writes the failing output into the task's `review_notes` and a system mail to the assignee, so
  "Open task" already shows the log (task notes/mail, same as any other task). Adding a second "View check
  log" button would point at a surface that doesn't exist.
- **`backoff`/`stalled`/`timeout`/`crash_loop`: "Poke" → "Wake now".** Same action (`d.command('poke', id)`,
  clears failure backoff per REQ-ENG-010), renamed to match the label the Agent view's button already uses
  — one verb for the same action, not two.
- **TUI "Open transcript" has no destination yet.** The TUI has no per-agent transcript pane (only
  Header/Team/Tasks/Comms/Chat, plus Concerns for #78). Until one exists, the TUI's "Open transcript"
  jumps to the **Team pane with that agent's row focused** — the closest available view of what an agent
  is doing — rather than inventing a new pane inside this task. Flagging this as a real gap, not a blocker:
  Team's current-activity line is enough to judge whether Wake/Stop is the right call, just not as much
  detail as the GUI's full transcript.

### Notes
- `concern`'s card is the **interim** shape only (per REQ-COM-029: content-free, no reporter, single "Got
  it" dismiss) — #78 replaces it entirely with the Concerns pane and Raise/Suppress/Kill/Reply, already
  designed in `design/tui.md`'s Needs-you section and specs/45-tui.md's TUI-014. Don't extend this card;
  build #78's instead when that task starts.
- `blocked` tasks with `human_request=true` (the human's own ask, #45) still sort first via the notifier's
  existing `urgent` flag — no visual star/badge yet, since #45 (not this task) owns that treatment. If #45
  ships first, its badge should appear on this card too rather than getting a second design pass.
- Dismissing an awareness-only card (`rate_limit`/`throttle`/`providers`) removes it for that key and it
  doesn't reappear until the state changes again (new reset time / new throttle episode) — same "dismiss
  = seen" rule the notifier already applies, not a new one.

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
  failure/blocked states, not rate limiting). Label: `"<backend> limited · back in 4h"` (countdown format,
  #105). A pill disappears the frame its backend's limit clears — no exit animation needed, these are
  low-frequency.
- In Chat, a working bubble for an agent on a limited backend shows the same limited-until copy inline
  (`_typing`'s existing bubble, swap the activity line for `"Claude limited, back in 4h"` in place of
  `"thinking…"`) rather than a separate banner — one state, shown where the user is already looking.

## Header rows: space allocation rule

Surfaced by #23's zoom work hitting the same bug twice (Board's assignee-vs-timestamp, then the Agent
header's name-vs-buttons) — there was no documented rule for how a row of mixed elements (identity text,
pills, buttons) shares width when the logical canvas shrinks (zoom, a small window, or a long name), so
each fix patched one collision without a shared principle, and QA found the next one. This is the rule,
for every such row in the app (Agent header, Board task cards, and any future one):

1. **Compute reserved blocks first, from actual content, never an assumed width.** Buttons/controls
   block: sum real button widths (`ui.button_w`, which already accounts for the current label — "Stop"
   only exists in `running` state, so the block is *wider* while running, not the same every time).
   Fixed badges/pills: same, measured, not guessed.
2. **Priority order when space is short, tightest first:** primary identity text (name/title) is what
   the row exists to show — it never gets silently covered by a sibling. Everything else yields to it
   in this order: decorative/secondary pills (role, backend/model, status) truncate or drop before the
   name does; controls/buttons are the one thing that never shrinks or disappears (a missing "Stop"
   button while an agent is running is worse than a truncated name) — they're sized off their real
   content and get first claim on the row's width, and the *rest* of the row budgets around them, not
   the other way around.
3. **The identity text always gets an explicit budget, computed live.** Not `ui.text` (no limit at
   all — this was the actual bug: `views.py agent_view`'s name draw has zero width budget, so the
   right-hand button block can grow over it) — use `ui.text_fit`/`ui.ellipsize` with
   `row_width - reserved_left - reserved_right - padding`, recomputed every frame from whatever's
   actually being drawn that frame (state-dependent button sets included), never a static guess made
   once.
4. **Below the documented minimum window (1120×720, REQ-GUI-008) or at extreme zoom, wrap rather than
   overlap** if even a one-character-ellipsized identity plus the reserved blocks don't fit — two lines
   is legible, overlapping text never is. This should be rare if rule 3 is followed correctly; it's the
   fallback, not the primary mechanism.

**Immediate application:** the Agent header (`views.py agent_view`, ~line 807) draws the agent name via
bare `ui.text` with no budget at all, then separately computes the button block from the right edge —
the two never coordinate, which is exactly QA's current #23 repro (Stop button, which only appears while
`running`, covers the name's suffix). Fix per rule 3: compute the button block's total width first
(already state-aware, since `buttons` already conditionally includes `"stop"`), then draw the name with
`ui.text_fit`/ellipsize against `head.w - reserved_left - reserved_right`, where `reserved_right`
includes that frame's actual button block width — not a fixed constant, so it correctly shrinks further
whenever Stop is showing.

## App icon (REQ-GUI-027)

`src/troupe/assets/icon/troupe-1024.png` (master), `troupe.iconset/` (standard macOS size set) and
`troupe.icns` (built via `iconutil`) — regenerate with `design/icon/build-icns.sh <svg>` from the
1024px source SVG. The iconset's file naming matches Xcode's `AppIcon.appiconset` convention exactly,
so the same PNGs drop into a SwiftUI asset catalog if/when that's needed — no separate Mac-app asset
work required.

**Direction ("Orb"), chosen by the human from 3 options (question #9):** a single luminous indigo
sphere (`ACCENT`-family gradient, bright specular highlight top-left per current Apple icon lighting
convention) on the midnight squircle background, with a thin orbit ring and one small green
satellite dot — the calmest, most premium-reading of the three explorations, and the most legible at
menu-bar/Dock scale since it resolves to one dominant shape rather than several small ones. The other
two explored directions (a three-orb "Triad" evolving the in-app logo mark literally, and a bold
abstract "T" monogram) are kept in `design/icon/` for reference; all three were verified legible at
32px and 16px before presenting them (`design/icon/legibility-check.png`; final shipped-asset check
at `design/icon/final-legibility-check.png`).

Rendered from hand-authored SVG via `qlmanage -t` (no new runtime dependency — `qlmanage`/`sips`/
`iconutil` are all macOS built-ins) rather than a raster tool, so the source stays editable and
resolution-independent.

## Known gaps (feeds the polish backlog below)

- No real type-scale or radius-scale constants — both are "whatever the nearest call site used."
- `STATUS_COLORS["backlog"]` and `STATUS_COLORS["cancelled"]` are both `TEXT_FAINT` — visually
  identical despite meaning opposite things.
- Empty states are inconsistent in polish: Chat and the human inbox get an illustrated
  glow+icon+copy treatment; Mail, Memory, Docs, and empty Board columns get a single gray line.
