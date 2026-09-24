# Stage — ambient full-screen view of the team at work

Behavior: `specs/40-gui.md` REQ-GUI-030..037. This doc defines the *look*: layout, visual states,
motion timing, and pacing. Builds on `design/system.md` (tokens, components). **As of the #32 redesign,
`design/pulse.md` is the master spec for the shared scene** (agent states, tethers, comets, task cards,
model chips, the Work panel) — Pulse and Stage are one scene at two densities. This doc keeps only
what's Stage-specific: entering/leaving full-screen, sizing-by-screen-fraction, quiet-mode dimming/
drift, the ready-queue tray, and the engine-offline state. Read `design/pulse.md` first for what a
node/comet/task actually looks like; this doc assumes it.

## Principle: watching, not reading

Stage runs unattended on a second monitor, possibly for hours. Everything here optimizes for a
glance from across a room, not for parsing every word. If a viewer has to lean in to understand
what's happening, the element is wrong — dial it back, slow it down, or cut it. Motion always
carries a signal (REQ-GUI-030..037 already encode which ones); nothing moves just to look alive.

## Sizing is relative, not fixed

Every other view in the app (`design/system.md`'s type scale) uses fixed point sizes because it's
read up close, at arm's length, on one window size range. Stage is read from a variable distance on
a variable-size display (a 13" laptop screen vs. a 32" second monitor vs. a TV), so **all Stage
sizing is a fraction of the current screen's shortest dimension** (`m = min(w, h)`), clamped to a
sane px range — never a bare literal. This is the one place in the app where that's true; don't
carry it back into the rest of the GUI.

| Element | Formula | Clamp |
|---|---|---|
| YOU node radius | `m * 0.052` | 34–64px |
| Agent node radius (n≤9, single ring) | `m * 0.034` | 26–48px |
| Agent node radius (n>9, two rings) | `m * 0.026` | 22–34px |
| Agent name label | `m * 0.019` | 16–26px, `med` |
| Activity/status caption | `m * 0.015` | 13–20px, `ui` |
| Comet label pill text | `m * 0.016` | 14–22px, `med` |
| Ticker text | `m * 0.015` | 13–20px, `ui` |
| YOU question count | `m * 0.024` | 20–34px, `bold` |

## Entry / exit (REQ-GUI-030)

- Pulse gets a small "Stage ▸" affordance near its feed header (ghost button, same treatment as
  other secondary top-bar actions) plus ⌘⇧F. Both call the same transition.
- Transition in: top bar, sidebar, and inbox panel slide/fade out over 220ms (ease speed ~14, matching
  existing panel-level transitions) while the Pulse graph simultaneously scales up and the window goes
  borderless full-screen. It should read as Pulse *becoming* Stage, not a hard cut to a new screen.
- A faint identity mark — the existing top-bar constellation glyph + project name, at 40% opacity —
  fades in top-left for 2s on entry, then fades to near-zero (8% opacity). It brightens back to 40% on
  mouse movement (idle-timeout-driven, like a cursor auto-hide) so you can confirm which project you're
  watching without permanent chrome. This is the only persistent text on screen besides the ticker.
- Esc or ⌘⇧F reverses the transition and restores the previous window geometry exactly.
- `troupe watch` skips the transition (opens directly in Stage's resting state). With no engine
  running it shows the **Engine offline** state below, not an error screen — Stage always renders
  something calm.
- `TROUPE_STAGE_DEMO=1` (with `TROUPE_TAB=Stage TROUPE_SHOT=...`) seeds one of everything at once for
  a single representative screenshot: a working node, an idle node, a parked node, a throttled node,
  one in-flight comet mid-label, one fan-out (2-3 comets from one sender), one attached task card, one
  bounce-in-progress, the gold YOU glow at count 2, and a full 5-line ticker. Design elements below are
  written so all of those can coexist without overlapping.

## Layout

Single center **YOU** node; agents ring around it. Angular order is fixed and role-priority-based, so
a regular viewer builds spatial memory (lead is always "up and to the left," etc.) rather than agents
reshuffling on every refresh: **lead, pm, spec, designer, qa, gadfly**, then builders sorted by id,
starting at 12 o'clock and proceeding clockwise — same convention as `pulse_view`'s existing
`_node_positions`.

**Ring count** depends on how many enabled agents there are:
- **n ≤ 9 — single ring.** Radius `m * 0.40`. This covers the default 7-role team plus a couple of
  extra builders comfortably (n=3 and n=8 both fall here).
- **n > 9 — two concentric rings.** Inner ring (radius `m * 0.26`) holds the fixed-role team (lead,
  pm, spec, designer, qa, gadfly — whichever of those are enabled, up to 6); outer ring (radius
  `m * 0.46`) holds everyone else (builders, overflow), spaced evenly. This keeps the "who does what"
  identity set close to center even when the builder pool grows, and keeps outer-ring label spacing
  legible instead of cramming 12 nodes into one crowded circle. At n=12 with the default 6 fixed roles
  enabled, that's 6 inner + 6 outer — comfortable on both rings.

Both rings leave a `0.16 * m` margin from the bottom edge clear, reserved for the ready-queue tray and
ticker (below) so full rings never collide with them.

**Adding/removing a node** (REQ-GUI-031, e.g. an agent gets disabled): the node eases its radius from
0→target (appearing) or target→0 (leaving) over 500ms while every other node on its ring eases its
angle to the new even spacing over the same 500ms — a re-flow, never a snap. Use `ui.ease` per-node on
both radius and angle.

## Elements

### YOU node
Center, `T.HUMAN` colored ring (matches Pulse). Two states:
- **Clear** — resting: a slow, low-amplitude glow (5s sine period), same `ui.glow` primitive Pulse
  already uses for the human node, just gentler.
- **Needs you** (≥1 open question, REQ-GUI-034) — the glow shifts to `T.PINK`→`T.YELLOW` gold and
  breathes on a 3s period (well under the ≤1-cycle-per-2s ceiling), with the open count rendered in the
  YOU label (`"YOU · 2"`). Fades back to Clear within 1s of the last question being answered/dismissed
  — a fast ease (speed ~10), distinctly quicker than the slow breathing itself, so resolution reads as
  immediate relief rather than another cycle.

### Agent nodes — four visually distinct states (REQ-GUI-031)
All states share the base: `ui.avatar`-style ringed circle in the agent's role color
(`design/system.md`'s role palette), name label below at the sizes in the table above.

| State | Treatment |
|---|---|
| **Idle** | Slow breathing glow, 5s sine period, low amplitude — calm, present, doing nothing. No caption. |
| **Working** | The existing avatar `running` treatment (rotating arc, `sin(t·3.2)` pulse — same formula as `core.py`'s `ui.avatar`, so Stage and the sidebar agree) plus a caption beneath the name: the current tool/activity, truncated to fit, `TEXT_DIM`→role-color blend. |
| **Parked** (owes work, REQ-GUI-002) | Static (non-pulsing) `T.ORANGE` outer ring plus a small `!` glyph badge at the node's upper-right. Deliberately *not* animated — parked means "stalled," and a pulsing warning would read as active/urgent, which is the wrong signal for "idle when it shouldn't be." |
| **Throttled / rate-limited** (REQ-ENG-016) | Node desaturates toward `T.CYAN` (mixed 55% toward `T.BG`, ringed in `T.CYAN`) — a "frozen" look, distinct from the warm amber of parked. Caption shows the reset time ("resets 14:05"), same phrasing as the top-bar rate-limit pill in `design/system.md`. This is a new *semantic* use of `T.CYAN` (elsewhere it labels the "ready" task status) — no collision risk since a task-status pill and an agent-node ring never share a view. |

Only one state applies at a time (a throttled agent can't also be "working" — REQ-ENG-016 blocks runs
on a limited backend entirely).

### Comets (REQ-GUI-032)
A comet = a bright head (glow + core dot, same construction as Pulse's particles) trailing a short
fading tail, following the same quadratic-bezier arc `pulse_view._bez` already uses between two node
positions — plus a **label pill**: a small `T.TOOLTIP`-colored rounded rect riding just above the
comet head, holding the subject (or first ~40 chars of the body), horizontal (never rotated to the
path — rotated text is unreadable in motion). The pill fades in over the first 150ms and out over the
last 200ms of flight; for the rest of the flight it's fully legible.

- **Flight time: 2.0s** (comfortably over the ≥1.5s floor, and slower than Pulse's 1.4s particles
  because these have text to read, not just motion to notice).
- **Fan-out** (`to=team` or `to=role`): siblings depart the sender within a single spray, staggered
  80ms apart, one per recipient, each on its own arc; the sender itself doesn't move.
  Human↔agent traffic always routes through YOU as one endpoint (never agent→agent bypassing YOU),
  matching REQ-GUI-032.
- **System mail is not a comet** — it goes straight to the ticker, per spec.
- **Cap at 12 concurrent comets** (REQ-GUI-032): once 12 are in flight, a new message whose edge
  (sender↔recipient pair) already has an active comet merges into it instead of spawning a new one —
  the existing comet's label becomes `"<subject> ×N"`, its counter increments, and its life extends to
  2.5s so the merged count is readable before it fades. A new message on an edge with *no* active comet
  still gets its own comet even over the cap (the limit bounds redundant same-edge traffic, not total
  distinct conversations).

### Task cards (REQ-GUI-033)
A task card on Stage is a compact chip — `#id` + truncated title, status-colored left edge (same
3px-accent-bar language as the Board's `_task_card`) — much smaller than the Board's full card, sized
to sit next to a node without crowding its label.

- **Ready queue tray**: unassigned `ready` tasks rest as a small stacked-card glyph in the reserved
  bottom margin, bottom-center, with a count badge. This is the flight's visual origin — spec defines
  the *behavior* (ready → flies to assignee) but not an origin point, so this tray is a designer
  addition to give that flight somewhere to start from; flagged to spec/lead below.
- **Ready → in_progress**: the tray's top card animates (400ms ease along a bezier, not a straight
  line — consistent with every other flight on Stage) from the tray to the assignee's node and
  attaches there (small offset from the node, upper-right), showing id + short title.
- **In_progress → review**: the attached card flies (same 400ms bezier) from the current node to the
  reviewer's node (QA, or YOU when the human reviews per REQ-ENG-039) and re-attaches there.
- **Merge** (review → done via merge): the card at the reviewer's node **bursts** — a quick radial
  particle pop in `T.GREEN`, 350ms, card fades within the burst — then it's gone. Satisfying, brief,
  never blocks anything else on screen.
- **Reject / conflict / checks-failed** (REQ-ENG-040): the card **bounces** back to the builder's node
  along a visibly different path than a normal flight — a tighter, wobbled arc in `T.RED`, 1.0s, with a
  brief radial flash (`T.RED`, 200ms) at the builder node on arrival and a small decaying shake
  (±10px, 3 cycles, 400ms) on that node. This must read as "rejected," not "merged," at a glance —
  color (red vs. green) plus motion shape (wobble/bounce vs. clean arc) both signal it, not color alone.
- **Blocked**: no flight — the attached card gets a small `T.RED` corner tag, node otherwise unchanged.
- **Overflow**: at most one card physically attached per node; additional in-flight/attached tasks for
  that agent show as a `+N` badge on the attached card instead of stacking more chips.

### Ticker (REQ-GUI-035)
Bottom edge, full width, thin translucent strip (`alpha(T.TOOLTIP, 0.85)` background, no border — it
should feel like a caption bar, not a panel). Rather than a scrolling marquee (motion competing with
comets, and marquees are hard to read from a distance mid-scroll), the ticker shows its 5 held entries
**stacked**, newest at top: newest at full `T.TEXT` opacity, each one older stepped down
(~85%, 70%, 55%, 40%) so recency is legible as a brightness gradient without requiring timing to catch
it. A new entry pushes in from the right with a 300ms slide+fade; older entries shift down and dim in
the same motion. Icon/color per event matches the existing activity-feed dot convention in `pulse_view`
(role color for agent actions, `T.RED` for rejections/errors, `T.PINK` for new questions).

## Quiet mode (REQ-GUI-036)

Trigger: no agent has run and nothing has moved (no comet, no card flight, no ticker entry) for 60s.

- **Entering**: over 2.5s, ease overall scene brightness down — implemented as a global alpha multiplier
  applied to every drawn element (nodes at ~45%, labels at ~55%, ticker at ~60%; YOU's gold glow, if
  active, is exempt and stays at full brightness since an open question is never "quiet"). Slow drift
  begins simultaneously: each node's position gets a low-frequency sine offset (independent per node,
  ~30s period, ±2% of ring radius amplitude) layered on top of its fixed angle — enough to prevent
  burn-in on a static image, subtle enough to not read as "something is happening."
- **Leaving**: any new activity (a comet spawns, a card moves, a ticker entry lands, a question opens)
  snaps brightness back to 100% within 0.5s (ease speed ~16) and drift stops (nodes ease back to their
  exact fixed positions over the same window).

## Engine offline state

Calm, not alarming — this is a background display, not a pager. Nodes desaturate toward `T.TEXT_FAINT`
(a grayscale read, distinct from the warm/cool tints of parked/throttled so it doesn't look like "every
agent is throttled"). YOU's label reads `"Engine offline"` in `T.RED` instead of showing a question
count (the gold glow doesn't apply — there's no live team to ask). No comets or card flights spawn. The
ticker keeps its last 5 held entries from before the engine went down, frozen, with one line prepended:
`"Engine offline since HH:MM"` in `T.RED`, which doesn't age out of the 5-slot window while offline.

## Motion timing reference

| Motion | Duration / period | Ease speed (where eased) |
|---|---|---|
| Idle agent/YOU breathing | 5s sine | — (pure sine, not `ui.ease`) |
| Working pulse (avatar arc) | ~2s sine (`t·3.2`, shared with `ui.avatar`) | — |
| Gold question glow breathing | 3s sine | — |
| Gold glow fade-out (resolved) | ≤1s | ~10 |
| Comet flight | 2.0s | — (bezier `t`, not eased) |
| Comet merge (>12 cap) flight | 2.5s | — |
| Fan-out sibling stagger | 80ms | — |
| Comet label fade in/out | 150ms / 200ms | — |
| Task ready→assignee / →reviewer flight | 400ms | — (bezier) |
| Merge burst | 350ms | — |
| Reject bounce (flight + flash + shake) | 1.0s + 200ms flash + 400ms shake | — |
| Node add/remove + ring re-flow | 500ms | ~10 |
| Entry/exit chrome transition | 220ms | ~14 |
| Identity mark fade (entry / idle-out / on-move-in) | 2s hold, then fade to 8% | ~8 |
| Ticker entry slide-in | 300ms | — |
| Quiet-mode dim in | 2.5s | ~6 |
| Quiet-mode brighten out | 0.5s | ~16 |
| Drift period | ~30s sine | — |

Ring re-flow and entry/exit use noticeably slower ease speeds than the rest of the app's UI (contrast
`design/system.md`'s 14–18 range for tab/button chrome) — Stage should feel unhurried even when
transitioning, per the calm principle.

## Performance (REQ-GUI-037)

All of the above reads only `gui/data.py` snapshots — no new store queries from draw code. The mapping
from a snapshot diff to comets/cards/ticker entries (fan-out split, the 12-comet cap and merge, the
reject-bounce trigger, the question-glow state) belongs in a pure module (e.g. `gui/stage_model.py`)
separate from the raylib draw calls, so it's unit-testable without a window — this mirrors how
`views.on_new_messages` already turns messages into Pulse particles, just promoted to its own testable
module given Stage's extra state (attached cards, merge counters, quiet-mode timers).

## Consistency with REQ-GUI-030..037

This design implements 030–037 as written; no contradictions found. Two additions beyond what the
requirements specify (flagged to spec/lead, not blocking):
1. **Ready-queue tray** — the requirements say ready tasks "fly to the assignee" but don't define a
   visual origin; I've added a small stacked-card glyph in the bottom margin as that origin. Doesn't
   change any behavior, purely gives the existing flight somewhere to start from.
2. **Two-ring layout above 9 agents** — not required by GUI-031, but needed for the n=12 case the brief
   asked me to cover; keeps the fixed-role "core team" visually closer to center as the builder pool
   grows.

## New tokens

No new RGB values. Two new *semantic* uses of existing `design/system.md` colors, both noted above:
`T.CYAN` for the throttled/rate-limited agent-node state (elsewhere only a task-status color), and
`T.PINK`→`T.YELLOW` gold blend for the YOU question glow (`T.PINK` already means "needs you" on the
inbox badge; the gold shift on Stage is new but built from existing hues, not a new constant).

Stage introduces its own **type scale** (the sizing table above), which is deliberately not part of
`design/system.md`'s scale — see "Sizing is relative, not fixed."

## Implementation notes: raylib-specific vs. portable

pm's note (msg #90): a native SwiftUI GUI is proposed (unconfirmed). This design's states, layout,
colors, copy, and motion *timings* (the seconds/periods in the tables above) are the design and carry
over regardless of shell. A few specific mechanics above were described in terms of the current raylib
toolkit and would translate rather than port literally:

- **The "ease speed" column** in the motion timing table is a raylib `ui.ease` parameter (exponential
  approach, `speed` in the 6–18 range used throughout the app). The *durations and periods* next to it
  are the actual spec; a SwiftUI implementation would drive the same numbers through
  `.animation(.easeOut(duration:))` / `TimelineView`-driven interpolation rather than that function —
  same curve shape, different API.
- **Comets, particles, glows, and the bezier flight paths** (`pulse_view._bez`) are exactly the "sexy
  part" pm's proposal calls out for `Canvas` + `TimelineView` (or Metal) — likely a *better* native fit
  than raylib's immediate-mode circles/lines, not just a port. The visual result (glow head, fading
  trail, bezier arc) is the spec; the draw technique is free to be whatever's idiomatic in Canvas.
- **The working-node pulse formula** (`sin(t·3.2)`, shared with `core.py`'s `ui.avatar`) is a raylib
  implementation detail of "a ~2s breathing pulse while working" — the timing is the spec (see the
  table), not the specific sine call.
- **The suggested `gui/stage_model.py` pure-module split** (snapshot → comets/cards/ticker mapping,
  unit-tested without a window) is architecture-neutral advice, not raylib-specific — it applies just
  as well to a SwiftUI app reading the same engine snapshots, and is worth keeping regardless of shell.

Nothing else in this doc assumes raylib.
