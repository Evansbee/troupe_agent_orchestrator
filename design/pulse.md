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

All Dribbble shot pages I tried failed to render (JS-heavy pages, known issue) — four leads
(`dribbble.com/shots/25510763`, `/27561937`, `/18901191`, `/26830008`, plus the tag pages
`ai-agent-dashboard` and `constellation-animation`) are noted for manual browsing but **not** cited
below since I never saw the actual visuals. The five below I have verified real page content for.

1. **["Give Your AI Agent a Living Cosmic Interface"](https://hellotrillion.ai/p/cosmic-orb-ui)** — a
   voice-reactive agent UI: sub-agents orbit a central orb, and light up with a trail-beam + pulse-ring
   burst *at the moment they're dispatched*, then visually dock near the panel showing what they're
   working on. **Idea taken:** the "dock near their panel while they work" pairing is stronger than a
   hover-only link — Layer 5 below adds a persistent (not just hover) thin connective line from a
   working node to its own Work-panel row, so the pairing is visible at a glance, not just on interaction.
2. **[Grafana Node Graph panel docs](https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/node-graph/)**
   — Grafana's network-topology panel encodes connection *type* as dashed-vs-solid strokes and traffic
   *volume* as edge thickness. **Idea taken:** validates the dashed(waiting)-vs-solid(spoke) split
   already in Layer 2, and adds a new one — Layer 1's warm-edge spokes now scale thickness with recent
   message volume on that edge, not just brightness, giving "who talks to whom a lot" a second visual
   dimension.
3. **[Fuselab Creative — CyberDefend case study](https://fuselabcreative.com/our-projects/cyberdefend/)**
   — a satellite-ops dashboard where clicking a flagged node surfaces its correlated detail inline,
   spatially anchored to the node, never a disconnected modal. **Idea taken:** confirms the no-modal,
   inline-linking approach already chosen for Layer 5's panel↔scene hover, and extends it — Layer 5 now
   specifies a **click** (not just hover) pins that link and scrolls/expands the panel row, for
   deliberately inspecting one agent rather than only transient hover.
4. **[Linear's UI redesign write-up](https://linear.app/now/how-we-redesigned-the-linear-ui)** — their
   dark surfaces are generated as LCH-derived elevation steps off one base color, not hand-picked grays,
   which is why stacked dark panels read as one material. **Idea taken:** principle-level validation for
   the scene background / Work-panel dock / ticker strip relationship — they should read as elevation
   steps of one surface (which `design/system.md`'s `PANEL`/`PANEL2`/`PANEL3` progression already
   approximates), not three separately-designed panels.
5. **[Vercel's dashboard redesign post](https://vercel.com/blog/dashboard-redesign)** — deployment state
   surfaces in the browser tab's favicon itself, so status is visible even unfocused. **Idea taken:**
   supports (doesn't replace) the already-speced but not-yet-designed REQ-GUI-027 dock badge/icon — a
   future pass on that requirement should consider reflecting "someone is waiting on you," not just the
   open-question count, echoing this ambient-outside-the-window principle.

(One further lead, Raycast's hairline-border/no-shadow card treatment, surfaced only in unverified search
snippets — not fetched directly, so not cited as a source, though it's a reasonable thing to eyeball
manually before committing to glow-heavy treatments everywhere.)

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
- **Thickness signals volume, not just recency** (borrowed from Grafana's Node Graph edges, see
  references below): a spoke's line thickness scales with how many messages have crossed it in the last
  3-minute warm window (capped at a modest max thickness so a chatty pair doesn't dominate the scene).
  Combined with the fade above, a glance answers both "who talked recently" (brightness) and "who talks
  a lot" (thickness).

## Layer 2 — who's waiting on whom

The core addition, now specified precisely against REQ-ENG-046's `waiting_on` data model: `{kind,
target, since, reset_at?, queue_position?}`, or null. Every agent is in exactly one of **ten** named
states at any moment: idle, working, seven `waiting_on` kinds, or parked. Two-tier legend,
deliberately: a **coarse silhouette** (idle / working / waiting / parked) readable from across a room
without color vision or close attention, and a **fine treatment** (which kind, tether target, age)
readable up close or in Pulse's tighter viewing distance.

### Coarse silhouette (the four shapes)
| Shape | Meaning |
|---|---|
| Calm breathing glow, no ring | Idle |
| Pulsing arc + activity caption | Working |
| Static **dashed** ring | Waiting on something specific (six kinds below have a real target) |
| Static **solid** ring, warm amber, `!` badge | Parked — owes work, waiting on nothing in particular (REQ-GUI-002; also a `waiting_on` kind, per REQ-ENG-046, just one with no target) |

Dashed vs. solid is the load-bearing distinction: dashed means "there's a specific thing this is waiting
on, follow the tether to see what"; solid amber (parked) means "this agent should be doing something and
isn't, but there's no single blocker to point at."

### The seven `waiting_on` kinds (fine treatment)
Precedence order matches REQ-ENG-046 exactly (only one kind applies at a time — a task could technically
be both in review and rate-limited, but `human` > `review` > `dependency` > `blocked` > `providers` >
`rate_limit` > `slot` decides which one shows). Each gets a dashed ring (coarse signal above) plus a
small glyph badge on the node, an age timer since `since` (`ago()`-style, e.g. "12m"), and — for the six
with a real target — a colored dashed **tether**: a "marching ants" dash pattern scrolling along the
line at a slow, constant ~12px/s (fast enough to read as "still pending," slow enough not to compete
with comet motion).

| Kind | Glyph | Tether color | Tether target | When |
|---|---|---|---|---|
| `human` | `?` | Gold (same hue as YOU's question glow) | YOU | Open question, or a task awaiting a human-approval card |
| `review` | small checkmark-pending mark | The waiting agent's own role color | The reviewer node(s) | Task is in `review` |
| `dependency` | chain-link | The waiting agent's own role color | The unmet dependency task's current assignee node, or the ready-queue tray if it's unassigned | Assigned task has unmet `depends_on` |
| `blocked` | chain-link with a small break/`×` through it (distinguishes from `dependency` at a glance, per spec's "shared glyph, distinguished by label and target") | — (no external target) | **None** — a short self-referential stub instead of a line, since the target is a task id + free-text reason, not another node | Task status is `blocked` |
| `providers` | broken-link/⊘ | `T.RED` (more severe than a single rate-limit) | **All** of that agent's configured provider badges at once (a small fan of tethers, not one) | Every provider in its fallback list is unavailable (REQ-BE-012) |
| `rate_limit` | snowflake | `T.ORANGE` | That single provider's **badge** (new element, see below) | Its current provider is limited (REQ-ENG-016) |
| `slot` | hourglass + numeral | — (no tether: waiting on the scheduler, not a node) | — | Has a wake candidate but no free run slot — a **queued ring** instead: a segmented dashed ring with the 1-based `queue_position` at the node ("Q3") |

Notes on the two pairs that are easy to conflate:
- **`dependency` vs. `blocked`:** same base glyph (chain-link) since both are "something else has to
  happen first," but `blocked` gets no tether (there's no single other node to point at — the reason is
  free text on the task note) while `dependency` does (there's a concrete other task/assignee). The
  label under the node always spells out which ("waiting on #14" vs. "blocked: needs prod access"), so
  the distinction never depends on noticing the glyph break alone.
- **`providers` vs. `rate_limit`:** `providers` is strictly worse (nothing this agent can run on right
  now, anywhere) and reads that way — red, broken-link glyph, fans out to every configured provider's
  badge — versus `rate_limit`'s single orange tether to one badge. Both show a reset countdown (the
  earliest `reset_at` across providers, for `providers`).

Tether color rule: **gold is reserved for `human`** — everywhere else a tether takes the *waiting*
agent's own role color (except `providers`/`rate_limit`, which are colored by severity, not role, since
the backend is the point), so several tethers converging on one busy reviewer stay visually separable by
whose they are, rather than all blending into one reviewer-colored knot.

### Provider badges (new element)
Small fixed badges — one per backend actually in use (claude / codex / local), positioned along the
scene's outer edge (evenly spaced, bottom arc, so they don't compete with the agent ring for the
center). Normally just a quiet glyph + label. When REQ-ENG-016 rate-limits that backend, its badge
lights `T.ORANGE` and shows the reset countdown ("resets 14:05"); if every backend an agent can fall
back to is out (the `providers` kind), that agent's tethers fan out to all of its badges at once, each
lit however that individual backend currently reads (some may be `T.ORANGE`/limited, others just
generically unavailable). This supersedes `design/stage.md`'s earlier plain "desaturate toward cyan"
throttled-node treatment — the tether + shared badge is more informative (shows *which* backend(s), and
the same countdown the top-bar pill already gives when a project is active), and reuses the same copy
convention as `design/system.md`'s REQ-ENG-016 pill.

## Layer 3 — mail backlog

A short arc of small envelope pips just outside the node's ring (opposite side from the activity
caption, so roughly the upper arc). Reads directly off REQ-ENG-046's two published counts. One pip per
unread message, up to 5; beyond that, a single numeral badge ("12") replaces the pips — this mirrors the
app's existing unread-count convention (`ui.badge`) rather than inventing a new one.
- **`mail_queued`** (unread, not yet delivered to a run): hollow/outline pips, `T.TEXT_FAINT` — quiet,
  background information.
- **`mail_reading`** (delivered to the currently running run): the same pips render filled and bright
  for the duration of that run, then disappear as they're marked read — a brief, legible "this mail just
  got picked up" moment rather than a silent state change.

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
collision between "who" (role, on the ring) and "what it's running on" (provider, on the chip). **Shape
is the primary signal, color secondary:** with only 3 provider identities against 7+ role hues sharing
the same small set of tokens, a coincidence is possible (a Spec-role agent, cyan ring, running on Codex,
also cyan badge) — the diamond/triangle/circle glyph shapes disambiguate regardless, the same way
`PRIORITY_COLORS` already reuses `RED` for P0 even though it's also Gadfly's role color elsewhere. Don't
chase perfect hue-exclusivity across two independent 3- and 7-entry palettes sharing one token set;
shape-code and move on.

**Level pips:** four dots, filled count = effort level (low=1 … max=4; Codex's ceiling is `xhigh`,
still renders as 4/4 with the tooltip spelling it out). Filled `TEXT`, empty hollow `TEXT_FAINT`.

**Fallback marker:** when an agent is running on anything other than its first-choice provider (the
provider-fallback ordering from team.yaml, REQ-BE-011/012), the chip gains a small italic "fallback"
suffix / secondary-color tag after the pips — e.g. `▲ gpt-5.x · ●●●○ fallback`. This is the visible
counterpart to the `providers`/`rate_limit` waiting kinds in Layer 2: those show *why* a switch might be
needed, this shows *that* one already happened.

Reads live off the current agent config snapshot — when team.yaml changes (#20) or the active provider
changes (fallback triggers), the chip updates on the next normal data refresh, no special-casing needed.

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
Hovering a row highlights that task's assignee node, its active tethers, and any in-flight comets
touching it in the scene (brighten those, dim everything else slightly); hovering a node does the
reverse. This is what ties "who" (the scene) to "what" (the panel) together, per the brief.

Two refinements, each borrowed from a specific reference (see Inspiration references below):
- **A persistent link, not just hover:** every node with an attached task gets a faint, static thin
  line to its own Work-panel row (not the bright hover-highlight — just enough presence that the
  pairing is visible without interacting). Borrowed from the "dock near their panel while they work"
  pattern in the cosmic-orb reference — hover-only linking makes you guess-and-check which row belongs
  to which node; a quiet permanent thread removes the guessing.
- **Click pins it:** clicking (not just hovering) a node or row locks the highlight and scrolls/expands
  that row, for deliberately inspecting one agent rather than a transient glance — borrowed from the
  CyberDefend case study's "click a node, its detail surfaces inline, spatially anchored" pattern.

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

The complete answer to "every agent state has a distinct, named treatment" — ten states: idle, working,
the seven REQ-ENG-046 `waiting_on` kinds, and parked (itself an eighth `waiting_on` kind, with no
target).

| State | Silhouette | Tether/badge | Age shown | Model chip |
|---|---|---|---|---|
| Idle | Calm breathing glow | none | no | yes (Pulse; glyph-only in Stage @ >9) |
| Working | Pulsing arc + activity caption | none | no (caption instead) | yes |
| `human` | Dashed ring + `?` badge | Gold tether → YOU | yes | yes |
| `review` | Dashed ring + check-pending badge | Role-color tether → reviewer | yes | yes |
| `dependency` | Dashed ring + chain-link badge | Role-color tether → dependency's assignee (or ready-queue tray) | yes | yes |
| `blocked` | Dashed ring + broken chain-link badge | none (label carries the reason) | yes | yes |
| `providers` | Dashed ring + broken-link/⊘ badge | Red tethers → **all** configured provider badges | yes (earliest reset) | yes (shows "fallback" if currently on a non-first choice) |
| `rate_limit` | Dashed ring + snowflake badge | Orange tether → the one limited provider's badge | yes (on badge) | yes |
| `slot` | Segmented dashed ring + "Q3" | none (queue position, not a target) | implicit (position) | yes |
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