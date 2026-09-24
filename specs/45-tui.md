# TUI: the per-project terminal dashboard

Status legend: **[x]** implemented · **[ ]** not yet · **[~]** partial.
Code: `src/troupe/tui/` (Textual app), `src/troupe/cli.py` (default command). Task: #66.

> The human, 2026-09-24: "in a tmux terminal window, I want to do troupe-project or something similar; the TUI for the
> project will have status for that specific project, something simple that shows mailboxes, comms, tasks, maybe a
> little chat window so I can talk to PM."

The interface model (pm decision "Interface model…"):
- **TUI:** one project, in a terminal, next to the work. The human's everyday seat.
- **PM:** the human's single point of contact (REQ-COM-027..029).
- **Mac app:** the cross-project portfolio view (specs/60-mac-app.md).
All three are clients of the same engine API (specs/50-api.md).

**Priority (human, 2026-09-24): the TUI is the primary interface.** The raylib GUI is frozen, and the Mac app is
deferred. The lead builds #66 in four slices:
| slice | REQs |
|---|---|
| 1. Shell + status | TUI-001, 002, 003, 010 (header, Team, Tasks), 011, 012, 013, 030, 031 (its parts) |
| 2. Needs you | TUI-010 (Needs you pane), 020 (`a` answering) |
| 3. PM chat | TUI-021 |
| 4. Feed + kill switch | TUI-010 (Comms), 020 (`s`) |
Each slice ships its own tests and SVG snapshots, and flips its REQs to [x].

## Launch
- **REQ-TUI-001 [ ]** `troupe` with no arguments (or `troupe tui`) in a project directory:
  - starts the service if it isn't running (`troupe start` semantics, REQ-ENG-006), then opens the TUI;
  - `troupe --project <path>` targets another project;
  - outside any project, it prints how to `troupe init` and exits 1.
  - **`q` quits the TUI and never stops the team.**
  - Test: the service starts when absent; `q` leaves it running.
- **REQ-TUI-002 [ ]** Data only through the API: hello, snapshot, then subscribe (REQ-API-010/020/060).
  - No direct DB reads and no polling.
  - Live updates appear within 1 s of a new message or task change.
  - A service reload or restart reconnects without clearing the screen (as REQ-MAC-012).
  - Methods the API reports as `unavailable` (REQ-API-006) are hidden or disabled, never errors.
  - Test: against a fixture API server, a pushed event updates the pane within 1 s.
- **REQ-TUI-003 [ ]** One new dependency, `textual`: the standard for rich, tmux-safe Python TUIs, and the human asked
  for a TUI. No other additions without a reason in the task summary.

## Layout
- **REQ-TUI-010 [ ]** Panes, dense and calm:
  - **Header:** project, engine state (REQ-GUI-029 states, including Stopped), active milestone progress
    (REQ-ENG-045), and provider usage meters (REQ-BE-011/014).
  - **Team:** one line per agent: handle, state (working / idle / waiting-on-X from `waiting_on`, REQ-ENG-046),
    current activity, and a mail count.
  - **Tasks:** in-flight tasks grouped by stage, ★ human requests first (REQ-ENG-048).
  - **Comms:** agent→agent mail subjects, decisions and merges, newest at the bottom, sticky to the bottom
    (REQ-GUI-017 rules).
  - **Needs you:** open cards, including escalations the PM forwarded, credited "via pm_1 from …".
  - **Chat with the PM:** an input line plus the last few messages, with a streaming "PM is working…" line
    while the PM runs.
- **REQ-TUI-011 [ ]** Sizes and terminals:
  - Usable at **80×24**: panes collapse to tabs. It scales to full screen.
  - Works inside tmux and over SSH.
  - Honors the terminal's color scheme, with role colors from `design/system.md` mapped to the nearest terminal
    color. No meaning carried by emoji or color alone.
  - Mouse is optional; everything works from the keyboard.
- **REQ-TUI-012 [ ]** Copy anything. The human's copy/paste pain must not come back.
  - The terminal's native selection works over every pane (the Textual/terminal modifier-drag, documented in the
    footer help).
  - `y` copies the focused item (message, card, task brief) to the clipboard via OSC 52, which works in tmux and
    over SSH, and shows a "Copied" flash.
  - Test: `y` on a focused message emits an OSC 52 sequence with its exact text.
- **REQ-TUI-013 [ ]** Details and catch-up.
  - Enter on a task, message or agent opens a detail view (brief and notes, full message, agent status and recent
    runs). Esc goes back.
  - When the TUI starts, or refocuses after ≥10 min away (`human_last_seen`, REQ-GUI-028), a one-line "Since you
    looked: 2 merged, 1 blocked, 3 decisions" appears. Enter opens the list.
  - The TUI connects with `notifications: false` (REQ-API-010), so the engine keeps sending OS notifications while
    the human is elsewhere in tmux.

## Keys
- **REQ-TUI-020 [ ]** Key bindings (shown in a footer):

  | key | action |
  |---|---|
  | Tab / Shift-Tab | cycle panes |
  | `/` | focus the PM chat |
  | Enter | send |
  | `a`, then 1–9 or free text + Enter | answer the focused Needs-you card (REQ-COM-025 rules: never while typing in chat) |
  | `s` | Stop everything (REQ-SAFE-010), after a y/N confirmation |
  | `q` | quit the TUI only |
- **REQ-TUI-021 [ ]** Chat with the PM works end to end: send, the streaming working state, and the reply shown
  (REQ-COM-020). Chatting with another agent is possible (`:chat <handle>`), but the default is always the PM
  (REQ-COM-029).

## Verification
- **REQ-TUI-030 [ ]** Snapshot mode: `TROUPE_SHOT=/path.svg troupe tui` renders once data has loaded (via Textual's
  `save_screenshot`), writes the SVG and exits 0. `TROUPE_API_FIXTURE` works as in REQ-MAC-051. UI tasks attach
  SVGs at 80×24 and full screen.
- **REQ-TUI-031 [ ]** `tests/test_tui.py` uses Textual's pilot against a fixture API server. It covers:
  - launching and quitting (team still running);
  - a live update within 1 s;
  - sending a chat;
  - answering a card by key;
  - the kill switch asking for confirmation;
  - the 80×24 tab layout.

## Out of scope
- Cross-project views (that's the Mac app).
- Editing settings.
- Board drag-and-drop.

## Changelog
- 2026-09-24 — written (human request via pm msg #418; task #66).
- 2026-09-24 — TUI made the primary interface; slice plan; new TUI-012 copy (OSC 52 plus native selection) and
  TUI-013 details and catch-up.
