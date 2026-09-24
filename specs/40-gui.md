# GUI — the troupe desktop app

Code: `src/troupe/gui/` (`core.py` = immediate-mode toolkit on raylib, `app.py` = shell, `views.py` = tabs,
`data.py` = DB snapshots + actions, `theme.py` = colors/metrics). Fonts: Inter + JetBrains Mono (OFL).
Must be **beautiful and crazy useful**: dark "midnight" theme, role colors, smooth easing, crisp HiDPI text.

## Shell
- **REQ-GUI-001 [x]** Top bar: logo, project, engine pill (Live / Paused / Throttled / Engine offline), stats
  (working, runs/h vs cap, 24h cost, open tasks), Claude 5h/7d usage meters, `+ Task`, Pause/Resume (⌘P).
- **REQ-GUI-002 [x]** Left: team sidebar — avatar (animated ring + glow while working), name, backend/model,
  live activity or status, task count, last run, unread-chat badge, "parked — owes work" diagnosis.
  Click → Agent tab; right-click → Chat.
- **REQ-GUI-003 [x]** Right: "Needs you" — question/idea cards with option buttons, free-text reply,
  dismiss; empty state when clear.
- **REQ-GUI-004 [x]** Center tabs (⌘1-7): Chat, Pulse, Board, Mail, Memory, Docs, Agent.
- **REQ-GUI-005 [x]** Toasts + macOS notifications for new questions and chat replies when unfocused.
- **REQ-GUI-006 [x]** Idle at 20 fps when nothing is happening; 60 fps when animating/working.
- **REQ-GUI-007 [x]** F12 saves a screenshot to `.troupe/`; `TROUPE_SHOT=path TROUPE_TAB=Board troupe gui`
  renders one frame to a PNG and exits (for agents to verify UI work visually).

## Views
- **REQ-GUI-010 [x]** Chat: partner list (PM, Spec, Lead first), markdown bubbles, live "is working" bubble with
  current activity, suggestions on empty threads, multi-line composer (Enter send, Shift+Enter newline).
- **REQ-GUI-011 [x]** Pulse: constellation of agents around "YOU"; messages fly as particles along curved
  edges; recent edges glow; activity feed with filters.
- **REQ-GUI-012 [x]** Board: Backlog / Ready / In progress (+blocked) / Review (+approved) / Done; cards show
  id, priority, role, title, assignee, age. Click → task modal (details, notes, actions, add note → mail).
- **REQ-GUI-013 [x]** Mail: all agent mail with per-agent filters; click to expand full markdown.
- **REQ-GUI-014 [x]** Memory: decisions / preferences / facts / ideas / notes with rationale.
- **REQ-GUI-015 [x]** Docs: README + specs/ + design/ + docs/ rendered as markdown, live-reloading.
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
- **REQ-GUI-021 [ ]** Settings tab: edit agents (backend, model, enabled, `idle_minutes`, effort) and budget
  fields; writes `.troupe/troupe.toml`. (#5)
  - Saving keeps the file valid TOML and keeps comments on untouched lines; the engine picks it up via REQ-ENG-019.
  - Invalid input (e.g. a negative number, unknown backend) is rejected inline and nothing is written.
  - Verified by a unit test of the TOML write round-trip and a TROUPE_SHOT screenshot.
- **REQ-GUI-022 [ ]** Prompt inspector in the Agent view. (#1)
  - A Transcript / Prompt toggle next to the run chips; Prompt shows the selected run's system prompt and wake
    prompt (REQ-ENG-018) as separate, collapsible, monospace sections. The choice persists when switching runs.
- **REQ-GUI-023 [ ]** Copy text from chat, docs and transcripts. (#7)
  - Required: a "Copy" affordance on hover for every chat bubble, transcript text block, tool result and doc code
    block. It copies the raw markdown/text to the clipboard and shows a "Copied" toast.
  - Stretch (separate requirement if pursued): drag-select arbitrary text in markdown blocks.
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

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
- 2026-09-23 — acceptance criteria for GUI-020/021/022/023/024/027 (from backlog #1,#5,#6,#7,#11,#12).
