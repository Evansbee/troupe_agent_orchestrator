# Swift spike (#49) — design fidelity punch list

Review of `/tmp/pm-mac-pulse.png` and `/tmp/pm-mac-board.png` (`TROUPE_API_FIXTURE=demo3.jsonl`)
against `design/pulse.md` and `design/system.md`. The plumbing works — real data, real layout,
real navigation — but the *look* falls well short of "don't lose the sexiness." Most of what's
missing is motion and the four information layers pulse.md defines, not detail polish.

P0 = must fix before the human sees it (reads as broken, or as if the four things they explicitly
asked Pulse to show aren't there at all). P1 = needed for full fidelity, doesn't block a first look.

**Approved by pm (msg #353), with 3 promotions to P0** beyond my original 5: waiting tethers and
mail pips (items 6/7 below — "the human explicitly asked for Pulse to show 'who's waiting' and
'mail backlog'... those are their requests, run to the ground, not polish"), and native materials/
SF Symbols (item 8 below — "if it doesn't feel more native than raylib, the switch hasn't paid
off"). Renumbered below to reflect the full 8-item P0 set.

## P0 — must fix first

**1. Header shows the literal template string `"[project] Demo"`.**
Not a style gap — a data-binding bug, and the single most "this isn't finished" thing on screen.
*Expected:* the project name substituted, matching the raylib top bar's `"troupe — <project>"`
pattern (`design/system.md` Color/top-bar notes). *Current:* `[project] Demo` verbatim.

**2. Board: ~40% of the window is empty, columns vertically centered instead of top-anchored.**
*Expected:* every raylib view fills its container top-down from just below the tab bar
(`board_view`: `area = r.inset(12,12)`, columns start at `area.y`) — nothing in the app centers
content vertically. *Current:* columns float in the middle of the window with a large dead band
above them. *SwiftUI note:* almost certainly a default-centered `VStack`/`Spacer` — pin the column
row to the top with `.frame(maxHeight: .infinity, alignment: .top)`, don't center it.

**3. Board: the Done column is clipped at the window edge with no way to reach it.**
*Expected:* raylib proportionally divides the full width across exactly 5 columns
(`cw = (area.w - gaps) / ncol`) so all five are always visible — the Header rows: space allocation
rule in `design/system.md` applies here too: don't let a fixed-width layout run content past the
container edge. *Current:* Done is cut off mid-card ("Ship t…"), no scroll affordance, no
indication there's more. *SwiftUI note:* either divide width proportionally like raylib
(`geometry.width / 5`, minus gaps) or wrap in `ScrollView(.horizontal)` if a minimum column width
is preferred — either is fine, silently clipping with no way to reach the content is not.

**4. Messages render as bare static dots — no comet motion, no label, no direction.**
*Expected* (`design/pulse.md` Layer 1): a bezier flight with an asymmetric head/tail (obvious
direction), a label pill carrying the subject, 2.0s duration, plus a warm residual spoke-glow on
the direct edge afterward. *Current:* two isolated indigo dots floating in open space, no arc, no
label, no visible motion in a static capture. This is one of the four things the human explicitly
asked for ("message passing") — its total absence, not just rough styling, is why this is P0.

**5. No ambient motion anywhere — every node is a flat, static ring.**
*Expected* (`design/pulse.md`'s coarse silhouette / `design/system.md` Motion): idle = slow
breathing glow, working = pulsing arc + activity caption. *Current:* `LD`/`QA`/`B1` are static
outlined circles with no glow, no pulse, regardless of state. This is very likely *the* reason the
spike reads as "looks worse than raylib" per pm/lead — a static node-link diagram instead of a
living scene. *SwiftUI note:* this is exactly the job pm's own plan assigned to `TimelineView` +
`Canvas` — worth confirming directly whether that's wired up at all yet, since a total absence
(not just under-tuned glow) suggests it may not be started rather than merely wrong.

**6. Waiting states / tethers are absent.** (`design/pulse.md` Layer 2 — human explicitly asked
Pulse to show "who's waiting.") The sidebar text says "qa_1@demo — parked, owes work," but the QA
node itself just shows a slightly heavier double ring — no `!` badge, no age timer, none of the
ten-state legend's distinct treatments. The fixture doesn't exercise the other seven `waiting_on`
kinds, so they're unverified rather than confirmed missing — extend the demo fixture to seed one of
each (mirrors `design/stage.md`'s `TROUPE_STAGE_DEMO=1` pattern) so this can actually be checked;
the engine data already exists (#50 wait snapshots), so this is a rendering gap, not a data gap.

**7. No mail-backlog pips.** (`design/pulse.md` Layer 3 — human explicitly asked for "mail
backlog.") Not visible on any node in either shot.

**8. No native materials or SF Symbols anywhere — the whole app is flat fills and custom shapes.**
"Native feel + performance" is why the human chose Swift at all (pm, msg #353); a spike that looks
like a reskinned raylib window hasn't delivered on that yet. Concretely: vibrancy on the sidebar and
Work-panel dock (`.background(.regularMaterial)` or a custom vibrant dark material, instead of flat
fills — also a direct implementation of `design/pulse.md`'s own Linear citation about elevation
reading as one coherent material) and SF Symbols for the provider glyphs (Layer 4) and waiting-state
glyphs (Layer 2) instead of hand-drawn shapes — sharper at every scale, free accessibility labels,
and likely faster to build than custom Canvas glyphs.

## P1 — needed for full fidelity

**9. Model chip has the right data, zero visual language.** (`design/pulse.md` Layer 4.) *Expected:*
provider glyph + color (◆ indigo claude / ▲ cyan codex / ● neutral local) + 4-pip effort meter.
*Current:* plain gray text, "claude · opus" / "codex · gpt-5.1-codex," no glyph, no color, no
pips. Glyphs should be SF Symbols per item 8.

**10. Work-panel stage-track is a generic 3-dot progress bar, not the 4-stop lit pipeline.**
(`design/pulse.md` Layer 5.) *Expected:* Building → Review → Checks → Merged, current stage lit,
rest dim, stage labels visible. *Current:* three evenly-spaced dots on a plain bar with no labels
and no clear mapping to the four stages. Also: rows #2 and #4 show no assignee avatar/handle even
though every row should have one — only #3 does.

**11. No activity/event feed below the Pulse graph.** This predates pulse.md (existing
REQ-GUI-011) — raylib's Pulse always has a filterable event feed filling the bottom of the view;
the spike's Pulse has empty dark space there instead. Related to P0 #5's "nothing moves," but
this is missing *content*, not just missing motion.

**12. Sidebar avatars are flat — no glow, no unread-badge language, no "N working" header count.**
(`design/system.md` Avatar component; sidebar header today shows `"{n} working"` next to "TEAM.")
Minor next to the graph's own gaps, but reinforces the same "static, not alive" read.

**13. Milestone progress bar reads low-contrast.** Hard to fully judge color from a screenshot, but
the fill looks closer to a thin gray line than the vivid fill `design/pulse.md`'s Work panel
implies. Worth a contrast pass once the rest lands.

## Remaining native-macOS notes (not promoted, still worth doing)

- **Native source list** for the team sidebar (`List` with built-in selection/hover/keyboard nav)
  instead of custom-drawn rows.
- **Board layout** via `HStack`/`LazyVGrid` with `.frame(maxWidth: .infinity)` per-column columns
  fixes both P0 Board bugs (#2, #3) essentially for free — this should end up *easier* to get right
  in SwiftUI than raylib's manual `Rect` math was, not harder.
- **`TimelineView(.animation)` + `Canvas`** is the mechanism for everything motion-related above
  (P0 #5, comets, tethers) — per pm's own stated plan for the "sexy parts." The finding here is
  that it doesn't look implemented anywhere yet, not that it's implemented and looks wrong — worth
  confirming which of those it actually is before scoping the fix.

## Verification for #59 (pm's addition, msg #353)

Stills can't show motion, and motion is most of what's missing here. #59 isn't done until it can be
*seen* moving, not just described:
- A short screen recording (`screencapture -v -V 8` or an equivalent frame sequence) showing, at
  minimum: idle breathing, a working pulse, and a comet traveling with its subject label visible.
- My sign-off, from actually watching the recording — not from reading a description of what was
  implemented.
- That recording is what pm shows the human as their first look at the Swift direction, alongside
  the stills — so it needs to actually demonstrate the P0 fixes above, not just play something.

## Screenshots referenced
- `/tmp/pm-mac-pulse.png` — Pulse tab, `demo3.jsonl` fixture.
- `/tmp/pm-mac-board.png` — Board tab, same fixture.
Both captured via `TROUPE_API_FIXTURE=Tests/TroupeKitTests/Fixtures/demo3.jsonl swift run
TroupeApp` with the mac/ screenshot env var, from `.troupe/worktrees/t49/mac`.