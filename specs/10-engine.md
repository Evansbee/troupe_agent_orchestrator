# Engine — orchestration, tasks, git

Status legend: **[x]** implemented · **[ ]** not yet · **[~]** partial.
Code: `src/troupe/engine.py`, `store.py`, `gitops.py`, `config.py`, `roles.py`.

## Process model
- **REQ-ENG-001 [x]** `troupe up` in a project dir starts the engine (background thread) + GUI in one
  process. `troupe engine` runs headless; `troupe gui` attaches a GUI to a running engine.
- **REQ-ENG-002 [x]** All shared state lives in `.troupe/troupe.db` (SQLite, WAL). Engine, GUI, and every
  agent's MCP server are separate readers/writers of it. GUI→engine control goes through the `commands` table.
- **REQ-ENG-003 [x]** Only one engine per project (`.troupe/engine.pid`); a second `troupe up` attaches.
- **REQ-ENG-004 [x]** On start the engine recovers: runs left `running` become `interrupted`, agents go idle.
- **REQ-ENG-005 [x]** Engine heartbeat (`kv.heartbeat`) every tick; GUI shows "Engine offline" when stale >5s.

## Wake-ups (who runs, when, why)
Each agent run is one session of a backend CLI. Agents never loop; they are woken with a reason.
- **REQ-ENG-010 [x]** Reasons, in priority order: `chat` (human messaged them) > `poke` (human clicked Wake)
  > `review` (QA: a task is in review) > `messages` (unread mail, 2s debounce) > `task` (owns an actionable
  task) > `proactive` (role cadence elapsed AND something significant changed since they last looked).
- **REQ-ENG-011 [x]** Pending human chat pre-empts every other wake reason for that agent; any run that
  consumes a human chat message replies to the human with its final text.
- **REQ-ENG-012 [x]** Concurrency: at most `budget.max_concurrent` autonomous runs; chat gets up to 2 extra slots.
- **REQ-ENG-013 [x]** Budget: `max_runs_per_hour` (autonomous runs) and `max_usd_per_day` (claude-reported
  cost). When exceeded, autonomous wakes stop and the top bar shows "Throttled" with the reason.
- **REQ-ENG-014 [x]** Pause: stops autonomous work; chat is still answered.
- **REQ-ENG-015 [x]** Failed runs back off exponentially per agent (30s → 10m) and their mail is re-queued.
- **REQ-ENG-016 [ ]** Rate-limit awareness: when a backend reports a usage/rate limit, back off that backend
  globally until its reset time and surface it in the GUI.
- **REQ-ENG-017 [ ]** Session rotation: after N runs (configurable) start a fresh backend session, seeded with a
  digest of the agent's memory, to bound context size.
- **REQ-ENG-018 [ ]** Store each run's full wake prompt and system prompt so the human can inspect exactly what
  an agent was told.
- **REQ-ENG-019 [ ]** Hot-reload `troupe.toml` (agents, models, budget) without restarting.

## Prompts
- **REQ-ENG-020 [x]** System prompt = team charter (roster, rules, tools) + role prompt (`roles.py`).
- **REQ-ENG-021 [x]** Wake prompt contains: reason, new messages, current task brief, other open tasks,
  board (lead/pm/spec/gadfly), the agent's unanswered questions, recent team decisions, private notes, team
  status, what changed since last look (proactive), and a reason-specific instruction.

## Tasks
Lifecycle: `backlog → ready → in_progress ⇄ blocked → review → approved → done` (+ `cancelled`).
- **REQ-ENG-030 [x]** Tasks created by the Lead or human start `ready`; by anyone else `backlog` (Lead triages).
- **REQ-ENG-031 [x]** Dispatch: unassigned `ready` tasks go to the least-loaded enabled agent of the task's
  role; builders hold at most one active task. `depends_on` must be done first.
- **REQ-ENG-032 [x]** Builder tasks get a git worktree `.troupe/worktrees/t<id>` on branch `troupe/t<id>-<slug>`.
- **REQ-ENG-033 [x]** `complete_task` on a code task commits the worktree and moves it to `review`; QA is woken
  inside the same worktree; `approve` → engine merges `--no-ff` into main and removes the worktree;
  `reject` → back to the builder with notes.
- **REQ-ENG-034 [x]** Merge conflicts abort the merge and send the task back with instructions to merge main.
- **REQ-ENG-035 [x]** A task session that ends without completion backs off (45s × attempts); after
  `max_task_attempts` it becomes `blocked` and the Lead is told.
- **REQ-ENG-036 [x]** Idle builders (no task) run in the main checkout but are told not to edit files there;
  only non-builder roles' changes in main are auto-committed after each run.
- **REQ-ENG-037 [ ]** Territory conflicts: warn (event + Lead mail) when two in-flight tasks have overlapping
  territories; optionally serialize them.
- **REQ-ENG-038 [ ]** Worktree setup hook (`[git] setup = "uv sync"`) run after creating a worktree; cleanup of
  orphaned worktrees and merged branches.

## Open questions
- Should QA be able to push small fixes itself, or always bounce to the builder?
- Should the human approve tasks before builders start ("human-gated" autonomy mode)?

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
