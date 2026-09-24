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
  globally until its reset time and surface it in the GUI. (#2)
  - Detection: claude `rate_limit_event` with status ≠ `allowed`, or a failed run whose error text mentions a
    rate/usage limit; codex error events mentioning a rate/usage limit.
  - Reset = the reported reset time; if none is reported, now + 15 min. Stored in kv (`limit.<backend>`) so
    it survives an engine restart.
  - While limited, **no** run of any kind (including chat) starts on that backend; agents on other backends
    are unaffected. Pending chat stays queued and the Chat view shows "Claude limited until 14:05" on the
    working bubble instead of silently waiting.
  - A rate-limited run does not count toward the per-agent failure backoff (REQ-ENG-015) and its mail is re-queued.
  - Top bar shows one pill per limited backend with its reset time (REQ-GUI-001); it disappears at reset.
  - Test: a unit test of the backoff decision (limited backend skipped, other backend dispatched, reset expiry).
- **REQ-ENG-017 [ ]** Session rotation: bound context size by starting a fresh backend session periodically. (#10)
  - Config `[budget] session_max_runs` (default 25; 0 = never rotate). When an agent's `session_runs` reaches it,
    the next run starts without `--resume` and `session_runs` resets.
  - The first prompt of a rotated session includes a "Where you left off" section: summaries of the agent's
    last 5 runs, its private notes, and its open tasks. (Manual "New session" in the GUI gets the same digest.)
  - Test: rotation happens exactly at the configured count; the digest appears only in the first prompt.
- **REQ-ENG-018 [ ]** Store each run's full wake prompt and system prompt so the human can inspect exactly what
  an agent was told. (#1)
  - `runs` gains `prompt` and `system` TEXT columns (additive migration); both are written when the run launches,
    before the backend starts, so they exist even for runs that crash.
  - Runs from before the migration show "(not recorded)" rather than erroring.
  - Test: migration on an old-schema DB; a launched run persists both fields.
- **REQ-ENG-019 [ ]** Hot-reload `troupe.toml` (agents, models, budget) without restarting. (#5)
  - The engine re-reads the file when its mtime changes (checked each tick); invalid TOML is ignored, the previous
    config kept, and an error event logged.
  - Changes apply at the next run; running runs are never interrupted. Changing an agent's **backend** clears its
    session (sessions can't cross backends); changing only the model keeps it.
  - Disabling an agent stops new wakes; adding an agent to the file makes it appear in the team without restart.

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
- **REQ-ENG-037 [ ]** Territory conflicts: warn when two in-flight tasks have overlapping territories. (#9)
  - Territory format: comma-separated paths; anything in parentheses is a note and is ignored
    (`src/troupe/gui/views.py (agent_view only)` → `src/troupe/gui/views.py`). Trailing `/` optional.
  - Overlap = one path equals or is a directory prefix of the other (`src/troupe/gui/` overlaps
    `src/troupe/gui/views.py`; `src/troupe/gui` does **not** overlap `src/troupe/gui_old.py`). Same-file tasks with
    different parenthetical scopes still count as overlapping (warn-only, false positives accepted).
  - Checked when a task enters `in_progress`, against tasks in `in_progress`, `blocked` or `review`.
  - On overlap: an event, one mail to the Lead per task pair (not repeated), and a warning pill on both board cards
    while both remain in flight. Tasks with empty territories are never flagged.
  - Serializing (holding the second task) is out of scope for now.
  - Test: parsing, prefix rules, notify-once-per-pair.
- **REQ-ENG-038 [ ]** Worktree setup and cleanup. (#3)
  - Config `[git] setup = "uv sync"` (default empty = none) runs once in each newly created worktree, before the
    builder's first run. Output is appended to that run's log; a non-zero exit adds a note to the task and the
    builder is still started (told that setup failed and why).
  - After a successful merge the task branch is deleted (`git branch -d`).
  - On engine start, worktrees under `.troupe/worktrees/` whose task is `done`/`cancelled` or missing are removed,
    followed by `git worktree prune`. Worktrees of open tasks are never touched.
  - Test: temp git repo covering setup-once, branch deletion and orphan cleanup.

- **REQ-ENG-039 [ ]** Human status overrides (board drag-and-drop, REQ-GUI-020) go through the same rules as
  the Lead's: moving a task that has a branch to **Done** sets it `approved` (the engine merges it and then marks it
  done, REQ-ENG-033); moving one to **Review** wakes QA. Moving a task out of `in_progress` while its run is live
  does not kill the run; the assignee sees the new status in its next prompt.
- **REQ-ENG-040 [ ]** Merge gate: a configured check must pass before a task merges into main. (#15, human
  approved idea #1)
  - Config `[git] check = "<cmd>"` (troupe itself uses `"uv run pytest"`) and `[git] check_timeout` (seconds,
    default 600). Empty `check` = today's behavior (merge straight after approval).
  - On `approved` (from QA or a human board move, REQ-ENG-039), in the task's worktree: merge main into the task
    branch, run the check with `cwd` = the worktree, then `merge --no-ff` into main **only if it exits 0**.
  - Tasks without a branch skip the gate. The check runs off the engine's tick: heartbeat, chat and other wakes
    keep going while it runs, and only one check runs at a time.
  - Failure (non-zero exit or timeout): the task goes back to `in_progress` with the assignee. The last ~50 lines of
    output go into `review_notes` and are mailed to the builder, an event is logged ("Checks failed on #15"), and
    the full output is saved to `.troupe/checks/t<id>.log`. The worktree is kept. The builder fixes the problem and
    calls `complete_task` again, which goes through QA review again, the same as the conflict path.
  - The board card shows a "checks failed" chip from the failure until the task next enters `review`.
  - A conflict while merging main into the branch, or while merging into main afterwards, takes the existing
    REQ-ENG-034 path.
  - Test: in a temp git repo, a passing check merges, a failing check doesn't merge and sends the task back with
    output, a timeout counts as a failure, and an empty check merges directly.

## Open questions
- Should QA be able to push small fixes itself, or always bounce to the builder?
- Should the human approve tasks before builders start ("human-gated" autonomy mode)?

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
- 2026-09-23 — acceptance criteria for ENG-016/017/018/019/037/038 (from backlog #1,#2,#3,#5,#9,#10); new ENG-039
  (drag to Done must merge, not skip it).
- 2026-09-23 — new ENG-040 merge gate (human approved idea #1 via pm; task #15, depends on #3's `[git]` section).
