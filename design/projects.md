# Multi-project rail: one window, every running troupe

Behavior: `specs/40-gui.md` REQ-GUI-040 (finalized — supersedes the earlier single-project switcher),
built on REQ-ENG-008 (project registry). Human request via pm msg #68: "the gui can connect to multiple
instances that are running, we might want you on multiple projects at a time."

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
- A small working-agent count, bottom-left, plain `TEXT_FAINT` numeral (no badge shape — this is
  secondary to the dot and the needs-you badge, present only when >0, absent when idle so a quiet
  project's tile stays visually quiet too).
- A needs-you count badge at the top-right corner when that project has open questions — same
  `ui.badge`-shape component used everywhere else (pill/circle, `T.PINK`), just anchored to a tile
  instead of a label or avatar.
- The active project's tile gets the same "selected" treatment the tab bar and sidebar already use:
  tinted background + a thin colored side accent (use `T.ACCENT`, the app's general "this is the
  selected thing" color — not a project-specific color, see "No per-project color palette" below).
- Hover reveals the tile's full project name as a tooltip (`ui.tip`, also carrying the full state
  detail — see table below) plus, for offline projects, a **Start** control — see below.
- Below the last tile, a **"+"** tile of the same shape, `TEXT_FAINT`, for **Add project…**.

### No per-project color palette
It's tempting to give each project its own accent color the way each role has one, but that risks
exactly the collision the app has otherwise avoided: role colors are a fixed set of 7 hues, and a
handful of projects would either reuse those hues (a project tagged the same cyan as the Spec role,
sitting next to a Spec avatar in a cross-project list, reads as related when it isn't) or need a
second palette to invent and keep distinct. Simpler and collision-proof: **projects are disambiguated
by name/handle, never by color.** A project's identity everywhere is its monogram (rail), its full
name (tooltips, the Add project… flow), or the `@project` suffix already baked into COM-005 handles —
never a swatch.

## Project states
GUI-040 specifies exactly five rail states — working / idle / paused / offline / crashed. Same
idle-vs-working distinction Stage already uses for agent nodes (`design/stage.md`), just applied at the
whole-service level: a calm breathing glow for idle vs. an active pulse for working, same hue.

| State | Dot | Detail (tooltip) |
|---|---|---|
| Working (≥1 agent running) | `T.GREEN`, active pulse (same `sin(t·3.2)` language as a working agent node) | "Working · 3 agents" |
| Idle (live, nothing running) | `T.GREEN`, calm breathing glow, no pulse | "Idle" |
| Paused | `T.YELLOW` | "Paused" |
| Offline (registered, not running) | hollow ring, `T.TEXT_FAINT`, no glow | "Offline — click Start" |
| Crashed (crash loop, REQ-ENG-042) | `T.RED`, solid, small warning glyph | "Crashed — see engine.log" |

**Missing** (`.troupe/` gone, ENG-008) isn't one of the five state dots — it's a registry condition,
not a service state, and gets its own treatment: hollow ring, `T.RED`, tooltip "Project folder not
found." A missing tile can't be opened, only removed (see Start / Add / Remove below).

## Needs you across projects

Per GUI-040, the inbox gets a **"This project / All" toggle** (two `ui.chip`s at the top of the panel,
same shape as any other filter chip pair in the app) rather than always aggregating — this keeps the
default view identical to today's single-project behavior, and All is an explicit choice, not a
surprise change to what "Needs You" means.
- **This project** (default): unchanged from today.
- **All**: lists every open question from every attached project. Each card gains one addition: a
  small neutral tag pill (`ui.pill`, `T.TEXT_FAINT`, the project's short name) before the asker's name
  — same treatment either way regardless of which project's card it is. Answering a card from a
  background project delivers the answer into *that* project's DB and wakes the right agent there,
  same as if you'd switched to it first.
- **Rail badges still always sum across every project** (not gated by the toggle) — a tile's needs-you
  badge is the "should I check All" signal even while the panel itself is scoped to "This project."
- **Notifications** for a background-project question name the project in the notification text;
  clicking one switches to that project and opens the panel.
- **Window title count** (REQ-GUI-027) is the total across every attached project, not just the active
  one — matches the rail badge total.

## Handles across projects (REQ-COM-005)

The rule: **full handle (`role_N@project`) wherever the view could plausibly mix projects; short/local
form wherever it can't.**
- Inside the active project's own views (team sidebar, Chat, Board, Agent tab, single-project Stage) —
  short form, no `@project` — exactly as COM-005 already specifies for "purely spatial" labels, and
  correct here too: you're unambiguously looking at one project's team.
- The **All** inbox, notifications, and rail tooltips — full handle, always (GUI-040's exact list).
  This is exactly the ambiguity COM-005 exists to resolve, and it only becomes real once two projects'
  agents can appear in the same list.

## Start / Add / Remove

- **Start** (offline project): hover reveals a small play-glyph control on the tile; one click starts
  a detached service for that project (no second window opens) and the dot eases from hollow to
  `GREEN` once its heartbeat appears — no confirmation needed, starting is non-destructive.
- **Add project…**: clicking the "+" tile opens a small popover (anchored to the tile, `T.TOOLTIP`-
  style background, not a full scrim modal — this is a light, occasional action) with a directory
  picker (hands off to the OS file picker). If the chosen directory has no `.troupe/`, it runs
  `troupe init` first; either way the project is registered and attached, and the rail switches to it.
- **Remove from rail**: hover action on any tile (most relevant for offline/missing ones, but available
  on any); unregisters the project from this window without touching its files or stopping its service
  if one is running elsewhere — this is "stop showing it here," not "stop the team." No confirmation
  needed: it's non-destructive and the project can be re-added any time.
- There's no rail-level **Stop** — stopping a project's service is a "Stop team" action that belongs to
  that project's own top-bar (REQ-ENG-006, with its existing confirmation), reached by switching to it
  first, same as any other in-project control. The rail is for visibility and reach, not for consequential
  per-project actions.

## Switching

Click any tile, or **⌘⌥1–9** for direct selection (GUI-040's exact shortcut — doesn't collide with
⌘1–7 for tabs or ⌘⇧F for Stage), to make it active. The switched-to project's full UI appears on the
**next frame**, from its already-cached background snapshot (see Performance below) — no loading
state, no blank frame. If that project qualifies for "While you were away" (REQ-GUI-028, ≥10 min since
it was last looked at), that panel appears immediately after the switch, exactly as it would if you'd
opened the app straight into that project.

Active-tile highlight (described above) is the only persistent indicator of "which project am I
looking at" — worth being unambiguous, since it's easy to forget once you've switched.

## Performance

Only the visible project refreshes at full rate; every background project's rail tile and inbox data
refresh **at most every 2 s** — that's all a background project needs to drive (a status dot, a working
count, a needs-you badge, and All-inbox rows), nothing else about it is being drawn. No DB I/O happens
in draw code for any project, visible or not. With 3 attached projects, idle frame rate stays the
existing 20 fps target (REQ-GUI-006) — the rail must not turn "idle" into "polling 3 databases 60 times
a second."

Top-bar stats (runs/h, 24h cost, Claude usage meters) and budget stay scoped to the active project only
— an aggregate view across projects is out of scope for now (per GUI-040).

## Layout at typical counts

- **1 project:** rail absent (see "When it appears").
- **2–4 projects:** comfortable, generously spaced tiles, no scrolling.
- **5+ projects:** rail scrolls (same scrollbar treatment as the team sidebar); tiles keep their full
  size rather than shrinking — a smaller tile would make the status dot / working-count / needs-you
  distinction (the whole point of the tile) harder to read, and scrolling a short list is cheap.

## Empty/edge states
- **No projects registered besides the current one:** rail absent, nothing to design (see above).
- **A project goes from a live state to Missing while attached** (its folder was deleted externally):
  tile transitions in place to the Missing treatment; if it was the active project, the center content
  shows a calm explanatory state ("This project's folder is no longer here") rather than a blank or
  crashed view, with a "Remove from rail" action and a prompt to pick another attached project.

## New tokens
None. Every state reuses existing `design/system.md` semantic colors (GREEN/YELLOW/TEXT_FAINT/RED)
with new referents (project state, not task/agent state) — same established pattern as Stage's reuse
of CYAN for the throttled agent-node state. The explicit decision *against* a new per-project color
palette (see above) is itself the notable design call here, not a token addition. The idle/working
pulse distinction reuses Stage's existing agent-node motion language rather than inventing a new one.

## Resolved with spec
Superseded the earlier draft of this doc, which was written before REQ-GUI-040 was finalized (it
guessed at cross-project-by-default inbox aggregation and a rail-level Stop action). Spec's msg #93
settled the shape: the five-state dot list, the This-project/All toggle (not always-on aggregation),
⌘⌥1–9, and Start/Add project…/Remove from rail (no rail-level Stop) — all reflected above.