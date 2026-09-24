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
- **REQ-GUI-020 [ ]** Drag-and-drop cards between board columns.
- **REQ-GUI-021 [ ]** Settings view: edit agents (backend, model, enabled, cadence) and budget; writes troupe.toml.
- **REQ-GUI-022 [ ]** Prompt inspector in the Agent view (system + wake prompt of the selected run).
- **REQ-GUI-023 [ ]** Text selection + copy in chat, docs and transcripts.
- **REQ-GUI-024 [ ]** Search (⌘K): jump to any task, message, memory, doc or agent.
- **REQ-GUI-025 [ ]** Usage view: cost/tokens per agent over time (charts).
- **REQ-GUI-026 [ ]** Docs: show what changed in each spec recently (git diff), and who changed it.
- **REQ-GUI-027 [ ]** App icon + window title with needs-you count; dock badge.

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
