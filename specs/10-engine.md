# Engine — orchestration, tasks, git

Status legend: **[x]** implemented · **[ ]** not yet · **[~]** partial.
Code: `src/troupe/engine.py`, `store.py`, `gitops.py`, `config.py`, `roles.py`.

## Process model
- **REQ-ENG-001 [~]** The engine runs as a background **service** per project, and the GUI is a window that
  attaches to it. (#24) (Human: "run you as a service then have the UI be able to break in and see what's going on".)
  Today `troupe up` runs the engine in the GUI process and stops it on close; the new behavior is:
  - `troupe up` starts a detached service (`troupe engine` in its own session, surviving the terminal) if none is
    running for this project, waits until its heartbeat appears (≤10 s, else prints the tail of `engine.log`),
    then opens the GUI.
  - **Closing the GUI never stops the team.**
  - `troupe engine` still runs the engine in the foreground (Ctrl-C stops it). `troupe gui` opens a window only.
    With no service running, it shows "Engine offline" with a **Start team** button.
- **REQ-ENG-002 [x]** All shared state lives in `.troupe/troupe.db` (SQLite, WAL). Engine, GUI, and every
  agent's MCP server are separate readers/writers of it. GUI→engine control goes through the `commands` table.
- **REQ-ENG-003 [~]** Only one engine per project; a second `troupe up` attaches to it. (#24)
  - Exclusivity uses a lock on `.troupe/engine.lock` held for the service's lifetime, so two simultaneous `troupe up`
    still yield exactly one engine. `.troupe/engine.pid` records the pid, start time and troupe version.
  - Stale detection: a pid file whose process is dead (or isn't a troupe engine) is removed and a new service may
    start. Test: a stale pid file doesn't block `troupe up`; a concurrent start yields one engine.
- **REQ-ENG-004 [x]** On start the engine recovers: runs left `running` become `interrupted`, agents go idle.
- **REQ-ENG-005 [x]** Engine heartbeat (`kv.heartbeat`) every tick; GUI shows "Engine offline" when stale >5s.
- **REQ-ENG-006 [ ]** Service control from the CLI. (#24)
  - `troupe stop` stops this project's service. No new runs start, and running agent runs are stopped (process group)
    and marked `interrupted` with their mail re-queued (the ENG-004 recovery path). It returns once the process has
    exited, and force-kills after 15 s. Stopping when nothing is running prints "not running" and exits 0.
  - `troupe start` starts the detached service only, with no GUI (used by the Mac app's Start team and by scripts).
    `troupe restart` = stop + start.
  - `troupe status` first prints the service state (`running` with pid, uptime, heartbeat age and version,
    `stopped`, or `stale`) and exits 0 if running, 1 otherwise.
  - `troupe reload` = graceful reload (REQ-ENG-009). `troupe status` shows the running service's version; a
    newer installed version is picked up automatically by ENG-009 (this replaces the earlier mismatch warning).
  - The GUI has a **Stop team** action (with confirmation) that goes through the `commands` table.
- **REQ-ENG-007 [ ]** (#24) The service logs to `.troupe/engine.log` (start/stop, errors, launches, merges, config reloads),
  rotated at 10 MB and keeping 3 files. Log lines identify agents by handle (REQ-COM-005).
- **REQ-ENG-008 [ ]** (#24) Project registry: `~/.troupe/projects.json` lists `{name, path, last_opened}`. It is written
  by `troupe init` and `troupe up`. `troupe projects` lists them with each one's service state. Entries whose
  `.troupe/` is gone are shown as missing, never auto-deleted. (The GUI project switcher is REQ-GUI-040.)
- **REQ-ENG-009 [ ]** (#28) Graceful reload, i.e. "auto hup" (human: "make this a service that auto hups").
  - Triggered by `troupe reload`, SIGHUP to the service, or automatically when the installed troupe changes: the
    service checks about every 30 s for a new version or changed package files (e.g. after
    `uv tool install --reinstall`).
  - Drain: no new runs start (chat included; it stays queued). Runs in flight, and a merge-gate check in progress
    (ENG-040), finish normally, up to `[service] drain_timeout` (default 600 s). After the timeout the remaining runs
    are stopped and marked `interrupted` with their mail re-queued (ENG-004).
  - Then config is re-read and the engine re-execs on the currently installed code. The lock, pid file and DB carry
    over, and the heartbeat resumes within 10 s of the drain ending. GUIs stay attached and reconnect by themselves.
  - An event and a log line record it ("Engine reloaded: v0.1.0 → v0.2.0", or "config reload" if the version is
    unchanged). A reload request during a reload is ignored.
  - Test: while draining nothing launches, in-flight runs complete, the timeout interrupts, and the version-change
    detector fires once per change.
- **REQ-ENG-042 [ ]** (#28) Crash supervision.
  - The service is a small supervisor process that holds the lock and runs the engine as a child. If the engine
    exits unexpectedly (non-zero, or killed, including `kill -9`), the supervisor restarts it with backoff
    (1 s, 2 s, 4 s … max 60 s), and recovery (ENG-004) marks its runs interrupted.
  - Crash loop: more than 5 restarts in 10 min → stop restarting and record the reason in kv and `engine.log`.
    `troupe status` shows `crashed` and the GUI shows it (REQ-GUI-029). `troupe up` or Start team clears it.
  - `troupe stop` and reload are not crashes. If the supervisor itself dies, the engine exits within 5 s, so no
    unlocked orphan engine can run beside a new one.
  - No launchd/login items are installed: auto-start at login stays out of scope. Test: backoff sequence and the
    crash-loop limit.
- Out of scope: auto-start at login (launchd) and one machine-wide service for all projects (human chose one service
  per project, with one GUI attached to all of them, REQ-GUI-040).

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
- **REQ-ENG-016 [x]** Rate-limit awareness: when a backend reports a usage/rate limit, back off that backend
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
- **REQ-ENG-018 [x]** Store each run's full wake prompt and system prompt so the human can inspect exactly what
  an agent was told. (#1)
  - `runs` gains `prompt` and `system` TEXT columns (additive migration); both are written when the run launches,
    before the backend starts, so they exist even for runs that crash.
  - Runs from before the migration show "(not recorded)" rather than erroring.
  - Test: migration on an old-schema DB; a launched run persists both fields.
- **REQ-ENG-019 [x]** The team is defined in `.troupe/team.yaml` and both config files hot-reload. (#20; human,
  live chat: "the agent setup should be a yaml file, which provider, which level")
  - `team.yaml` holds `agents:`, a list of `{id, role, name, providers, enabled, idle_minutes, extra_args}`, plus the
    top-level `provider_limits` (REQ-BE-011).
    - `providers` is an **ordered** preference list of `{provider, model, level}`, first = preferred (human request).
      Fallback between entries is REQ-BE-012.
    - `provider` is `claude | codex | local`. `level` is `low | medium | high | max`, or empty for the provider
      default (mapping in REQ-BE-010).
    - Shorthand: `provider`/`model`/`level` at the agent's top level means a one-entry list.
    - Only `id`, `role` and a provider are required. The config exposes `cfg.agent(id).providers` as the list.
  - `troupe.toml` keeps `[project]`, `[budget]`, `[backends]`, `[git]`, `[safety]` and `[research]`.
  - Validation (on load and on reload): ids are unique, exactly one `lead`, roles and providers are known, `level`
    is valid, and numbers are non-negative. A `providers` list is non-empty with no duplicate `provider`+`model`
    entries. The error message names the file, the agent id and the field.
  - Migration: if `team.yaml` is missing and `troupe.toml` has `[[agents]]`, write `team.yaml` from them once
    (`backend` → `provider`, `effort` → `level`) and log an event. `troupe.toml` is not modified. When both exist,
    `team.yaml` wins and a leftover `[[agents]]` gets a one-time "ignored" event. `troupe init` writes `team.yaml`.
  - Hot reload: the engine checks both files' mtimes each tick and re-reads a file when it changes. An invalid file
    is ignored and the last good config kept. An error event is logged and the GUI top bar shows a red
    "team.yaml invalid" (or troupe.toml) pill with the message until a valid version is saved.
  - Changes apply at each agent's next wake; a run in flight is never interrupted. Changing `provider` clears the
    agent's session (sessions can't cross providers); changing `model` or `level` keeps it.
  - Adding an agent makes it appear in the team without restart. Disabling one stops new wakes. Removing one (or
    changing its role) returns its open tasks to `ready` and unassigned.
  - Comments in `team.yaml` survive a GUI save (REQ-GUI-021), so the YAML library must round-trip comments.
  - Test: parse and validate, migration from `[[agents]]`, reload applies a model change to the next launch, and an
    invalid file keeps the old config.
  - Ids: new agents use `<role>_<N>` ids (the handle's local part, REQ-COM-005). If `id` is omitted it is derived
    from role + the next free N. Legacy ids (`builder-1`, `spec`) stay valid and are never rewritten in the DB.
- **REQ-ENG-041 [~]** (#20 ships the mechanism and today's roles; each row notes its task) Default roster, written by `troupe init` and
  adopted for this project once #20 lands. Human: trust codex; reviewers run on a different provider from writers.
  Existing roles and the roster mechanism shipped in #20; architect/researcher entries await #36/#41.
  Each cell is a preference list, first = preferred:
  | id | providers (provider · model · level) |
  |---|---|
  | lead_1 | claude · opus · high → codex · default · high |
  | pm_1 | codex · default · high → claude · opus · high |
  | spec_1 | codex · default · high → claude · opus · high |
  | designer_1 | claude · sonnet · medium → codex · default · medium |
  | builder_1, builder_2 | codex · default · high → claude · sonnet · high → local · detected |
  | architect_1 (#36) | claude · opus · high → codex · default · high |
  | qa_1 | claude · opus · high → codex · default · high |
  | researcher_1 (#41) | local · detected → codex · default · medium |
  | gadfly_1 | local · detected → codex · default · medium |
  - "detected" = the first model the local server reports during `troupe init`. If no local server answers, local
    entries are written commented out, with a note saying how to enable them.
  - Default `provider_limits`: claude `five_hour: 50, seven_day: 50` (the human's example); codex and local uncapped.
  - `troupe init` writes the architect and researcher once their roles exist (#36, #41), and either can be disabled
    in team.yaml.
  - Test: the generated team.yaml validates and matches the table.

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
- **REQ-ENG-038 [x]** Worktree setup and cleanup. (#3)
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
- **REQ-ENG-040 [x]** Merge gate: a configured check must pass before a task merges into main. (#15, human
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
- **REQ-ENG-043 [ ]** (#36) Architecture review for **risky changes only** (human's answer). A task needs the architect's
  approval in addition to QA's when its diff (against its merge base) matches any of these:
  - **schema:** added or removed lines containing `CREATE TABLE`, `ALTER TABLE` or `CREATE INDEX`, or files under a
    `migrations/` dir or `*.sql`;
  - **dependency:** any change to a manifest (`pyproject.toml`, `requirements*.txt`, `package.json`, `Cargo.toml`,
    `go.mod`, `Package.swift`);
  - **new top-level module:** a new file directly in a source root (`src/<pkg>/`, `mac/Sources/<target>/`), or a new
    directory at that level;
  - **contract:** any path in `[review] arch_paths`. troupe's own list is `src/troupe/mcp_server.py`,
    `src/troupe/team.py`, `src/troupe/api*`, `specs/50-api.md` and the commands handling in `src/troupe/store.py`;
  - **flag:** the lead set `arch_review` on the task (`update_task(arch_review=True)`).
  Rules:
  - When such a task enters `review`, QA and the architect are both woken, and the board card shows an "arch ✓ / arch
    pending" chip. Merging needs both approvals. Either rejection sends the task back with that reviewer's notes, and
    both review again after the fix.
  - Order after both approve: human approval if protected paths are touched (REQ-SAFE-020), then the merge gate
    (REQ-ENG-040).
  - With no enabled architect, the gate is skipped and the approval notes say so.
  - The detection is a pure function of the diff and flag.
  - Test: a `store.py` schema change or a `pyproject.toml` dependency needs both approvals; a `gui/views.py`-only change
    merges on QA alone; an architect reject sends it back.
- **REQ-ENG-045 [ ]** Milestones are first-class (human: Pulse should show the major work and how close the goal is).
  - Additive schema: a `milestones` table (`id, name, goal, sort_order, status active|done, created`) and
    `tasks.milestone_id`.
  - Tools:
    - the lead (or human) creates and edits milestones with `milestone(action=create|update, name, goal, order,
      status)`;
    - `create_task` and `update_task` accept `milestone`;
    - `list_tasks(milestone=…)` filters by it.
  - Progress = done / (total − cancelled) tasks in the milestone, computed, never stored. Only the lead or human marks
    a milestone `done`.
  - The lead's and pm's wake prompts show the active milestones with progress. The API exposes milestones
    (specs/50-api.md), and Pulse shows them (REQ-GUI-038).
  - The first milestone is "Ready for a test project" (#1, #2, #3, #15, #20, #23, #24, #25, plus the lead's additions).
    The lead creates it with the tool, not a code seed.
  - Test: create, assign and filter; progress excludes cancelled tasks; non-lead edits return `ERROR:`.
- **REQ-ENG-046 [ ]** The engine publishes each agent's **wait state** and mail backlog every tick, as data (additive
  columns or kv, exposed by the API), so Pulse (REQ-GUI-038) doesn't infer it.
  - `waiting_on` = `{kind, target, since, reset_at?, queue_position?}` or null. Kinds, in precedence order:
    - `human`: the agent's open question, or its task awaiting a human approval card. Target is `human`.
    - `review`: its task is in review. Targets are the reviewer handles.
    - `dependency`: its assigned task has unmet `depends_on`. Target is the dependency task id(s).
    - `blocked`: its task is `blocked`. Target is the task id plus the reason from the task note.
    - `providers`: every provider in its list is unavailable (REQ-BE-012). `reset_at` is the earliest reset.
    - `rate_limit`: its current provider is limited (REQ-ENG-016). Target is the provider, plus `reset_at`.
    - `slot`: it has a wake candidate but no run slot is free. `queue_position` is its 1-based position in the order
      the tick would launch.
    - `parked`: idle while owing work (today's derived diagnosis, REQ-GUI-002).
  - `since` = when the current kind began. It persists across ticks while the kind doesn't change.
  - Mail: `mail_queued` = unread messages not yet delivered. `mail_reading` = messages delivered to the currently
    running run.
  - Test: one fixture per kind, precedence when several apply, and a stable `since`.

## Open questions
- Should QA be able to push small fixes itself, or always bounce to the builder?
- Should the human approve tasks before builders start ("human-gated" autonomy mode)?

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
- 2026-09-23 — acceptance criteria for ENG-016/017/018/019/037/038 (from backlog #1,#2,#3,#5,#9,#10); new ENG-039
  (drag to Done must merge, not skip it).
- 2026-09-23 — new ENG-040 merge gate (human approved idea #1 via pm; task #15, depends on #3's `[git]` section).
- 2026-09-23 — ENG-019 rewritten: agents move to `.troupe/team.yaml` (provider/model/level), hot-reloaded (#20).
- 2026-09-23 — service process model: ENG-001/003 rewritten, new ENG-006/007/008. Default roster: ENG-041 (the
  roster open question is resolved).
- 2026-09-23 — tagged: service → #24, roster → #20.
- 2026-09-23 — ENG-019: ordered `providers` list per agent (human). ENG-041: roster as preference lists, plus architect_1,
  researcher_1 and default provider_limits. New ENG-043 architect gate for risky changes (#36), ENG-045 milestones, and
  ENG-046 agent wait state + mail backlog as engine data (for Pulse, #32).
- 2026-09-23 — human chose one service per project (open question closed). New ENG-009 graceful/auto reload and
  ENG-042 crash supervision (#28). ENG-006 version warning replaced by auto reload.
