# TUI: the per-project terminal dashboard

Status legend: **[x]** implemented · **[ ]** not yet · **[~]** partial.
Code: `src/troupe/tui/` (Textual app), `src/troupe/cli.py` (default command). Tasks: #66–#69. Look and layout:
`design/tui.md`.

> The human, 2026-09-24: "in a tmux terminal window, I want to do troupe-project or something similar; the TUI for the
> project will have status for that specific project, something simple that shows mailboxes, comms, tasks, maybe a
> little chat window so I can talk to PM."

The interface model (pm decision "Interface model…"):
- **TUI:** one project, in a terminal, next to the work. The human's everyday seat.
- **PM:** the human's single point of contact (REQ-COM-027..029).
- **Mac app:** the cross-project portfolio view (specs/60-mac-app.md, deferred until after the TUI and the Links
  benchmark, along with the portfolio coordinator in `docs/architecture/portfolio.md`).
All three are clients of the same engine API (specs/50-api.md).

**Priority (human, 2026-09-24): the TUI is the primary interface.** The raylib GUI is frozen, and the Mac app is
deferred. The lead builds #66 in four slices:
| slice | REQs |
|---|---|
| A (#66). Shell + status + engine lifecycle | TUI-001, 002, 003, 010 (header, Team, Tasks), 011, 012, 013, 030, 031 (its parts) |
| B (#67). Needs you (questions + safety approval cards) | TUI-010 (Needs you pane), 020 (`a` answering) |
| C (#68). PM chat | TUI-021, 012 (in chat) |
| D (#69). Feed + kill switch | TUI-010 (Comms), 020 (`s`) |
Each slice ships its own tests and SVG snapshots, and flips its REQs to [x].

## Launch
- **REQ-TUI-001 [~]** (Ctrl-C + rerun-resumes shipped with #100/#113; the 5 s bound at real project size is #119.)
  "Run and everything runs, quit and everything quits" (human, 2026-09-24). `troupe` with no
  arguments (or `troupe tui`) in a project directory starts the project's engine **as a child of the TUI** and opens
  the TUI on its API.
  - **The entrypoint** (human, 13:31: "start you with `troupe` and the TUI launches"): bare `troupe` in a
    project directory is the primary way to run a team, and help/README/`troupe init` output point to it.
    `troupe up` and `troupe gui` (the frozen raylib GUI) are secondary.
  - **Quit (`q`):** if runs are in flight, it first asks "N agents are working — stop them and quit? y/N". Quitting
    stops the engine and every agent run (process groups plus verified descendants). Interrupted runs are marked
    `interrupted` with their mail re-queued, so they **resume on the next start** (REQ-ENG-004).
  - **SIGHUP / SIGTERM** (terminal closed, `tmux kill-window`) does the same stop without asking: SIGTERM to runs,
    then SIGKILL after 5 s.
  - **Ctrl-C** (human, 13:31: "if i have to kill you and restart, i can ctrl-c and run it again") does the same
    stop without asking, whichever pane or input has focus, including the PM chat input. That covers both the
    key press in the TUI's raw-mode terminal and a real SIGINT. It overrides Textual's default Ctrl-C (copy),
    since copy is `y` plus native selection (REQ-TUI-012). The TUI shows "Stopping… N runs" and exits within
    5 s. A second Ctrl-C while stopping SIGKILLs the remaining runs at once. The exit code is 0 for a clean
    stop. After the stop, `.troupe/engine.lock` is released, `.troupe/engine.pid` is removed, and no backend
    process or descendant of a run is left. In an attached TUI, Ctrl-C only closes the TUI, like `q`.
  - **Rerun resumes:** the next `troupe` in the same directory, after `q`, Ctrl-C, SIGHUP or even `kill -9` of
    the TUI, continues where the team left off (ENG-003 stale-lock recovery, then ENG-004):
    - interrupted runs' mail is re-queued, and each task whose run was interrupted is still `in_progress`
      with the same assignee and its worktree untouched, including uncommitted changes;
    - the assignee is woken for it on the first tick (subject to pause, the kill switch, caps and pacing);
    - the board, chat history, mailboxes, memory and pending Needs-you cards are all intact;
    - the approved safety baseline (REQ-SAFE-021) persists, so a restart never raises a re-approval card;
    - a kill-switch `stopped` state also persists (REQ-SAFE-010), so a restart never silently resumes a team
      the human stopped.
  - **Unattended work:** detach tmux. The TUI, and so the team, keeps running.
  - **Already running:** if an engine already holds the project lock (REQ-ENG-003), for example a headless
    `troupe engine` or another TUI, the TUI attaches without owning it. Quitting that TUI leaves the engine running,
    and the header says "attached".
  - **Engine died:** the header shows "Engine offline" and `r` restarts it. There's no automatic supervision
    (REQ-ENG-042 is deferred).
  - `s` (kill switch, REQ-SAFE-010) is different from quit: it stops all runs and puts the engine in `stopped`
    while the TUI stays open, and Resume brings it back.
  - `troupe --project <path>` targets another project. Outside any project, it prints how to `troupe init` and
    exits 1.
  - Test:
    - starting spawns the child engine and its socket;
    - `q` with no runs stops the engine;
    - `q` with a run in flight asks first, then leaves the run `interrupted` with its mail re-queued;
    - SIGHUP stops everything within 5 s with no orphans;
    - Ctrl-C, with the chat input focused and a run in flight, stops everything within 5 s with no prompt, exit
      code 0, lock released, pid file gone and no orphans; SIGINT from `kill -INT` does the same;
    - restart after that Ctrl-C (and again after `kill -9` of the TUI): the interrupted task is re-dispatched
      to the same assignee in the same worktree with its uncommitted file intact. Board, chat, memory and
      pending cards match their state before the stop, and no safety-baseline card appears;
    - a second TUI attaches, and its `q` doesn't stop the engine.
- **REQ-TUI-002 [x]** Data only through the API, including the TUI's own child engine: hello, snapshot, then
  subscribe (REQ-API-010/020/060).
  - [x] (#108; QA: the TUI died at launch on a realistically sized project.)
    - **Size:** the client accepts any response line up to the server's `MAX_LINE`, imported from api.py so
      the two can't drift.
    - **Isolation:** a pane whose load fails shows "couldn't load: <reason> (r to retry)" inline, and the rest
      of the TUI keeps running. In the chat pane the error goes on its status line, and `r` isn't a retry key
      there because it's typed text.
    - **Exit code:** an unhandled exception exits non-zero.
    - Test: a >64 KiB response loads through the real socket; a failing pane renders inline and the app
      survives; a crash exits non-zero. Live (QA): a seeded project at least the size of the human's (≥300
      tasks, ≥1,000 messages, ≥300 memories, ≥8,000 events) stays up 5 minutes with every pane loaded, with
      scrolling, pane switches and a resize along the way.
  - No direct DB reads and no polling. Read-only local files are the exception, as in REQ-API-022: docs, and
    `git diff` for an approval card's "Open full diff" (design/tui.md).
  - Live updates appear within 1 s of a new message or task change.
  - An engine restart (`r`) reconnects without clearing the screen (as REQ-MAC-012).
  - Methods the API reports as `unavailable` (REQ-API-006) are hidden or disabled, never errors.
  - Test: against a fixture API server, a pushed event updates the pane within 1 s.
- **REQ-TUI-003 [x]** One new dependency, `textual`: the standard for rich, tmux-safe Python TUIs, and the human asked
  for a TUI. No other additions without a reason in the task summary.

## Layout
- **REQ-TUI-010 [~]** Panes, dense and calm (Header/Team/Tasks done, #66; Needs you done, #67; Comms done, #69; Chat is #68):
  - **Header:** project, engine state (REQ-GUI-029 states, including Stopped), active milestone progress
    (REQ-ENG-045), and provider usage meters (REQ-BE-011/014).
  - **Team:** one line per agent: handle, state (working / idle / waiting-on-X from `waiting_on`, REQ-ENG-046),
    current activity, and a mail count.
  - **Tasks:** in-flight tasks grouped by stage, ★ human requests first (REQ-ENG-048).
  - **Comms:** agent→agent mail subjects, decisions and merges, newest at the bottom, sticky to the bottom
    (REQ-GUI-017 rules).
  - **Needs you:** open cards, including escalations the PM forwarded, credited "via pm_1 from …".
    [~] (#109 shipped the safety part) Must-deliver cards are pinned above every other card, whatever their age. Rules are in
    design/system.md "Pinning & priority order":
    - Pinned kinds: `concern`, then `safety` approvals, then `crash_loop` and `crash` (REQ-ENG-057, once #91
      lands).
    - Safety cards go oldest-first, since they gate merges. The other pinned groups go newest-first.
    - Each pinned card has a red edge and the word "Pinned" in its title, so color doesn't carry the meaning.
    - Everything else stays newest-first below the pinned group.
    - #109 ships the safety part. The rest applies as those card kinds reach the panel (#97, #78, #91).
    - Test: 6 questions plus 1 safety card, and the safety card is first; two safety cards come oldest-first.
  - **Chat with the PM:** an input line plus the last few messages, with a streaming "PM is working…" line
    while the PM runs.
- **REQ-TUI-011 [~]** (resize state preservation pending #104/#111) Sizes and terminals:
  - Usable at **80×24**: panes collapse to tabs. It scales to full screen.
  - [ ] (#104, #111) Resizing across the collapse threshold, either way and repeatedly, loses nothing. Every
    pane keeps its contents: chat history including the PM greeting, Needs-you cards, the feed, tasks. The
    chat composer keeps its unsent text, and focus stays in the same pane. Test: fill every pane, type a draft,
    resize full → 80×24 → full, and each pane's content and the draft are unchanged.
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

- **REQ-TUI-014 [ ]** (#78) Concerns and Kill.
  - A red "⚑ N" concern badge in the header, and a Concerns pane listing the whistleblower board (REQ-COM-029), with
    the actions Raise / Suppress / Kill / Reply. The content is shown only here, never in panes an agent's text flows
    through.
  - Kill asks for confirmation. Once confirmed, the whole screen turns red with "KILLED" (REQ-SAFE-011) until the
    human resumes, which also asks for confirmation. The PM chat is disabled while killed.
  - Test: the badge appears on a new concern; each action's effect; the red KILLED screenshot; resume asks first.

## Keys
- **REQ-TUI-020 [x]** Key bindings (shown in a footer) (Tab/`q`/`s`/`r` done, #66; `a` answering done, #67; `s`'s STOPPED/Resume detail and Comms' `f`/`d`/`[`/`]` filters done, #69; `/` done, #83):

  | key | action |
  |---|---|
  | Tab / Shift-Tab | cycle panes |
  | `/` | focus the PM chat |
  | Enter | send |
  | `a`, then 1–9 or free text + Enter | answer the focused Needs-you card (REQ-COM-025 rules: never while typing in chat) |
  | `s` | Stop everything (REQ-SAFE-010), after a y/N confirmation |
  | `q` | quit: stops this project's engine and runs if the TUI owns it, after confirmation when runs are in flight (TUI-001) |
  | `r` | restart the engine when it's offline |
- **REQ-TUI-021 [x]** Chat with the PM works end to end: send, the streaming working state, and the reply shown
  (REQ-COM-020). The chat is **PM-only**: the human interacts only with the PM (REQ-COM-029), and there's no way to
  chat with other agents.
  - Done (#68, `src/troupe/tui/panes/chat.py`): send (Enter, Shift+Enter for a newline where the terminal
    reports it distinctly), a "PM is working…" line while the PM is running and the human sent the last
    message, rendered markdown, sticky-to-bottom (REQ-GUI-017 semantics), a bordered "CHAT — pm_1" pane. Native
    terminal selection isn't blocked by any custom mouse handling (REQ-TUI-012's other half — OSC 52 `y` — is
    global and not part of this slice).
  - [ ] (#116) Sticky-to-bottom never overrides the human. If they scroll up while a message or a history replay
    is still settling, the view stays where they put it, and re-snapping resumes only once they're back at the
    bottom. Test: a live message arrives and the human scrolls up 50 ms later; one second on, the view hasn't
    moved.

## Verification
- **REQ-TUI-030 [x]** Snapshot mode: `TROUPE_SHOT=/path.svg troupe tui` renders once data has loaded (via Textual's
  `save_screenshot`), writes the SVG and exits 0. `TROUPE_API_FIXTURE` works as in REQ-MAC-051. UI tasks attach
  SVGs at 80×24 and full screen.
- **REQ-TUI-031 [x]** `tests/test_tui.py` uses Textual's pilot against a fixture API server. It covers:
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
- 2026-09-24 — TUI-001 rewritten: the TUI owns its engine (quit and SIGHUP stop everything, confirm if runs are in flight,
  resume on next start, attach without owning if already running). Slices mapped to #66–#69.
- 2026-09-24 — TUI-021 implemented (#68): PM chat pane, coded against the shared `Pane(client)` /
  `load()` / `on_troupe_event()` interface with a local fixture client ahead of #66's `tui/client.py` landing.
- 2026-09-24 — TUI-021: chat is PM-only (human).
- 2026-09-24 — TUI-014 Concerns pane and red KILLED state (#78).
- 2026-09-24 — TUI-001 → [~]: bare `troupe` is the primary entrypoint; Ctrl-C (key or SIGINT, any focus) is a no-prompt
  clean stop that clears lock/pid with no orphans; rerun resumes interrupted tasks in place, with no baseline
  re-approval and a kill that stays in force (human 13:31 via pm msg #887).
- 2026-09-24 — TUI-002 (#108): responses up to the server's MAX_LINE, per-pane load errors inline, a crash exits
  non-zero. TUI-010 (#109): safety approval cards pinned first in Needs you.
- 2026-09-24 — TUI-010 pinning aligned with design/system.md (concern > safety > crash_loop/crash; safety oldest-first;
  "Pinned" label). TUI-011 → [~]: resizing across 80×24 preserves every pane and the chat draft (#104, #111).
- 2026-09-24 — TUI-002's #108 item shipped [x]. TUI-021: sticky-to-bottom never undoes a human scroll-up (#116).
- 2026-09-24 — TUI-001: Ctrl-C and rerun-resumes shipped (#100, #113); the 5 s bound at real size stays open (#119). TUI-010:
  #109 shipped the safety-card pinning.
