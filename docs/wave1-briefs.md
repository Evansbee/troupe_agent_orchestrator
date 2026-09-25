# Wave 1 briefs: the shape (resident seats, one vocabulary, CLI first)

Draft by the lead, 2026-09-24 21:56, from docs/openrig-gap.md §4 and the human's answer to card #47
("look like OpenRig first and foremost"). spec_1 adds REQ ids and tightens acceptance. The PM
reviews before any dispatch. Every brief follows the same rules: the brief is the unit, the scope
is the whole spec section by default, the territory is exclusive, and the builder navigates the
build on their own.

**Wave 1 exit criterion:** the human runs a real task end to end from the TUI and `troupe send`,
sees every agent's honest state, and the day's run count (seat turns included) drops by half or
better.

## Order and parallelism

```
W1-A state oracle ───────────┬─> W1-B CLI (ps/parked/send/transcript) ─┐
W1-G GUI archive (after #127) ┘                                         ├─> W1-E attach + seat CLI
W1-C Claude seats (protected) ─> W1-D Codex seats (protected) ──────────┘
W1-A ─> W1-F TUI renders the taxonomy (designer copy first)
```

The first parallel set is W1-A, W1-C and W1-G, three builders on disjoint territories.
- W1-B waits for W1-A and W1-G because they share `cli.py`.
- W1-D and W1-E wait for W1-C.
- W1-F waits for W1-A.

Protected paths touched: W1-C and W1-D touch `runners.py` and `sandbox/`, and W1-G touches
`gates.py`. Each gets one batched card, and the PM tells the human they are coming.

---

## W1-A: State oracle, the three-axis taxonomy derived at read time

**Goal.** One module computes every agent's honest state, and every surface renders it with the
same words. Nothing about state is stored. It is derived when read.

**Scope.**
- Session: `present · detached · exited · absent`.
- Activity: `working · idle-at-prompt · unknown`.
- Needs-input: a **count plus reasons** (open questions, blocked tasks, a permission prompt in a
  seat). It is never a status.
- Resumability: `live · resumable · context-walled`.
- Derived flags:
  - **PARKED**: idle while owing work. That means a ready or in-progress task assigned to the agent,
    or unread actionable mail, with no live turn and not HELD.
  - **HELD**: stopped on purpose, with an armed wake. Examples: pause/stop, a cap or rate-limit
    until time T, backoff until T, or dispatch frozen. The reason and the wake time are shown.
  - **DONE-UNSEEN**: the agent finished something (a task moved to review/done, or a run ended with
    output) and the human hasn't looked since.
- `unknown` is an allowed, honest value. It is used whenever evidence is missing.
- Under today's run model: a live run means session `present` and activity `working`. No run means
  session `absent`. A stored session id means resumability `resumable`.
- Seat evidence (W1-C) plugs in through a single reader function that returns None until seats
  exist.

**Acceptance.**
- A table-driven test covers every axis value and every derived flag from seeded store state:
  - an idle agent with an assigned ready task → PARKED;
  - a capped agent → HELD with its reason and until-time;
  - a merged task not yet viewed → DONE-UNSEEN;
  - no evidence → `unknown`, never a guess.
- An API endpoint returns the full state for all agents in one call, in under 50 ms on this
  project's DB.
- The "human saw it" mark for DONE-UNSEEN is set by the TUI/CLI view actions, and nothing else.

**Context.**
- docs/openrig-gap.md §1 (state)
- ~/.openrig/reference/agent-state-taxonomy (read-only reference)
- src/troupe/store.py (runs, tasks, mail, questions, kv)
- src/troupe/engine.py (pause, caps, backoff)
- src/troupe/usage.py
- specs/10-engine.md

**Territory.**
- src/troupe/state.py (new)
- src/troupe/api.py (state endpoint only)
- src/troupe/api_client.py (state call only)
- tests/test_state.py
- specs/10-engine.md (spec_1: new "Agent state" section)

## W1-B: CLI first: `troupe ps`, `parked`, `send`, `transcript`

**Goal.** Every capability is a command, and the TUI only mirrors it.

**Scope.**
- `troupe ps` prints one row per agent: handle, backend/model, the three axes, needs-input
  count/reason, flags, and the current task.
- `troupe parked` lists only PARKED and HELD agents, each with what it owes, since when, and why
  (or when it wakes).
- `troupe send <agent> <text>`: a human message that wakes the agent. Today that means mail plus a
  wake. With seats, the text is typed into the seat. It replaces `say`, which stays as an alias.
- `troupe transcript <agent> [-n N] [--follow]` renders the agent's recent turns readably and
  redacted: tool calls summarized, text in full. The source is run logs today and the seat session
  file later.
- Every command takes `--json` and `--project`.

**Security (must).** Today `troupe say` opens the store directly and writes as `human` from any
process. An agent's Bash can impersonate the human this way. That is a live hole. The fix:
- `send`/`say` refuse when `TROUPE_AGENT` is set.
- They go through the API, not the store.
- A test proves an agent-env invocation is refused and nothing is written.

The guard-side block (`troupe send|say` from agent Bash) is a separate one-line safety.py card.
Batch it with W1-G's card.

**Acceptance.**
- Golden-output tests for ps/parked on seeded states (from W1-A's fixtures).
- `send` wakes the target within one engine tick.
- `transcript` never prints anything that `safety.redact` would redact.
- The refusal test above passes.

**Context.**
- src/troupe/cli.py (cmd_say, cmd_status)
- src/troupe/runs/*.jsonl formats (runners.py emit)
- src/troupe/tui/panes/feed.py (existing transcript rendering)
- docs/openrig-gap.md §1 (`rig ps`/`parked`/`send`/`transcript`)

**Territory.**
- src/troupe/cli.py (new subcommands, say→send)
- src/troupe/transcript.py (new)
- tests/test_cli_rig.py
- specs/50-api.md or a new CLI section (spec_1)

**Depends on:** W1-A, W1-G.

## W1-C: Resident seats for Claude (PROTECTED: one card)

**Goal.** An agent is a long-lived interactive `claude` session in a tmux seat. The engine
delivers work by typing a short line into the seat, not by rebuilding a ~15k brief per wake. This
is the main token cut.

**Scope.**
- Seat setup:
  - One tmux server on a dedicated socket (`tmux -L troupe-<project>`).
  - One session per project, one window per seated agent.
  - Opt-in per agent: `seat: true` in team.yaml. The default stays off until one real task is
    proven.
- Seat start:
  - Uses the same command builder as the run path: MCP config, permission args, PreToolUse safety
    hook, writable roots, `child_env`, and cwd (the worktree, or the root for non-builders).
  - The charter/system prompt is loaded once at seat start.
- Delivery:
  - Wake reasons become one short line typed into the seat, e.g. "New mail (2). Task #N is yours."
  - It is sent as a bracketed paste plus Enter, and only when the seat is idle-at-prompt.
  - If the seat is busy, the line queues. Nothing is typed mid-turn.
- Observation:
  - Claude lifecycle hooks (SessionStart, UserPromptSubmit, Stop, Notification) run a troupe hook
    entry point. It records seat evidence (working / idle / needs-input with reason) for W1-A.
  - A dead seat is reported as `exited`, and the engine restarts it with the restore line.
- Context wall: when the session hits its context limit (it reports context-walled), the engine
  restarts the seat fresh with the charter plus the current task pointer. A restore brief is Wave 2.
- Spend and caps:
  - Turns and usage are parsed from the Claude session file into the runs/spend tables, so
    `troupe spend` and the claude_cap_5h_percent pause stay honest.
  - A capped seat is HELD: no deliveries until reset.
- Lifecycle: seats live and die with the engine, per REQ-TUI-001 (quitting the TUI stops the
  engine, which stops its seats; tmux detach keeps everything running). Open question for the PM
  below.

**Security (must, all tested).**
- Safety parity: in a seat, the PreToolUse hook blocks exactly what it blocks today (an unapproved
  push, troupe.db/api.sock, remote changes). There is no skip-permissions flag. Writable roots are
  identical.
- **No cross-seat injection:** an agent must not be able to drive another seat, or answer its own
  permission prompt, via `tmux send-keys`/`attach`/`-L troupe-*`. Add a guard rule denying agent
  Bash commands that invoke tmux or name the troupe tmux socket, with a test. (safety.py, same card.)
- The seat environment carries `TROUPE_AGENT`, as runs do.
- A human attaching to a seat is the only interactive input path.

**Acceptance.**
- With one builder seated, a real small task goes dispatch → seat → complete_task → gate → merge,
  with no one-shot run spawned for that agent. Logs show the delivered lines.
- Kill the seat's claude process: it becomes `exited` within one tick, is restarted, and the task
  continues.
- Spend shows the seat's turns and tokens.
- The injection and parity tests above pass.
- The number of per-wake prompt tokens delivered to a seated agent is under 1k.

**Context.**
- src/troupe/runners.py (ClaudeRunner._run_once: the command, MCP config and permission args to
  reuse)
- src/troupe/sandbox/claude.py
- src/troupe/safety.py (hook_main)
- src/troupe/engine.py (dispatch/wake)
- src/troupe/spend.py
- src/troupe/usage.py
- specs/05-safety.md (REQ-SAFE-050)
- specs/30-backends.md
- docs/openrig-gap.md §1

**Territory.**
- src/troupe/seats.py (new: tmux control, delivery queue, hook ingestion)
- src/troupe/runners.py (extract a shared claude command builder; PROTECTED)
- src/troupe/sandbox/claude.py (PROTECTED, only if needed)
- src/troupe/safety.py (tmux guard rule; PROTECTED)
- src/troupe/engine.py (seat vs run dispatch branch)
- src/troupe/config.py (the `seat` flag)
- src/troupe/store.py (additive seat-evidence table)
- tests/test_seats.py
- specs/30-backends.md (spec_1)

## W1-D: Resident seats for Codex (PROTECTED: one card)

**Goal.** Same as W1-C, for interactive `codex`.

**Scope.**
- The command matches CodexRunner's: sandbox args, hooks_toml, the MCP approve config, and
  CODEX_HOME. `.git` is never writable, and complete_task keeps committing from trusted code.
- Observation uses Codex hooks/notify plus the session files under CODEX_HOME.
- Spend comes from token_count events.

**Acceptance.**
- W1-C's acceptance, run on a Codex-first builder.
- The Codex sandbox still denies writes outside the roots and under `.git`, verified in the seat.

**Context.**
- src/troupe/runners.py (CodexRunner)
- src/troupe/sandbox/codex.py
- src/troupe/usage.py (codex usage)
- W1-C's seats.py

**Territory.**
- src/troupe/seats.py (codex adapter)
- src/troupe/runners.py (codex builder; PROTECTED)
- src/troupe/sandbox/codex.py (PROTECTED)
- tests/test_seats_codex.py

**Depends on:** W1-C.

## W1-E: `troupe attach` and seat lifecycle commands

**Goal.** The human can step into any agent's seat and step back out.

**Scope.**
- `troupe attach <agent>` attaches to that agent's tmux window. When run inside tmux, it switches
  the client instead.
- `troupe seats up|down|restart [agent]`.
- The seat's status bar shows the agent handle and task.
- Attach refuses under `TROUPE_AGENT`.

**Acceptance.**
- attach, detach and restart work against a fake-backend seat in a test harness.
- The refusal test passes.
- The docs line in AGENTS.md is updated.

**Territory.**
- src/troupe/cli.py (attach/seats)
- src/troupe/seats.py (lifecycle API only)
- tests/test_attach.py

**Depends on:** W1-B (cli.py), W1-C.

## W1-F: The TUI renders the taxonomy

**Goal.** The TUI header and team pane speak W1-A's vocabulary, and nothing else.

**Scope.**
- Header: counts for working, idle, PARKED, HELD, needs-input and DONE-UNSEEN.
- Team pane rows: session/activity glyphs, needs-input count with its reason, flag badges,
  resumability, and the current task.
- Viewing an agent's finished work clears DONE-UNSEEN.
- `a` on a row attaches, once W1-E lands. Hide it until then.
- The designer puts the glyphs, colours and copy in design/tui.md first.

**Acceptance.**
- A snapshot test renders each state.
- `scripts/launch_smoke.py` passes (TUI).
- Screenshot review against design/tui.md.

**Context.**
- src/troupe/tui/panes/header.py
- src/troupe/tui/panes/team.py
- src/troupe/tui/client.py
- design/tui.md
- specs/45-tui.md

**Territory.**
- src/troupe/tui/panes/header.py
- src/troupe/tui/panes/team.py
- src/troupe/tui/client.py (state fetch)
- design/tui.md (designer)
- tests/test_tui_state.py

**Depends on:** W1-A.

## W1-G: Archive the raylib GUI

**Goal.** No GUI in the product, the CLI or the gate. The code survives only in git history.

**Scope.**
- Delete src/troupe/gui/ and its tests.
- CLI:
  - Remove `troupe gui`.
  - `troupe up` opens the TUI.
- Remove the `raylib` dependency from pyproject.
- The `--gui` path in scripts/launch_smoke.py goes. That needs #127 merged first.
- gates.py: remove `src/troupe/gui/` from LAUNCH_SMOKE_PREFIXES (PROTECTED: one card; batch it
  with W1-B's guard line).
- specs/40-gui.md and 60-mac-app.md get an "Archived 2026-09-24" header. They are not deleted.
- In AGENTS.md, the GUI shot instructions become TUI shot instructions.

**Acceptance.**
- `uv run pytest` is green.
- `uv run troupe --help` shows no gui.
- A fresh `uv sync` installs no raylib.
- launch_smoke passes as TUI-only.
- `git grep -n raylib -- src tests scripts` is empty.

**Territory.**
- src/troupe/gui/ (delete)
- src/troupe/cli.py (gui/up only)
- pyproject.toml
- uv.lock
- scripts/launch_smoke.py
- src/troupe/gates.py (PROTECTED)
- tests/test_gui_*.py
- specs/40-gui.md and specs/60-mac-app.md (header only)
- AGENTS.md

**Depends on:** #127.

---

## Open questions for the PM

1. **Daemon vs "quit = stop".** The gap doc says the engine becomes a daemon. The human's standing
   lifecycle decision is that the TUI owns the engine: quit = stop, and tmux detach keeps it
   running (REQ-TUI-001, vision 2026-09-24). The briefs keep the human's decision, so seats die with
   the engine. If OpenRig-style "the daemon outlives the TUI" is wanted, that is a human call.
2. **Seat rollout.** Recommendation: seat one builder first, prove one real task, then seat
   everyone in one team.yaml change. The alternative is flipping all agents at W1-D's merge.

## Proposed wave tags for the rest of the board

These get applied at tomorrow's cancel pass with the PM.
- **Finishing as-is:** #127, #128, #118, #75.
- **Folded into Wave 1 (cancel as separate tasks):**
  - #88: the screenshot leak; W1-G/launch_smoke.
  - #101: cmd_gui; W1-G.
  - #93: dispatch without a worktree; the W1-C dispatch branch.
- **Cancel (raylib/Swift GUI archived):** #5, #6, #11, #22, #29, #47, #49, #51, #55, #59, #60,
  #64, #70.
- **Cancel (anti-pattern under the plan):** #4 (team chat room = broadcast).
- **Wave 2, spec and recovery:**
  - #10: session rotation → continuity.
  - #28: supervision.
  - #91: crash watcher → crash-cart.
  - #36, #39, #41: agent specs, skills and roles.
  - #40: shared memory.
  - #73: the troupe/.troupe boundary.
  - #80: git posture.
- **Wave 3, proportionality and posture:**
  - #97: posture replaces the card-per-kind.
  - #45: human requests.
  - #38, #95, #110: provider caps and fallback.
  - #94: pacing, possibly moot with seats.
  - #78: whistleblower board.
  - #89: must-deliver.
  - #87: new-project defaults.
  - #105: reset clock times.
- **Safety, keep regardless of wave:** #84 (real OS boundary; seats raise its stakes).
- **Maintenance, take when a builder is idle between Wave 1 slices:**
  - #82: deflake.
  - #98: the reload loop.
  - #99: claude_command override.
  - #106: projects.json pollution.
  - #116: chat scroll.
  - #119: Ctrl-C latency.
  - #123: gate process group.
  - #125: nohup SIGHUP.
  - #126: needs-you order.
