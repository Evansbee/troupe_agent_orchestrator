# Multi-project rail: one window, every running troupe

Behavior basis: REQ-ENG-008 (project registry), REQ-GUI-040 (project switcher — being superseded by
#29's REQs; spec hasn't written those yet, so this design uses ENG-008/GUI-040's shape as the floor —
flagged to spec below). Human request via pm msg #68: "the GUI connects to all running projects at
once."

**A portability note up front:** pm has a proposed (unconfirmed) plan to move the GUI to a native
SwiftUI client (decision #37), which would retarget #29 away from the raylib toolkit. This doc is
written at the information-architecture/token level — states, layout, data, copy — rather than leaning
on raylib-specific implementation calls, so it stays useful either way. Where a raylib-specific
implementation note is genuinely useful, it's called out explicitly as one, not load-bearing for the
design itself.

## Principle

The rail exists so the human never has to wonder "is anything wrong in the other project" — that's
the whole value of "one window, every running troupe" over just running `troupe gui` twice. It should
answer that question without being clicked, and switching projects should feel as cheap as switching
tabs, not like reopening the app.

## When it appears

With exactly one attached project (today's normal case, and probably the common case for a while),
the rail **doesn't render at all** — zero layout cost, zero new chrome, existing single-project UI
unchanged. It appears the moment a second project is attached and stays until back down to one. Don't
build a "rail with one tile" state; there's nothing to switch to.

## The rail

A slim vertical strip at the window's far left edge, outside (to the left of) the existing team
sidebar — the two are different things and shouldn't merge: the team sidebar lists agents in the
*current* project, the rail lists *projects*. Width ~56–64px — narrower than the team sidebar
(`SIDEBAR_W=272`), tall enough only for its tiles, scrollable if the list outgrows the window
(same pattern as the team sidebar's own scroll).

**Tile anatomy** — deliberately **square/rounded-square**, not circular, so a project tile never reads
as "an agent" at a glance (agents/avatars are always circles throughout the app):
- A neutral-filled square (`T.PANEL3`) holding a 1–2 letter monogram of the project name, `TEXT` bold.
- A status dot at the bottom-right corner (see states below).
- A needs-you count badge at the top-right corner when that project has open questions — same
  `ui.badge`-shape component used everywhere else (pill/circle, `T.PINK`), just anchored to a tile
  instead of a label or avatar.
- The active project's tile gets the same "selected" treatment the tab bar and sidebar already use:
  tinted background + a thin colored side accent (use `T.ACCENT`, the app's general "this is the
  selected thing" color — not a project-specific color, see "No per-project color palette" below).
- Hover reveals the tile's name as a tooltip (`ui.tip`) plus (for non-active projects) start/stop
  controls — see below.
- Below the last tile, a **"+"** tile of the same shape, `TEXT_FAINT`, for attaching another project.

### No per-project color palette
It's tempting to give each project its own accent color the way each role has one, but that risks
exactly the collision the app has otherwise avoided: role colors are a fixed set of 7 hues, and a
handful of projects would either reuse those hues (a project tagged the same cyan as the Spec role,
sitting next to a Spec avatar in a cross-project list, reads as related when it isn't) or need a
second palette to invent and keep distinct. Simpler and collision-proof: **projects are disambiguated
by name/handle, never by color.** A project's identity everywhere is its monogram (rail), its full
name (tooltips, the attach popover), or the `@project` suffix already baked into COM-005 handles —
never a swatch.

## Project states
Five states, each a status-dot color plus (where color alone is ambiguous) a small glyph overlay —
tooltip always gives the full state name and detail on hover, so the dot is a glance-level signal, not
the only source of truth:

| State | Dot | Glyph | Detail (tooltip) |
|---|---|---|---|
| Live | `T.GREEN`, same slow breathing pulse as the top-bar Live pill | — | "Live · N working" |
| Paused | `T.YELLOW` | — | "Paused" |
| Restart needed (stale version, ENG-006) | `T.YELLOW` | small refresh glyph overlaid on the dot | "Restart needed — service is 0.1.0, installed 0.2.0" |
| Throttled/rate-limited | `T.ORANGE` | — | same phrasing as the top-bar/Stage throttle treatment: "Claude limited until 14:05" |
| Offline (registered, not running) | hollow ring, `T.TEXT_FAINT`, no glow | — | "Offline — click Start" |
| Missing (`.troupe/` gone, ENG-008) | hollow ring, `T.RED` | small warning glyph | "Project folder not found" — tile can't be opened, only removed from the registry |

Paused and Restart-needed share a color family deliberately (both are "intentional/needs-action, not
broken") but need the glyph to tell apart at tile size, since they call for different human actions.

## Cross-project "needs you"

The top-bar/inbox needs-you badge becomes a **sum across every attached project** (not just the active
one) — this is the single most important payoff of the rail existing at all; if it only counted the
active project, you'd have to click through every tile just to find out nothing's wrong, which defeats
the point.

The "Needs You" panel itself goes **cross-project by default**: it lists every open question from
every attached project, not just the current one (same card layout as today —
`views._q_layout` — unchanged), each card gaining one addition: a small neutral tag pill
(`ui.pill`, `T.TEXT_FAINT`, the project's short name) before the asker's name. A flat list with tags
rather than grouped sections — simpler, no new list structure, and scans fine at the realistic project
count (a handful); if the typical count grows a lot, grouped sections with a project header become the
natural upgrade, but that's not needed yet.

Clicking a cross-project question card: switches the rail's active project to that card's project
(tile highlight moves), *then* behaves exactly as today (reply inline, option buttons work as normal)
— the switch is instant and automatic, not a separate step the human has to do first.

Each rail tile's own needs-you badge (above) is the "at a glance, which project" signal; the panel is
the "read and answer" surface. They always agree in total count.

## Handles across projects (REQ-COM-005)

The rule: **full handle (`role_N@project`) wherever the view could plausibly mix projects; short/local
form wherever it can't.**
- Inside the active project's own views (team sidebar, Chat, Board, Agent tab, single-project Stage) —
  short form, no `@project` — exactly as COM-005 already specifies for "purely spatial" labels, and
  correct here too: you're unambiguously looking at one project's team.
- The cross-project needs-you panel, and any other view that ever aggregates across projects (a future
  cross-project Mail or activity feed) — full handle, always. This is exactly the ambiguity COM-005
  exists to resolve, and it only becomes real once two projects' agents can appear in the same list.

## Start / stop

- **Start** (offline project): hovering an offline tile reveals a small play-glyph button overlapping
  the tile; one click starts the service (`troupe up`-equivalent) and the dot eases from hollow to
  `GREEN` once its heartbeat appears — no confirmation needed, starting is non-destructive.
- **Stop** (live project): hovering a live tile reveals a stop-glyph button; clicking it goes through
  the same confirm-modal pattern used for other consequential actions in the app (Decisions' delete,
  Board's cancel) — this stops running agents mid-work (ENG-006), so it deserves the pause a modal
  provides, unlike Start.
- **Attach another project**: clicking the "+" tile opens a small popover (anchored to the tile,
  `T.TOOLTIP`-style background, not a full scrim modal — this is a light, frequent-ish action) listing
  registry entries (REQ-ENG-008) not currently attached, plus an "Open folder…" row that hands off to
  the OS file picker for a path not yet in the registry. Picking an entry attaches and switches to it.
- **Remove a missing project** from the registry: available from that tile's hover actions (a single
  "Remove" — the project folder is already gone, so there's nothing to confirm against).

## Switching

Click any tile to make it active — the whole window's content (team sidebar, all tabs, the top bar's
single-project stats) re-points to that project's data. Active-tile highlight, described above, is the
only persistent indicator of "which project am I looking at" — worth being unambiguous, since it's easy
to forget once you've switched. A keyboard shortcut for cycling projects should use a modifier
combination not already claimed (⌘1–7 are tabs, ⌘⇧F is Stage) — e.g. ⌥1–9 for direct tile selection,
matching the tab bar's direct-select convention rather than only offering next/previous cycling.

## Layout at typical counts

- **1 project:** rail absent (see "When it appears").
- **2–4 projects:** comfortable, generously spaced tiles, no scrolling.
- **5+ projects:** rail scrolls (same scrollbar treatment as the team sidebar); tiles keep their full
  size rather than shrinking — a smaller tile would make the status dot/glyph distinction (the whole
  point of the tile) harder to read, and scrolling a short list is cheap.

## Empty/edge states
- **No projects registered besides the current one:** rail absent, nothing to design (see above).
- **A project goes from Live to Missing while attached** (its folder was deleted externally): tile
  transitions in place to the Missing treatment; if it was the active project, the center content shows
  a calm explanatory state ("This project's folder is no longer here") rather than a blank or crashed
  view, with a "Remove from list" action and a prompt to pick another attached project.

## New tokens
None. Every state reuses existing `design/system.md` semantic colors (GREEN/YELLOW/ORANGE/TEXT_FAINT/
RED) with new referents (project state, not task/agent state) — same established pattern as Stage's
reuse of CYAN for the throttled-node state. The explicit decision *against* a new per-project color
palette (see above) is itself the notable design call here, not a token addition.

## Open item for spec
GUI-040 (`[ ]`, not yet superseded by dedicated #29 REQs as of this writing) only specifies "lists
projects from the registry with service state; choosing one re-opens the GUI on that project" — this
design goes further (cross-project needs-you aggregation, start/stop from the rail, the popover attach
flow, handle rules) since the human's ask was "connects to all running projects **at once**," not a
picker that reopens a new window per project. Flagging so #29's REQs are written against this shape
rather than GUI-040's narrower one — let me know if any of it should be scoped out.