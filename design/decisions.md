# Decisions tab

Behavior: `specs/40-gui.md` REQ-GUI-041, `specs/20-comms-and-memory.md` REQ-COM-034..037 (+ COM-005
handles, COM-033 pin/edit/delete). This doc is the look. Builds on `design/system.md` tokens and the
existing Memory tab's card language (`views.memory_view`) and Mail's inline-expand pattern
(`views.mail_view`) — Decisions is those two patterns combined, not a new one.

## Principle: the human's steering surface

This is where the human exercises oversight without reading every message. Default to **collapsed and
scannable** — a decision's title, author, and outcome should be readable in the time it takes to
glance down the list; you only pay the cost of a full thread when you choose to open one. Never let
an open thread's height destabilize the list around it (expand in place, don't reflow siblings out
of view).

Suggested position in the tab bar: right after Memory (`TABS` in `app.py`) — decisions used to live
there (per spec's changelog note) and it's the natural sibling now that they have their own view.

## List

One row per decision, newest first, except **pinned decisions float above everything else** regardless
of sort or filter (COM-033) — if there's more than one, they keep newest-first order among themselves.
No separate "PINNED" section header needed at the usual scale (a handful at most); a filled pin glyph
to the left of the title is enough to mark them.

**Row anatomy** (collapsed), closely following `_task_card`'s and Memory's row language:
- Left accent bar, 3px, colored by outcome (see palette below) — absent for decisions with no outcome
  yet (the common case: most decisions just stand).
- Author avatar (role color, initials) + **full handle** (`lead_1@troupe`) — COM-005 requires full
  handles here, never shortened, since this is exactly the "who said what" surface the requirement
  calls out.
- Timestamp (`ago()`), right-aligned, `TEXT_FAINT`.
- Title, `bold`, `TEXT` — truncated to one line; the full title is never worth wrapping a row for.
- Rationale, one line, `TEXT_DIM`, ellipsized (this is the "why," and it's the second most important
  thing after the title — Memory's cards already treat rationale this way).
- A **"MAJOR" tag** (small pill, `TEXT_DIM`) — only shown when the **All** filter is active, since
  under **Major** (the default) it'd be redundant on every row; it exists so All-filter browsing can
  still tell major from minor at a glance.
- An outcome badge (pill, see palette) when one exists; absent otherwise.
- An open-comment indicator: a small speech-bubble glyph + count when the thread has any comments,
  `TEXT_FAINT` normally, `ACCENT` when there's an **open** human comment awaiting a reply (COM-037) —
  that distinction matters, it's the difference between "there's history" and "someone is waiting."
- Hover reveals a right-aligned action cluster (pin / edit / delete, human-only) — see Controls below.

### Outcome badge palette
No new RGB — these reuse `design/system.md`'s existing semantic colors, applied to a new referent
(decision outcomes) the same way Stage reuses `T.CYAN` for a new node state. Following the app's
established convention that a hue can mean different things in different component types as long as
it's never ambiguous within one glyph:

| Outcome | Color | Why this one |
|---|---|---|
| `acknowledged` | `T.GREEN` | the decision stands — same "settled, good" meaning as done/success elsewhere |
| `revised` | `T.CYAN` | content changed in place — "in motion," same flavor as the ready/throttled uses |
| `superseded` | `T.ACCENT` | redirects you elsewhere — indigo already means "the important next thing to look at" (primary buttons, human bubble) |
| `reverted` | `T.RED` | withdrawn — same weight as rejected/blocked elsewhere |

### Superseded / reverted rows
Title renders with a **strikethrough** (see "New primitive" below) and the outcome badge becomes a
link: `superseded → #<successor-id>` / clicking it opens that decision (scrolls it into view and
expands it). This applies under both filters — Major vs. All controls *scope* (which decisions are
listed at all), not whether a superseded/reverted one within scope is shown struck through. A
superseded/reverted decision stays collapsed by default even if it has an unread reply (see below) —
its content is closed, but the thread is still worth a glance, so the unread badge still counts it.

## Filters & controls
Header bar, same shape as Memory's filter row (`ui.chip` row + `ui.hline` below):
- Chips: **Major** (default, selected) / **All** / **Has open comments** (COM-037's "owes a reply"
  set — useful as "what needs me right now").
- **Mark all reviewed** — a small ghost button, right-aligned in the header, advances
  `kv.decisions_seen_at` immediately (same action as leaving the tab, offered explicitly for "I've
  seen enough, clear it now").
- Tab label badge: count of decisions/replies/outcomes newer than `decisions_seen_at`, rendered
  exactly like Chat's unread badge on its center-tab label (`app.draw_center`'s `ui.badge` on the tab).

## New since you looked
- **What counts:** any decision, reply, or outcome with a timestamp after `kv.decisions_seen_at`.
- **Treatment:** the row gets a soft `alpha(T.ACCENT, 0.05)` background tint (subtle — this is
  informational, not an alert) plus a small solid `T.ACCENT` dot beside its timestamp. Inside an
  expanded thread, individual new comments/replies/outcome-events get the same dot beside their
  own timestamp.
- **Persistence for the visit:** compute the highlighted set **once**, on tab entry (or on first data
  refresh after switching to the tab), and hold that snapshot — don't recompute against a live-ticking
  `decisions_seen_at` while the tab stays open, or highlights would disappear mid-read as soon as the
  underlying value advances. Only recompute when the tab is re-entered.
- **Clearing:** advances `decisions_seen_at` on tab-leave (switching to another tab) or on "Mark all
  reviewed" — either clears every current highlight together, not item-by-item.

## Expanded decision (thread)
Clicking a row expands it in place — the same inline-expand affordance Mail already uses
(`views.mail_view`'s `app.expanded` toggle), not a modal (a modal would break the "scan the list"
flow; you want to read one thread while keeping the list's context visible above/below it).

Expanded content, top to bottom:
1. Full rationale/content as markdown (`ui.markdown`), not truncated.
2. If `revised`: a **"Previous version"** block immediately below — the same visual as a markdown
   blockquote (left accent bar, `TEXT_DIM`, per `core.py`'s existing `>` rendering) holding the
   pre-revision title/content, so the history is visible without a separate diff view.
3. The thread itself, in time order. Two entry kinds, visually distinct:
   - **Comments** (human or agent reply) — same anatomy as the Task modal's notes list
     (`views._task_modal`'s Notes section): author line (avatar + full handle + `ago()`) then markdown
     body. The human's own comments get a thin `alpha(T.ACCENT, 0.4)` left bar to distinguish "the
     human said this" from "an agent replied," without going as far as a full chat bubble — this is a
     log, not a live conversation.
   - **Outcome events** — not a comment, a structural marker: a short centered line,
     `"— acknowledged by lead_1@troupe —"` style, in the outcome's color (palette above), smaller
     and quieter than a comment row. It should read as "something changed here," distinct at a glance
     from "someone said something."
4. Composer: a text input + **Comment** button, same shape as the Task modal's note composer
   (`text_input` + primary button), placeholder `"Add a comment…"`. Human-only — agents reply via
   their `update_decision` tool, not this UI.

## Controls (COM-033 — human only)
Hover-revealed icon cluster on the collapsed row (same interaction as the question card's dismiss "×":
`18×18` targets, `T.HOVER` wash, tooltip on hover):
- **Pin** — toggles pinned state; filled vs. outline pin glyph shows current state.
- **Edit** — switches the row into inline edit mode: title becomes a single-line input, rationale/
  content a multiline input (same sizing pattern as the New Task modal's Brief field), with small
  Save/Cancel buttons replacing the action cluster. No separate edit screen.
- **Delete** — destructive, so it goes through the app's one confirm pattern: the existing centered
  modal-over-scrim (`_scrim` + panel, click-outside/Esc to cancel) rather than an inline confirm bar,
  matching how every other consequential action in the app (task cancel, etc.) is guarded.

## Auto-linking `#n`, `REQ-XXX-nnn`, `specs/…`
Visually these render exactly like the `link` inline style already in `ui._spans` (`T.ACCENT`,
underline optional — match whatever chat/docs markdown links already look like today, don't invent a
second link style). The difference is **interaction**: today's `ui.markdown` renders link-styled text
but doesn't hit-test it anywhere in the app (chat, mail, memory links are colored but inert). Decisions
needs these to actually navigate (`#n` → task modal, `REQ-XXX-nnn` → Docs at that heading, `specs/…` →
Docs at that file), so:
- Preprocess decision title/rationale/content text to wrap recognized tokens in normal markdown link
  syntax (`[#12](task:12)`) before handing it to the renderer — this keeps the visual styling free
  (already-built `link` span), and means only Decisions needs new code, not a change to the shared
  markdown component's contract.
- That new code needs to track a **clickable rect per link span** (the existing `_layout_spans` only
  emits draw ops, no rects) — the simplest implementation walks words the same way `_layout_spans`
  already does, using `ui.measure` to accumulate x position and recording a `Rect` for spans tagged
  `link`, then does normal `ui.hover`/`ui.click` against those rects. This is scoped to Decisions'
  renderer; it does not need to become a general `ui.markdown` capability (flag to spec/lead only if
  another view wants clickable links later — not needed for #26).

## Empty state
No decisions match the current filter (most likely: a fresh project with nothing major yet) — use the
illustrated empty-state pattern established for Chat/Inbox (glow + icon + headline + helper copy; see
`design/system.md`'s polish item on bringing Mail/Memory/Docs up to that bar). Icon: reuse the pin or a
simple checkmark-in-circle motif already used elsewhere (`ring` + `line` primitives, no new asset).
Copy: `"No major decisions yet"` / helper: `"Decisions the team flags as major will show up here for
your review."` — adjust copy per active filter (e.g. "No decisions have open comments").

## New tokens / primitives
- **No new RGB values** — outcome badges reuse `T.GREEN`/`T.CYAN`/`T.ACCENT`/`T.RED`; the new-item
  highlight reuses `T.ACCENT` at low alpha, same formula as other tinted-row treatments in the app.
- **New primitive: strikethrough text.** Nothing in `core.py` draws a line through text today (the
  markdown inline styles are bold/code/link/em only, no `~~text~~`). Superseded/reverted titles need
  it. Simplest implementation: after drawing the title text, draw a `1.5px` `ui.line` through its
  vertical center for its measured width — no new markdown syntax needed unless another view wants
  strikethrough later (not required for #26).
- **New interaction: clickable inline links**, scoped to Decisions only (see above) — not a change to
  the shared `ui.markdown`/`core.py` contract.

## Consistency with REQ-GUI-041 / COM-034..037
No disagreements — this design implements the requirements as written. One clarification worth
confirming with spec: COM-036 says a `superseded` outcome is recorded automatically when an agent
supersedes a decision while a human comment is open; visually that should produce **both** the
outcome-event marker in the thread ("— superseded by lead_1@troupe —") **and** the struck-through title
+ successor link on the row, same as a manually-set `superseded` outcome — i.e. there's no visual
difference between an automatic and a manually-set outcome. Flagging only so it's explicit; I don't
think it needs a spec change.