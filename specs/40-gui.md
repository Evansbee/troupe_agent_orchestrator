# GUI — the troupe desktop app

Code: `src/troupe/gui/` (`core.py` = immediate-mode toolkit on raylib, `app.py` = shell, `views.py` = tabs,
`data.py` = DB snapshots + actions, `theme.py` = colors/metrics). Fonts: Inter + JetBrains Mono (OFL).
Must be **beautiful and crazy useful**: dark "midnight" theme, role colors, smooth easing, crisp HiDPI text.

**Platform move:** the human confirmed a native SwiftUI app (`mac/`, specs/60-mac-app.md) talking to the engine over
the local API (specs/50-api.md). The REQ-GUI requirements here stay the behavioral source of truth for both clients.
raylib remains the daily GUI until the Mac app reaches parity (REQ-MAC milestones), and new GUI features are built in
the Mac app.

## Shell
- **REQ-GUI-001 [x]** Top bar: logo, project, engine pill (Live / Paused / Throttled / Engine offline), stats
  (working, runs/h vs cap, 24h cost, open tasks), Claude 5h/7d usage meters, `+ Task`, Pause/Resume (⌘P).
- **REQ-GUI-002 [x]** Left: team sidebar — avatar (animated ring + glow while working), name, backend/model,
  live activity or status, task count, last run, unread-chat badge, "parked — owes work" diagnosis.
  Click → Agent tab; right-click → Chat.
- **REQ-GUI-003 [x]** Right: "Needs you" — question/idea cards with option buttons, free-text reply,
  dismiss; empty state when clear.
- **REQ-GUI-004 [x]** Center tabs (⌘1-7): Chat, Pulse, Board, Mail, Memory, Docs, Agent.
- **REQ-GUI-005 [x]** Toasts + macOS notifications for new questions and chat replies when unfocused. (OS
  notifications move into the engine service with REQ-ENG-047, #35. The GUI keeps in-window toasts.)
- **REQ-GUI-006 [x]** Idle at 20 fps when nothing is happening; 60 fps when animating/working.
- **REQ-GUI-007 [x]** F12 saves a screenshot to `.troupe/`; `TROUPE_SHOT=path TROUPE_TAB=Board troupe gui`
  renders one frame to a PNG and exits (for agents to verify UI work visually).
- **REQ-GUI-008 [ ]** (#55) Minimum window 1120×720: all seven tabs render without overlap or clipping at zoom 1.0 and
  1.15, and nothing regresses at 1560×980. Board card height grows with wrapped titles at any width (unit test on the
  height calculation). Verified by TROUPE_SHOT at both sizes.
- **REQ-GUI-009 [ ]** (#23; human: "a bit larger") Global UI zoom.
  - ⌘+ / ⌘- / ⌘0 change it live. The default is 1.15, and ⌘0 resets to it. The range is 0.8 up to the largest step
    at which all seven tabs render without overlap or clipping at the reference window of 1560×980. The builder
    records the actual maximum here when shipping.
  - Zoom persists across restarts, and idle stays at 20 fps.
  - At 1120×720 with zoom 1.0, rendering is unchanged from before zoom existed. Small-window fixes are REQ-GUI-008.
  - The Mac app gets zoom natively (REQ-MAC).

## Views
- **REQ-GUI-010 [x]** Chat: partner list (PM, Spec, Lead first), markdown bubbles, live "is working" bubble with
  current activity, suggestions on empty threads, multi-line composer (Enter send, Shift+Enter newline).
- **REQ-GUI-017 [ ]** (#33; human: "make sure our chat always stays scrolled to the bottom") Stick to the bottom.
  - Chat and the Agent transcript follow new content while the view is at the bottom, even when content grows
    by hundreds of pixels in one frame (a streaming reply, a long message). Stickiness changes only on user scroll,
    never on content growth.
  - Scrolling up stops following. New content then shows a "↓ New messages" pill instead of jumping.
  - Clicking the pill, sending a message or switching partner returns to the bottom and sticks again.
  - Test: unit tests of the stick logic (e.g. content +500 px in one frame stays at the bottom), plus a pill screenshot.
- **REQ-GUI-011 [x]** Pulse: constellation of agents around "YOU"; messages fly as particles along curved
  edges; recent edges glow; activity feed with filters.
- **REQ-GUI-012 [x]** Board: Backlog / Ready / In progress (+blocked) / Review (+approved) / Done; cards show
  id, priority, role, title, assignee, age. Click → task modal (details, notes, actions, add note → mail).
- **REQ-GUI-013 [x]** Mail: all agent mail with per-agent filters; click to expand full markdown.
- **REQ-GUI-014 [x]** Memory: decisions / preferences / facts / ideas / notes with rationale. (Decisions move to their
  own tab with REQ-GUI-041; Memory keeps facts, notes, ideas and preferences.)
- **REQ-GUI-015 [x]** Docs: README + specs/ + design/ + docs/ rendered as markdown, live-reloading.
- **REQ-GUI-042 [ ]** Docs gains a "Skills" group (`.agents/skills/*/SKILL.md`, showing author and source task;
  REQ-COM-044), a "Research" group (`research/`, REQ-ROLE-020) and an "Architecture" group (`docs/architecture.md`,
  `docs/adr/`).
- **REQ-GUI-016 [x]** Agent: header with controls (Chat, Wake now, Stop, Enable/Disable, New session), run
  history chips, live transcript (text, tool calls, results, errors), tasks, memory, recent mail.

## Next
- **REQ-GUI-020 [ ]** Drag-and-drop cards between board columns. (#6)
  - Drag starts after the pointer moves >4 px with the button held; a shorter press is a click and opens the task
    modal as before.
  - While dragging: a translucent ghost of the card follows the cursor and the target column is highlighted.
  - Drop maps column → status: Backlog→`backlog`, Ready→`ready`, In progress→`in_progress`, Review→`review`,
    Done→`done` (or `approved` for tasks with a branch, see REQ-ENG-039). Dropping on the same column, or outside
    the columns, changes nothing. Esc cancels.
  - Each move logs an event ("human moved #6 Ready → In progress").
- **REQ-GUI-021 [ ]** Settings tab: edit the team and the budget. (#5, after #20)
  - Agents section (per agent: provider, model, level, enabled, `idle_minutes`) reads and writes
    `.troupe/team.yaml`. Budget section reads and writes `[budget]` in `.troupe/troupe.toml`.
  - Saving keeps each file valid and keeps comments on untouched lines. The engine picks up the change via
    REQ-ENG-019 at each agent's next wake, with no restart.
  - Invalid input (a negative number, unknown provider or level, removing the only lead) is rejected inline using
    the same validation as ENG-019, and nothing is written.
  - Verified by unit tests of the YAML and TOML write round-trips and a TROUPE_SHOT screenshot.
- **REQ-GUI-022 [x]** Prompt inspector in the Agent view. (#1)
  - A Transcript / Prompt toggle next to the run chips; Prompt shows the selected run's system prompt and wake
    prompt (REQ-ENG-018) as separate, collapsible, monospace sections. The choice persists when switching runs.
  - (#54) Every run is reachable: a "Load older runs" control pages past the most recent 200. Paging is cached, so
    idle fps is unchanged, and it works at the minimum and default widths. Test: with 201+ seeded runs, the oldest
    run opens in both Prompt and Transcript mode.
- **REQ-GUI-023 [x]** Copy text (phase 1, #7 + #53). A "Copy" affordance on hover for every chat bubble, mail item,
  transcript text block and tool entry, memory, question and doc code block. It copies the raw markdown/text to the
  clipboard and shows a "Copied" toast. The button stays clickable under the pointer (#53).
- **REQ-GUI-043 [ ]** (#47) Real text selection (copy phase 2) in chat, docs and transcripts.
  - Drag-select any part of a message, doc or transcript entry, and ⌘C copies exactly that text. ⌘A selects the
    focused block, and double-click selects a word.
  - The selection survives scrolling. No frame-rate regression (idle stays at 20 fps).
  - Test: unit test of glyph hit-testing on a cached layout, plus a selection screenshot. The Mac app gets this
    natively (REQ-MAC).
- **REQ-GUI-024 [ ]** Search palette (⌘K). (#11)
  - ⌘K opens an overlay with a focused input; Esc or clicking outside closes it.
  - Fuzzy matches across tasks (id + title), messages, memories, doc titles/headings, and agents, grouped by type
    with at most 5 per group; ↑/↓ move, Enter opens: task modal, Mail with the message expanded, Memory scrolled
    to the item, Doc at the heading, Agent tab.
  - Searching must not block the frame: it runs over the `gui/data.py` snapshot, not direct DB queries per keystroke.
- **REQ-GUI-025 [ ]** Usage view: cost/tokens per agent over time (charts).
- **REQ-GUI-026 [ ]** Docs: show what changed in each spec recently (git diff), and who changed it.
- **REQ-GUI-027 [ ]** App icon + window title with needs-you count; dock badge.
  - Window title is `troupe — <project>`, prefixed with `(N) ` when N questions are open; it updates within a
    second of a question arriving or being answered. (Title part: #12. Icon and dock badge: not yet tasked.)
- **REQ-GUI-028 [ ]** (#24) "While you were away": the GUI records when the human was last looking (`kv.human_last_seen`,
  updated about every 10 s while a window is focused, and on close).
  - On open or refocus after ≥10 min away, if anything notable happened since, a catch-up panel lists: merges,
    rejected tasks, failed runs, failed checks (REQ-ENG-040), newly blocked tasks, new decisions, and questions
    that arrived (still open ones first). Each group shows a count; each item is clickable and navigates to it.
  - "Got it" or Esc closes the panel and advances `human_last_seen`. Nothing notable → no panel.
  - Test: selecting catch-up items from a fixture DB for a given `last_seen` is a pure function with unit tests.
- **REQ-GUI-029 [ ]** Service states in the engine pill:
  - (#24) "Engine offline" with **Start team** (REQ-ENG-001).
  - (#28) "Reloading… 2 runs draining" during a reload (REQ-ENG-009) and "Restarting…" after a crash (REQ-ENG-042).
  - (#28) "Crashed — see engine.log" with **Start team** after a crash loop.
  - (#42) "Stopped" with **Resume** after the kill switch (REQ-SAFE-010), taking precedence over every other state.
    A **Stop everything** button (⌘⇧.) is always visible in the top bar.
- **REQ-GUI-040 [ ]** (#29; design #31) Multi-project window (human: "the gui can connect to multiple instances that
  are running, we might want you on multiple projects at a time"). One window is attached to every registered project
  (REQ-ENG-008); each project keeps its own service. Layout: `design/projects.md`.
  - The rail is hidden while only one project is registered and appears at 2+.
  - **Project rail:** every registered project with a state dot (working / idle / paused / stopped / reloading /
    offline / crashed, REQ-GUI-029, plus "missing" when its `.troupe/` is gone), the
    number of working agents, and a needs-you badge. Click or ⌘⌥1–9 switches, and the switched-to project's full UI
    appears on the next frame (from its cached snapshot). Switching shows "While you were away" (GUI-028) for that
    project if it applies.
  - **Needs you across projects:** the inbox toggles "This project / All", defaulting to All while 2+ projects are
    attached, so a background question is never hidden. In All, each card is tagged with its
    project, and answering it delivers the answer to the right agent in the right project's DB.
    - Notifications for background-project questions name the project; clicking one switches to it.
    - The window title count (GUI-027) is the total across projects.
  - **Handles:** anything that mixes projects (the All inbox, notifications, rail tooltips) uses full `role_N@project`
    handles. Views inside the current project follow REQ-COM-005.
  - **Start / add / remove:** an offline project can be started from the rail (detached service, no second window).
    **Add project…** picks a directory, runs `troupe init` if it has no `.troupe/`, and registers it. **Remove from
    rail** unregisters it without touching its files or stopping its service.
  - **Performance:** only the visible project refreshes at full rate. Background projects refresh at most every 2 s,
    for rail and inbox data only. No DB I/O in draw code. The frame rate stays at 20 fps idle with 3 projects.
  - Top-bar stats and budget stay per project. An aggregate cost across projects is out of scope for now.
  - Test: per-project snapshots and cross-project answer routing, plus a TROUPE_SHOT of the rail with 2+ projects.
- **REQ-GUI-041 [ ]** Decisions tab: the human reviews major decisions and comments on them. (#27; design
  `design/decisions.md`, #26. Human: "a tab should be for major decisions that were made with comments that the
  lead/manager can review in case I have comments.")
  - Lists team decisions newest first: title, rationale, author handle (full `role_N@project`), time, and outcome
    badge. `#<n>`, `REQ-XXX-nnn` and `specs/…` in the text become links to the task, spec requirement or doc.
  - Major decisions (REQ-COM-034) are shown by default. A filter shows all, and superseded and reverted ones are
    struck through with a link to the successor.
  - **New since you looked:** decisions, agent replies and outcomes newer than `kv.decisions_seen_at` are
    highlighted, and the tab label shows their count. The highlights stay for the visit; leaving the tab or
    clicking "Mark all reviewed" advances `decisions_seen_at`.
  - Each decision has a comment thread (REQ-COM-035) with a composer for the human. Replies and outcomes appear in
    the thread in time order.
  - Pin, edit and delete (REQ-COM-033) are available here for decisions.
  - Verified: unit tests for the unread selection and link parsing, plus a TROUPE_SHOT screenshot of
    `TROUPE_TAB=Decisions`.

## Pulse and Stage — one scene (#22 Stage, #32 Pulse layers; design: `design/stage.md`, `design/pulse.md`)
Human request: "a compelling background visualization so I can just watch you guys work". Stage is for watching,
not clicking. These requirements define *what* it shows and *when*. `design/stage.md` defines how it looks.
- **REQ-GUI-030 [ ]** Entering and leaving Stage.
  - A "Stage" button on Pulse and ⌘⇧F (ignored while a text input has focus) open Stage: the top bar, sidebar
    and Needs-you panel are hidden and the window goes borderless full-screen on its current monitor.
  - Esc (or ⌘⇧F again) returns to Pulse and restores the previous window size and position.
  - `troupe watch` opens a GUI straight into Stage, attached to the running engine. It never starts an engine: with
    none running it shows Stage with the "Engine offline" state (REQ-ENG-005). Esc from `troupe watch` goes to the
    normal GUI.
  - `TROUPE_TAB=Stage` (with `TROUPE_SHOT`) renders Stage for screenshots. `TROUPE_STAGE_DEMO=1` seeds synthetic
    comets, task cards and an open question so one screenshot shows every element.
- **REQ-GUI-031 [ ]** Agents: every enabled agent is a node around a central "YOU" node, in its role color.
  - Node states follow the legend in REQ-GUI-038 and `design/pulse.md` (which replaces stage.md's old 4-state table).
    A working node shows its current activity (e.g. tool name) as a short label.
  - Up to 16 agents render without overlapping nodes or labels (above 9, two rings; layout in `design/pulse.md`).
  - Adding, removing or disabling agents (REQ-ENG-019) adds or removes nodes with an animation, not a jump.
- **REQ-GUI-032 [ ]** Messages are comets that travel from sender to recipient, labeled with the subject (or the
  first ~40 characters of the body if there is no subject).
  - Human chat and questions travel to or from YOU. A fan-out message (`to=role` or `team`) splits into one comet
    per recipient, leaving the sender together. System mail from the engine is not a comet; it goes to the ticker.
  - Only messages created while Stage is open animate (plus the last 5 s on opening). With more than 12 comets in
    flight, further ones on the same edge merge into one comet with a count ("×4").
  - Each comet stays on screen long enough to read its label (≥1.5 s).
- **REQ-GUI-033 [ ]** Tasks are cards that move with their lifecycle (REQ-ENG-030..040):
  - `ready` tasks wait in a ready-queue tray (see `design/stage.md`) and fly from it to the assignee when they go
    `in_progress`. The card stays attached to that node, showing id + title.
  - `review` → flies to the reviewer. Merged (`done` via merge) → a burst effect and the card leaves.
  - Rejected, merge conflict, or checks failed (REQ-ENG-040) → a visibly different "bounce" back to the builder.
  - `blocked` → the card is marked blocked on its node.
  - At most 1 card per agent is attached; extra in-flight tasks show as a count.
- **REQ-GUI-034 [ ]** While ≥1 question is open, YOU glows gold and shows the count. The glow breathes slowly and
  never strobes (≤1 cycle per 2 s). It fades within 1 s of the last question being answered or dismissed.
- **REQ-GUI-035 [ ]** A slow ticker shows notable events: merges, rejections, new questions and answers, new
  decisions, throttle or rate-limit changes, engine offline. It holds the last 5, newest first.
- **REQ-GUI-036 [ ]** Quiet mode: when no agent has worked and nothing has moved for 60 s, Stage dims and the whole
  scene drifts slowly (burn-in protection for an always-on second monitor). Any new activity restores full
  brightness within 0.5 s.
- **REQ-GUI-037 [ ]** Performance: 60 fps while anything is animating or an agent is working, 20 fps when quiet
  (REQ-GUI-006). No blocking I/O in draw code: Stage reads only client snapshots (`gui/data.py`, or the API
  in the Mac app). The mapping from snapshot
  changes to comets, cards and ticker entries is a pure module, unit-tested without raylib (fan-out split, the
  12-comet cap, the reject bounce and the question glow state).
- **REQ-GUI-038 [ ]** (#32 design; build follows) Pulse information layers. Human: "pulse should show the message passing,
  who's waiting, mail backlog and some indication as to what their model is." Pulse and Stage render **one scene**:
  Pulse is the scene with its information layers on, and Stage is the same scene full-screen with the layers turned
  down to ambient (`design/pulse.md` / `design/stage.md`). All data comes from the engine (REQ-ENG-046, REQ-BE-012),
  never inferred in the client.
  - **Message passing:** comets per REQ-GUI-032, with a visible direction. Edges used in the last 5 min stay warm, so
    "who's been talking to whom" reads at a glance.
  - **Who's waiting on whom:** every `waiting_on` kind (human, review, dependency, blocked, providers, rate_limit, slot,
    parked) has a named node treatment in the `design/pulse.md` legend. `dependency` and `blocked` may share one,
    distinguished by label and tether target. `providers` (every provider unavailable) must read differently from a
    single-provider `rate_limit`. Where there's a target, a dashed tether is drawn to it: YOU for
    human, the reviewer, the dependency's assignee, or a provider badge. Each tether shows the age since `since`.
    Rate-limit and providers waits show a reset countdown, and slot waits show the queue position.
  - Idle, working and waiting must be distinguishable at a glance from across the room (design legend).
  - **Mail backlog:** per agent, `mail_queued` (waiting) and `mail_reading` (in the current run) are shown
    differently, with a number above 5.
  - **Model:** each node has a chip showing the provider (a distinct glyph/accent for claude, codex and local), the
    model and the level (1–4 pips). It's marked "fallback" when not on the first choice, and it updates live when
    team.yaml or the provider changes.
  - Verified: a TROUPE_SHOT of Pulse with `TROUPE_STAGE_DEMO=1` seeding every waiting kind, and unit tests of the
    scene model for tether targets and ages.
- **REQ-GUI-039 [ ]** Pulse **Work panel**: what the team is busy *with* and how close the goal is.
  - A milestone header with a progress bar (REQ-ENG-045) for each active milestone.
  - One row per in-flight task that is P0/P1 or in an active milestone: id, title, assignee handle, a stage track
    (building → review → checks → merged, with the current stage highlighted, plus "blocked" or "awaiting human" when
    true), time in the current stage, and the assignee's `waiting_on` if any.
  - Hovering or selecting a row highlights its assignee's node, and hovering a node highlights its rows. Clicking a
    row opens the task.
  - Stage shows a ticker variant instead: milestone progress plus the current top rows, rotating slowly.
  - Test: row selection (priority/milestone filter, stage mapping) is a pure function with unit tests.

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
- 2026-09-23 — acceptance criteria for GUI-020/021/022/023/024/027 (from backlog #1,#5,#6,#7,#11,#12).
- 2026-09-23 — GUI-021: agents are edited in team.yaml, budget in troupe.toml.
- 2026-09-23 — Stage REQ-GUI-030..037 (human request via pm; #22, design #21).
- 2026-09-23 — GUI-028 "While you were away", GUI-029 offline/restart states, GUI-040 project switcher (service model).
- 2026-09-23 — GUI-041 Decisions tab (human request via pm; #27, design #26). Decisions leave the Memory tab.
- 2026-09-23 — GUI-040 rewritten as the multi-project window (#29), replacing the single-project switcher. GUI-029 gains
  reload/crash states (#28). Stage: ready-queue tray origin and a 16-agent layout bound (from design/stage.md).
- 2026-09-23 — platform move to the Mac app noted (REQs stay behavioral). GUI-038 Pulse information layers and GUI-039
  Work panel (#32, human). GUI-042 Docs groups for skills, research and architecture. GUI-040 adopts design/projects.md
  (rail hidden with one project, All inbox by default). Restored the missing Changelog heading.
- 2026-09-23 — GUI-031/038 point to design/pulse.md's legend (#32 design done). dependency/blocked may share a
  treatment, and `providers` must be distinct from `rate_limit`.
- 2026-09-24 — new GUI-008 minimum window (#55), GUI-009 zoom (#23), GUI-017 stick-to-bottom (#33), GUI-043 text
  selection (#47). GUI-022 gains older-run paging (#54). GUI-023 shipped (#7/#53). GUI-005 OS notifications move to ENG-047.
