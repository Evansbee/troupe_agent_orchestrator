# TUI visual language

Written fast, for the morning MVP (#66–#69, all P0). Deliberately minimal — just enough that four
panes built in parallel by three different builders converge on one look instead of three. Behavior
is `specs/45-tui.md`; this is the *how it looks* companion, same relationship `design/system.md` has
to the raylib GUI. Ask me for anything this doesn't cover rather than guessing — better one
quick question than four inconsistent panes.

## Color: use the hex values directly, no ANSI table needed

Textual (built on Rich) auto-downsamples truecolor hex to whatever the terminal actually supports
(24-bit → 256 → 16 → 8), so **use `design/system.md`'s existing hex values verbatim** — role colors
(`roles.py`) for agent identity, `theme.py`'s semantic colors (GREEN/YELLOW/ORANGE/RED/CYAN/ACCENT)
for status. No separate terminal palette to maintain; don't hand-map to ANSI-16, Textual does it.

One exception: **REQ-TUI-011 requires no meaning by color alone** (a dark/light or 16-color terminal
may collapse several of our hues toward each other). Every colored state below also gets a stable
glyph or text label — color reinforces, never carries, the meaning.

## State glyphs (not emoji — REQ-TUI-011)

Reuses `design/pulse.md`'s state vocabulary, translated to plain Unicode glyphs a monospace terminal
font renders reliably (no emoji, no font-fallback risk):

| State | Glyph | Color | Label always shown too |
|---|---|---|---|
| Working | `●` (filled) | role color | activity text, e.g. "editing views.py" |
| Idle | `○` (hollow) | role color, dim | "idle" |
| Waiting on human | `?` | `T.YELLOW`/gold | "waiting: you" |
| Waiting on review | `→` | role color | "waiting: qa_1" |
| Blocked / dependency | `✕` | role color | "blocked: #14" |
| Rate-limited / providers | `‡` | `T.ORANGE` (rate_limit) / `T.RED` (providers) | "limited: claude, resets 14:05" |
| Queued for a slot | `…` + number | dim | "queued #3" |
| Parked (owes work) | `!` | `T.ORANGE` | "parked" |
| Merged / done | `✓` | `T.GREEN` | "merged" |
| Rejected / failed | `✗` | `T.RED` | "rejected" |

Same glyph+color+label triple everywhere a state appears (Team pane, Tasks pane, Comms feed) — one
vocabulary, not one per pane.

## Layout

Full screen: two columns. **Left** (narrower, ~30%): Header spans full width above both columns,
then Team and Tasks stacked. **Right** (wider): Comms feed above, Chat-with-PM pinned to the bottom
(input line always visible, same "composer anchored at the bottom" idea as the raylib Chat view).
Needs-you renders as its own bordered pane, gold-accented border (`T.YELLOW`/gold, matching the GUI's
established "needs you" treatment) inserted above Comms whenever it's non-empty — it should be the
first thing the eye catches, not a tab you have to remember to check.

At **80×24** (REQ-TUI-011): panes collapse to tabs. Keep the same tab order as the pane priority
above (Needs you first when non-empty, then Team, Tasks, Comms, Chat) so the collapsed and expanded
layouts agree on what matters most.

Borders: Textual's default rounded-border panels, each with a title matching its pane name ("TEAM",
"TASKS", "COMMS", "NEEDS YOU", "CHAT — pm_1"). Focused pane gets a brighter border in `T.ACCENT`
(matches the GUI's general "this is the selected thing" color) — dim `T.BORDER`-equivalent gray when
unfocused. Don't invent a second focus color.

## Needs-you cards (questions + safety approval cards, #67)

Same shape as the raylib question card and its approval-card variant (`design/system.md`
Components), in text: asker name (role color) + question, numbered options (`1`, `2`, `3`…, matching
REQ-TUI-020's `a` then `1–9` flow) or a free-text prompt, gold-accented border. An approval card
additionally shows the file list and +/− counts inline, with "Open full diff" as one of the numbered
options rather than a separate control — everything actionable goes through the same `a`+number
pattern, no special-cased UI for this one card type.

## Chat pane (#68)

Sender name in role color, message text plain, most recent at the bottom (sticky-to-bottom, same
rule as Comms — REQ-GUI-017). While the PM is running: a single dim, non-bold line "PM is working…"
in place of a reply — no spinner animation needed, Textual's own cursor/loading conventions are fine
here, don't hand-roll one.

## Copy flash (REQ-TUI-012)

No floating toast (doesn't translate well to a terminal) — a brief inline flash in the status/footer
area: "Copied" in `T.GREEN`, then fades back to the normal footer content after ~1.5s. Same meaning
as the GUI's toast, different mechanism because the surface is different.

## Catch-up line (REQ-TUI-013)

One line, top of the Comms pane or just below the header: "Since you looked: 2 merged, 1 blocked, 3
decisions" — plain text, `T.TEXT_DIM`, each clause pointing at its own state color inline (2 in
green-ish, 1 blocked in orange-ish) is a nice-to-have, not required for the MVP cut. Enter opens the
full list per spec; don't build a modal for this, just navigate to/filter the relevant pane.

## Footer / key bindings

Use Textual's built-in `Footer` widget (auto-generated from bound keys) rather than hand-building a
key-hint bar — it already matches this guidance's spirit (plain text, no emoji) and is one less thing
to keep consistent across four builders.

## Not doing right now

No new tokens, no new component types beyond what's listed above — everything here is a direct
translation of existing `design/system.md`/`design/pulse.md` language into text-and-glyph terms.
If a pane needs something not covered here, ask rather than invent — given four people are building
this in parallel today, one inconsistent pane is worse than one short delay for an answer.