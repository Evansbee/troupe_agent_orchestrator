# Pulse & Stage: one scene, two densities

Behavior basis: human request via live chat (2026-09-23) — "pulse should show the message passing,
who's waiting, mail backlog and some indication as to what their model is," plus "pulse should include
a list of major tasks that are being worked on." PM direction: Pulse (in-tab) and Stage (full-screen,
`design/stage.md`) are **one scene** — Pulse is that scene with its information layers on, Stage is the
same scene full-screen with the layers dialed to ambient. This doc is the master spec for the shared
scene (the five layers, the state legend, the model chip, the Work panel); `design/stage.md` keeps only
what's Stage-specific (entering/leaving full-screen, quiet-mode dimming/drift, the ready-queue tray) and
now points here for everything about what a node/comet/task looks like.

**Platform-neutral by instruction** (lead, msg #170 — Swift is confirmed): everything below is states,
layout, tokens, motion timings and density rules, not raylib/Canvas/Metal API calls. The SwiftUI spike
(#49) builds Pulse from this document directly.

## Inspiration references

*(Filled in from a live design-research pass — Dribbble tag search plus comparable dark ops/network
dashboards. Noted honestly where a source was fully viewed vs. only visible through search
snippets/thumbnails, per the task's caveat that Dribbble shot pages often don't render for automated
fetching.)*

<!-- REFERENCES_PLACEHOLDER -->

## Principle: one scene, two densities

Pulse and Stage read the same underlying model (nodes, tethers, comets, task cards, the ticker) — there
is no separate "Stage data model," only a **density dial**:

| | Pulse (in-tab) | Stage (full-screen) |
|---|---|---|
| Model chip | full (`glyph · model · pips`) | glyph + pips only above 9 agents (see Density) |
| Mail pips | shown | shown |
| Tethers + age timers | shown, age timer as text | shown, age timer only on hover (avoid text clutter at a distance) |
| Work panel | docked list, always visible | folded into the ticker (cycling milestone/task lines) |
| Event ticker | separate Activity tab strip below the scene (existing) | the only ticker, at the bottom edge |
| Chrome | top bar, sidebar, tabs all visible | none (per REQ-GUI-030) |

Nothing about the *scene itself* — node states, tether meaning, comet behavior, colors, motion timings
— differs between the two. Only how much of it is simultaneously on screen, and how much chrome
surrounds it, differs.

## Layer 1 — message passing (comets)

Builds directly on `design/stage.md`'s existing comet design (bezier flight, 2.0s duration, label pill,
fan-out, the 12-in-flight merge cap) — two refinements the human specifically asked for:

- **Obvious direction:** the comet head is visibly larger/brighter than its tail (not just "a dot with
  a trail" — the head should read as "this end is arriving," the tail as "this end is where it came
  from") — an asymmetric glow, not a symmetric one.
- **Warm recent edges, so you can read "who's been talking to whom" at a glance:** every edge a comet
  travels leaves a residual glow on the direct line between the two nodes (not the bezier arc — a
  straight "spoke," same convention Pulse already uses for its faint always-on spokes). Brightest right
  after arrival, linearly fading to nothing over **3 minutes**. Multiple recent messages on the same
  edge don't stack brightness — the edge just resets to full warmth and restarts its fade. This turns
  the constellation into a readable "who's active with whom right now" map without requiring anyone to
  watch comets fly in real time.

## Layer 2 — who's waiting on whom

The core addition. Every agent is in exactly one of eight named states at any moment. Two-tier legend,
deliberately: a **coarse silhouette** (idle / working / waiting / parked) readable from across a room
without color vision or close attention, and a **fine treatment** (which kind of waiting, tether target,
age) readable up close or in Pulse's tighter viewing distance.

### Coarse silhouette (the four shapes)
| Shape | Meaning |
|---|---|
| Calm breathing glow, no ring | Idle |
| Pulsing arc + activity caption | Working |
| Static **dashed** ring | Waiting on something specific (five substates below) |
| Static **solid** ring, warm amber, `!` badge | Parked — owes work, waiting on nothing in particular (REQ-GUI-002) |

Dashed vs. solid is the load-bearing distinction: dashed means "there's a specific thing this is waiting
on, follow the tether to see what"; solid amber (parked) means "this agent should be doing something and
isn't, but there's no single blocker to point at."

### The five waiting substates (fine treatment)
Each gets a dashed ring (coarse signal above) plus a small glyph badge on the node, a colored dashed
**tether** to whatever it's waiting on (a "marching ants" dash that slowly scrolls along the line to read
as pending, not static), and an age timer (`ago()`-style, e.g. "12m") at the node end of the tether.

| Substate | Glyph | Tether color | Tether target | When |
|---|---|---|---|---|
| Waiting on the human | `?` | Gold (same hue as YOU's question glow) | YOU | It asked a question or proposed an idea and it's still open |
| Waiting on review | small checkmark-pending mark | The waiting agent's own role color | The reviewer's node (QA, or another role per task assignment) | Its task is in `review` |
| Blocked on a dependency/task | chain-link | The waiting agent's own role color, or gold if only the human can unblock it | The blocking task's owner node, or YOU | Task status `blocked` |
| Rate-limited | snowflake | `T.ORANGE` | That backend's **provider badge** (new element, see below) | REQ-ENG-016, backend-wide |
| Queued for a run slot | hourglass + numeral | — (no tether: waiting on the scheduler, not a node) | — | A **queued ring**: a segmented dashed ring with a position number at the node ("Q3") instead of a line to anywhere |

Tether color rule: **gold is reserved for "the human is the blocker"** (waiting-on-human, and
blocked-when-only-the-human-can-unblock) — everywhere else the tether takes the *waiting* agent's own
role color, so several tethers converging on one busy reviewer stay visually separable by whose they are,
rather than all blending into one reviewer-colored knot.

### Provider badges (new element)
Small fixed badges — one per backend actually in use (claude / codex / local), positioned along the
scene's outer edge (evenly spaced, bottom arc, so they don't compete with the agent ring for the
center). Normally just a quiet glyph + label. When REQ-ENG-016 rate-limits that backend, the badge
lights `T.ORANGE` and shows the reset countdown ("resets 14:05") — every agent on that backend grows a
rate-limited tether pointing at it. This supersedes `design/stage.md`'s earlier plain "desaturate toward
cyan" throttled-node treatment — the tether + shared badge is more informative (shows *which* backend,
and the same countdown the top-bar pill already gives when a project is active), and reuses the same
copy convention as `design/system.md`'s REQ-ENG-016 pill.

## Layer 3 — mail backlog

A short arc of small envelope pips just outside the node's ring (opposite side from the activity
caption, so roughly the upper arc). One pip per unread message, up to 5; beyond that, a single numeral
badge ("12") replaces the pips — this mirrors the app's existing unread-count convention
(`ui.badge`) rather than inventing a new one.
- **Queued, not yet delivered** (waiting for the agent's next wake): hollow/outline pips, `T.TEXT_FAINT`
  — quiet, background information.
- **Being read right now** (delivered as part of the agent's current run): the same pips render filled
  and bright for the duration of that run, then disappear as they're marked read — a brief, legible
  "this mail just got picked up" moment rather than a silent state change.

## Layer 4 — model chip

A small pill beneath the node's name (Pulse: always; Stage: only at ≤9 agents, see Density), reading
`<provider glyph> <model> · <level pips>`, e.g. `◆ opus · ●●●○`, `▲ gpt-5.x · ●●●○`, `● qwen3.8-27b ·
●●○○`.

| Provider | Glyph | Color | Note |
|---|---|---|---|
| Claude | `◆` | `T.ACCENT` (indigo) | Same association already established by the top-bar rate-limit pill in `design/system.md` |
| Codex | `▲` | `T.CYAN` | Freed up by retiring the old cyan "throttled" node meaning (Layer 2) |
| Local | `●` | `T.TEXT_DIM` | Deliberately neutral/desaturated — local is the unbranded default, not a fourth vivid hue |

These are small badge glyphs, never the node's ring color (which stays the agent's *role* color) — no
collision between "who" (role, on the ring) and "what it's running on" (provider, on the chip).

**Level pips:** four dots, filled count = effort level (low=1 … max=4; Codex's ceiling is `xhigh`,
still renders as 4/4 with the tooltip spelling it out). Filled `TEXT`, empty hollow `TEXT_FAINT`.

Reads live off the current agent config snapshot — when team.yaml changes (#20) and the engine picks it
up, the chip updates on the next normal data refresh, no special-casing needed.

## Layer 5 — Work panel

**Pulse:** a docked panel beside the scene (right side, similar width convention to the existing
human-inbox column). **Stage:** no panel (chrome-free) — folded into the ticker instead (see below).

### Milestone header (Pulse panel top)
Current milestone name, a progress bar (e.g. "▓▓▓▓▓░░░░ 2/9 merged"), and the count remaining.

### Task rows
One row per in-flight task. Default filter: **major** (P0/P1, or part of the current milestone) with a
toggle for **all in flight**. Each row:
- `#id` + title (truncated).
- Assignee avatar + handle.
- A **stage-track** widget: four stops — Building → Review → Checks → Merged — connected by a line;
  stops up to and including the current one are lit (filled + bright connecting line), the rest dim.
  This is a new small component (first use here; a natural fit for the Board later too, out of scope
  now).
- Age in the current stage (`ago()`).
- What it's waiting on, if anything — same vocabulary and coloring as Layer 2's tethers (e.g., a small
  gold "waiting: you" or role-colored "waiting: qa" tag), so the language is consistent whether you're
  reading it on the node or in the panel.

### Linking (panel ↔ scene)
Hovering or clicking a row highlights that task's assignee node, its active tethers, and any in-flight
comets touching it in the scene (brighten those, dim everything else slightly). Hovering a node does the
reverse — highlights its row in the panel. This is what ties "who" (the scene) to "what" (the panel)
together, per the brief.

### Completion
On merge, the row plays the same burst effect as the scene's task-card merge burst (`design/stage.md`),
then slides out of the list — one visual event, not two separate ones happening independently.

### Stage's ticker version
Stage has no docked panel, so the Work panel's content folds into the **single bottom ticker** instead
of adding a second strip (keeps Stage's "chrome-free, calm" principle — one ticker, not two competing
for the same real estate). The ticker's five held slots (`design/stage.md`'s existing "notable events"
ticker, REQ-GUI-035) now draw from two pools: **event** entries (merges, rejections, decisions, etc. —
unchanged) and, on a slower cadence, **work** entries — roughly every 4th ticker slot is a work line
instead of an event line: either the milestone summary ("Ready for a test project — 2/9 merged, 3
remaining") or a compact inline stage-track for one in-flight task ("#14 → building → **[review]** →
checks → merged"). Same visual treatment (stacked-fade, brightness-by-recency) as event entries — the
viewer shouldn't have to learn a second ticker style, just notice the content varies.

## Full state legend

The complete answer to "every agent state has a distinct, named treatment":

| State | Silhouette | Tether/badge | Age shown | Model chip |
|---|---|---|---|---|
| Idle | Calm breathing glow | none | no | yes (Pulse; glyph-only in Stage @ >9) |
| Working | Pulsing arc + activity caption | none | no (caption instead) | yes |
| Waiting on human | Dashed ring + `?` badge | Gold tether → YOU | yes | yes |
| Waiting on review | Dashed ring + check-pending badge | Role-color tether → reviewer | yes | yes |
| Blocked | Dashed ring + chain-link badge | Role-color (or gold) tether → owner/YOU | yes | yes |
| Rate-limited | Dashed ring + snowflake badge | Orange tether → provider badge | yes (on badge) | yes (shows the limited provider) |
| Queued for a run slot | Segmented dashed ring + "Q3" | none (queue position, not a target) | implicit (position) | yes |
| Parked (owes work) | Solid amber ring + `!` badge | none | yes | yes |

## Density: 3, 8, 12(–16) agents

Reuses `design/stage.md`'s existing ring-layout rule (single ring ≤9; two rings above 9, inner = fixed
roles, outer = builders/overflow), now confirmed against REQ-GUI-031's finalized bound of **16 agents
without overlapping nodes or labels** — the inner/outer split (inner capped at the ~6 fixed roles, outer
taking the rest) comfortably covers 16 (6 inner + 10 outer) without changing the rule.

What changes with density is **how much of each layer renders per node**, not the layout math:
- **n ≤ 9 (single ring), Pulse or Stage:** full model chip, full mail pips, tether age timers as text.
- **n > 9 (two rings):** model chip shrinks to glyph + pips only (drop the model name text — it's
  available on hover); mail pips beyond 3 collapse straight to the numeral form (skip the 4th/5th
  individual pip) to keep the ring uncluttered; tether age timers show on hover only, not as
  always-on text. This is a straightforward extension of Pulse-vs-Stage's own density dial (above) —
  applied a second time, by node count instead of by view.
- **n = 3:** no compression needed in either direction; if anything, nodes render at their max clamp
  size (`design/stage.md`'s sizing table) since there's headroom.

## Wireframes

### Pulse (in-tab), 8 agents, windowed
```
┌───────────────────────────────────────────────────┬────────────────────────┐
│                                                     │ READY FOR A TEST PROJ. │
│                                                     │ ▓▓▓▓▓░░░░░  2/9 merged │
│                lead                 pm             ├────────────────────────┤
│            ┄┄┄(?)┄┄┄┐          ◆ opus · ●●●○        │ #14 Add global zoom     │
│                       \                             │  ● builder_1@troupe    │
│                        \                            │  bld→[rev]→chk→mrg     │
│     spec ───────────── YOU (2❓) ──────────── designer│  8m · —                │
│  ◆ sonnet · ●●○○      gold breathing ring    ▲ gpt-5.x│├───────────────────────┤
│                        /│\               ·●●●○·      │ #12 Fix rate limiting  │
│                       / │ \                          │  ● builder_2@troupe    │
│              builder-1  │  builder-2                 │  [bld]→rev→chk→mrg     │
│           [working: git]│  ┄┄┄(❄)┄┄┄ orange           │  waiting: claude ❄     │
│           ◆ opus·●●●○   │        \                   ├────────────────────────┤
│                         │         provider badge:     │ □ show all in flight   │
│           qa            │        claude — limited 14:05                       │
│      ┄┄(✓)┄┄ builder-1  │gadfly                       │                        │
│      ● sonnet·●●○○      │ [idle]                      │                        │
│                         │ ● haiku·●●○○                │                        │
├─────────────────────────┴─────────────────────────────┴────────────────────────┤
│ ACTIVITY   All  Messages  Tasks  Runs  Questions  Decisions                     │
│  10:41:02  ● builder_1@troupe → qa   sent #14 for review                       │
│  10:40:55  ● lead merged #9                                                     │
└──────────────────────────────────────────────────────────────────────────────┘
```
(`┄┄┄` = dashed tether, `(?)`/`(❄)`/`(✓)` = waiting-state glyph badges on a node, `·` marks pip rows.)

### Stage (full-screen), 8 agents
```
 troupe · myproj                                                    (fades after 2s, corner mark)



                    lead                                    pm
                ┄┄┄(?)┄┄┄┐                            ◆ opus ●●●○
                          \
        spec               \
     ◆ sonnet          YOU ● (2❓ — slow gold breathing pulse)          designer
                          /│\                                      ▲ gpt-5.x ●●●○
                         / │ \
                builder-1  │  builder-2
            [working: git] │  ┄┄┄(❄)┄┄┄→ [claude — limited 14:05]
                           │
                  qa    ┄┄(✓)┄┄  gadfly
             [rev: #14]           [idle]

                         ▤▤▤ ready queue (3)
──────────────────────────────────────────────────────────────────────────────
 10:41  builder_1@troupe sent #14 for review
```
No side panel, no top bar — the Work panel's content only appears in the ticker's rotating work-line
slot (not shown in this static frame).

## New components (not new colors)
- **Tether**: a dashed, slowly-scrolling line — new motion primitive, first use here.
- **Provider badge**: small fixed scene element for claude/codex/local, lights up + shows a countdown
  when REQ-ENG-016 fires.
- **Queued ring**: a segmented dashed ring + position numeral, distinct from the tether-bearing dashed
  ring (no target, so no line).
- **Mail pips**: small envelope-glyph arc, hollow-vs-filled for queued-vs-reading.
- **Model chip**: provider glyph + model + 4-pip level meter.
- **Stage-track**: 4-stop lit pipeline widget, used in Work panel rows and the Stage ticker's compact
  form.

No new RGB values: gold reuses the existing YOU-glow hue, role colors reuse the 7-role palette, ORANGE/
CYAN/ACCENT/TEXT_DIM/TEXT_FAINT are all existing `design/system.md` tokens applied to new referents
(provider identity, waiting-tether meaning) — consistent with how Stage and Decisions have each done
this already.

## Supersedes in design/stage.md
This doc's Layer 2 legend replaces `design/stage.md`'s original four-state agent table (idle / working /
parked / throttled) with the richer eight-state one above, and Layer 1's warm-edge rule extends its
comet design. `design/stage.md` is being updated alongside this doc to point here rather than duplicate.