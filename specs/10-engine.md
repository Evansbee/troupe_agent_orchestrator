# Engine — orchestration, tasks, git

Status legend: **[x]** implemented · **[ ]** not yet · **[~]** partial.
Code: `src/troupe/engine.py`, `store.py`, `gitops.py`, `config.py`, `roles.py`.

## Process model
> **Lifecycle change (human, 2026-09-24): "run and everything runs, quit and everything quits."** In the default flow,
> the TUI owns its project's engine (REQ-TUI-001), and tmux detach keeps a team working unattended. The detached
> background service below (ENG-001/006, GUI-028) is **superseded for the default flow**. It stays available for
> headless use (`troupe engine`, and `troupe start` for scripts). Reload and supervision (ENG-009/042, #28) are
> **deferred**.
- **REQ-ENG-001 [x]** (superseded for the default flow by REQ-TUI-001; kept for headless/scripted use) The engine runs as a background **service** per project, and the GUI is a window that
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
- **REQ-ENG-003 [x]** Only one engine per project; a second `troupe up` attaches to it. (#24)
  - Exclusivity uses a lock on `.troupe/engine.lock` held for the service's lifetime, so two simultaneous `troupe up`
    still yield exactly one engine. `.troupe/engine.pid` records the pid, start time and troupe version.
  - Stale detection: a pid file whose process is dead (or isn't a troupe engine) is removed and a new service may
    start. Test: a stale pid file doesn't block `troupe up`; a concurrent start yields one engine.
- **REQ-ENG-004 [x]** On start the engine recovers: runs left `running` become `interrupted`, agents go idle.
- **REQ-ENG-060 [ ]** (#103; found by QA: 74 orphaned test engines, 3.2 GB, 37% CPU on the human's laptop) An engine
  never outlives its project. If its project root or `.troupe/` disappears, it stops its runs, removes its pid
  file and exits within a few ticks. This check lives in the heartbeat loop.
  - Test hygiene, same task: a full `uv run pytest` session, even one killed mid-run, leaves no engine whose
    cwd is under the test temp root. A session-level guard fails the run if one survives.
  - Test: delete a running engine's project dir, and it exits and removes its pid within a few ticks. QA's check:
    run the full suite twice plus a forced mid-suite kill, then `pgrep -f "troupe.cli engine"` finds zero test
    engines (the team's real engine excepted).
- **REQ-ENG-005 [x]** Engine heartbeat (`kv.heartbeat`) every tick; GUI shows "Engine offline" when stale >5s.
- **REQ-ENG-006 [~]** Service control from the CLI. (#24) (Superseded for the default flow: the TUI's quit and SIGHUP
  stop its engine, REQ-TUI-001. These commands remain for a headless `troupe engine`/`troupe start` service.)
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
- **REQ-ENG-007 [x]** (#24) The service logs to `.troupe/engine.log` (start/stop, errors, launches, merges, config reloads),
  rotated at 10 MB and keeping 3 files. Log lines identify agents by handle (REQ-COM-005).
- **REQ-ENG-008 [x]** (#24) Project registry: `~/.troupe/projects.json` lists `{name, path, last_opened}`. It is written
  by `troupe init` and `troupe up`. `troupe projects` lists them with each one's service state. Entries whose
  `.troupe/` is gone are shown as missing, never auto-deleted. (The GUI project switcher is REQ-GUI-040.)
  - [ ] (#106; QA found 300 of the human's 308 entries pointing at deleted temp dirs) Entries whose path no
    longer exists are left out of `troupe projects`, replaced by one line: "N missing — `troupe projects
    --prune`". `--prune` first writes `projects.json.bak`, then removes only entries whose path doesn't exist.
    Entries whose path exists but whose `.troupe/` is gone still show as missing. Nothing is removed without
    `--prune`.
  - [ ] Tests and harnesses never write the human's registry: a session-wide fixture points `HOME` at a temp dir.
    Test: the real `~/.troupe/projects.json` is byte-identical before and after a full `uv run pytest`.
- **REQ-ENG-009 [ ]** **DEFERRED** (human lifecycle change 2026-09-24; #28 on hold) (#28) Graceful reload, i.e. "auto hup" (human: "make this a service that auto hups").
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
- **REQ-ENG-042 [ ]** **DEFERRED** (with #28; the TUI shows an offline engine and offers a restart, REQ-TUI-001) (#28) Crash supervision.
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

## Crash reporting (#91)
Scope decision: human via PM, 2026-09-24 (msg #814): report both client and engine crashes and auto-file P0 work.
Client reporting does not depend on the deferred service supervisor shipping.
- **REQ-ENG-056 [ ]** Every troupe process entrypoint captures an unhandled crash in a report file at the
  affected project's `.troupe/crashes/<utc>-<entrypoint>.txt`, including GUI, TUI and engine startup and runtime
  failures.
  - The report is persisted even when the engine is offline, so the human never has to copy a traceback to the
    team. Normal quit, intentional stop and reload are not crashes.
  - The failed process exits non-zero and prints the report path to stderr. The original traceback is never
    swallowed: it remains visible on stderr as well as in the report. A report-writing failure must not hide
    the original traceback or turn the crash into a successful exit.
  - Entrypoints: GUI (`troupe up`, `troupe gui`), TUI (`troupe`), CLI subcommands, the engine main loop and the
    MCP server.
  - The handler is installed before any window or UI exists, so a crash on the first frame (the #90 case) or
    before the GUI window opens still writes a report.
  - Report fields (#91): troupe version, git sha of the installed copy, entrypoint, argv, Python version, full
    traceback, and the last ~50 lines of `engine.log`.
  - Secrets and private data must not be exposed through reports or the tasks, notifications and messages
    derived from them (Principle 0).
  - Acceptance: inject an exception during startup and during normal operation of each entrypoint; each produces
    a report at the specified path, including with no engine running. Raising inside the GUI notification
    handler (the #90 case) produces a report. Verify non-zero exit, the stderr pointer and original traceback;
    repeat with an unwritable report directory and verify the traceback still appears and exit remains non-zero.
    Each report contains every field above. A normal exit produces none.
- **REQ-ENG-057 [ ]** The engine consumes pending crash reports on startup and while running (on its normal
  tick; no new process), and within one tick of a report appearing automatically files a **P0** task assigned
  to the builder role with the traceback and report path, sends one message each to the PM and lead with the
  task id, and emits a human-facing notification of kind `crash` (REQ-ENG-047).
  - Startup triage (human, 2026-09-24 12:59): on engine start, unfiled reports are filed before any agent
    dispatch. The PM's wake-up brief lists unfiled reports and open crash tasks first, ahead of other work.
  - Dedupe survives engine restarts: consuming the same report again never creates another task or repeats its
    messages/notification. Crash identity is `(entrypoint, last traceback frame)`: repeated occurrences with
    the same identity attach to the existing open crash task rather than flooding the board with duplicates;
    a different entrypoint or last frame creates its own task.
  - `crash` is must-deliver alongside #89's kinds: neither `[notify] enabled = false` nor `quiet` can silence it.
    A crashed client must not remain the notification destination merely because its focus/connection state is
    stale; delivery must reach the human through the available notification path.
  - Acceptance: a saved client report produces, within one tick, a P0 containing the traceback, one message each
    to PM and lead with the task id, and a notification; replay it across an engine restart and verify no
    duplicate delivery/task. The same `(entrypoint, last frame)` three times leaves exactly one open task; change
    only the entrypoint, then only the last frame, and verify each creates a distinct task. Disable
    notifications and list every kind in `quiet`; the crash notification still arrives. Start the engine with a
    pending report and verify it is filed before the first dispatch and appears at the top of the PM's brief.
- **REQ-ENG-058 [ ]** Engine crashes use the same report and ingestion path as client crashes. A report saved
  before engine exit is consumed when the engine next starts, including after supervisor restart (ENG-042/#28).
  - When the supervisor observes an unexpected exit that could not write its own report (for example `kill -9`),
    it records the observed failure for the same path; it does not invent an unavailable traceback or file a
    second report for a failure already captured by the engine.
  - Acceptance: crash the engine and restart it; the saved report produces the ENG-057 outcomes once. With the
    #28 supervisor enabled, exercise a killed child and verify the same reporting and dedupe behavior.
  - Engine restart policy, backoff and crash-loop limits remain ENG-042/#28, which is deferred. Reporting does
    not re-enable supervision or change TUI engine ownership (REQ-TUI-001).
- **REQ-ENG-059 [ ]** For changes under `src/troupe/gui/` or `src/troupe/tui/`, the merge gate includes headless
  GUI and TUI launch smoke tests using the candidate merged tree (REQ-ENG-040).
  - Exercise actual startup and at least one render/update cycle, then clean shutdown in a disposable project;
    an import-only check is insufficient. A startup exception, non-zero exit or timeout blocks the merge through
    the normal ENG-040 failure path. The check leaves no client or engine processes running.
  - Implemented by #92 as `scripts/launch_smoke.py` (one command for QA) plus a selectable pytest wrapper. It
    runs `troupe init` in a fresh temp project with fake/no backends.
  - After each client has initialized its notification cursor (about 10 frames), insert these through the Store:
    a `needs_help` message to the human, a PM chat message and a pending safety-baseline question. Then wait
    for the notification handler to run and render. Events present before launch don't cover this path, because
    the GUI only treats post-first-refresh messages as new (#90). Capture a GUI screenshot (non-empty PNG) and
    the TUI output as review evidence. Any traceback during startup, notification handling or shutdown fails
    the gate, and the stderr tail goes in the check log.
  - Where no window can be opened (headless CI), the pytest wrapper skips and prints the reason. In the merge
    gate, a skip is reported in the check log as "launch smoke not run: <reason>", never as a pass. QA then
    runs the script by hand before approving (QA's gui/tui review rule).
  - Hardening (QA's #92 review, 2026-09-24):
    - **The candidate tree, not the installed copy.** The script and both client subprocesses run the task
      tree's code, through the tree's own environment (`uv run` in the tree). They must not use the engine's
      installed interpreter. The check log prints the exact command it runs.
    - **Deterministic injection.** Events are inserted after a positive first-refresh signal (or re-inserted
      until each client exits), never after a fixed delay alone. With the #90 bug re-added the check fails
      10 of 10 runs; on a healthy tree it passes 10 of 10.
    - **Skip is decided up front.** Only a positive "no display" detection made before the clients launch may
      skip, for example a window-open probe or raylib/GLFW's own no-display error. A client killed by a
      signal (negative exit code, such as a segfault) or any other non-zero exit is a failure, never a skip.
      (#112) A probe that itself raises a traceback or times out is also a failure; only its positive
      "no display" result skips. On a gate timeout, the whole process group (clients and any engine they
      started) is killed.
    - **Nothing of the human's is touched.** `HOME` (and XDG dirs) point into the temp dir for `troupe init`
      and both clients, so `~/.troupe/projects.json` and the human's live project are never modified.
    - **Cleanup always runs.** Clients and any engine they start are stopped in a `finally`, even if setup or
      injection raises.
  - Acceptance: inject the #90 failure (GUI calls a missing `Data.notify`) and verify the gate refuses the merge;
    inject a TUI startup exception and verify the same. Healthy clients pass and exit cleanly. Also: a tree
    whose `gui/app.py` fails to import fails the gate when run from the engine's installed python; a fake
    client that exits -11 fails, it doesn't skip; the real registry's hash is unchanged after a run. The check runs
    headlessly without interacting with the human's live project.

### Out of scope
- Automatically restarting or relaunching the human-facing client. Engine restart remains ENG-042/#28.
- Agent-run stalls and timeouts, already covered by ENG-050.
- Sending crash reports or their contents off-machine; this workflow uses local files, board tasks and delivery.

## Wake-ups (who runs, when, why)
Each agent run is one session of a backend CLI. Agents never loop; they are woken with a reason.
- **REQ-ENG-010 [x]** Reasons, in priority order: `chat` (human messaged them) > `poke` (human clicked Wake)
  > `review` (QA: a task is in review) > `messages` (unread mail, 2s debounce) > `task` (owns an actionable
  task) > `proactive` (role cadence elapsed AND something significant changed since they last looked).
- **REQ-ENG-011 [x]** Pending human chat pre-empts every other wake reason for that agent; any run that
  consumes a human chat message replies to the human with its final text.
- **REQ-ENG-012 [x]** Concurrency: at most `budget.max_concurrent` autonomous runs; chat gets up to 2 extra slots.
  - When eligible task work is waiting and no autonomous task-work run is active, it gets the next free
    autonomous slot ahead of coordination wakes. Task work means a worktree-role agent with a due,
    dependency-ready `ready`/`in_progress` task, including a mail/poke wake carrying that task.
    Choose the least recently run eligible agent (id breaks ties). Other slots keep wake-priority order.
    Existing runs are not interrupted; the bound is the first tick with a free autonomous slot after
    current runs finish. Pause, budgets, backend limits and failure backoff still apply; chat stays first.
- **REQ-ENG-013 [x]** Budget: `max_runs_per_hour` (autonomous runs) and `max_usd_per_day` (claude-reported
  cost). When exceeded, autonomous wakes stop and the top bar shows "Throttled" with the reason.
- **REQ-ENG-014 [x]** Pause: stops autonomous work; chat is still answered.
- **REQ-ENG-015 [x]** Failed runs back off exponentially per agent (30s → 10m) and their mail is re-queued.
- **REQ-ENG-050 [x]** (#61) Run watchdog, for every backend. Observed 2026-09-24: codex runs hung silently for
  2h45m (builder-2) and 78 min (QA) at 0% CPU, and nobody noticed.
  - Each run tracks the time of its last stream event (any line from the backend), taken in the engine from the
    run's stream, so the watchdog doesn't touch the protected `runners.py`.
  - **Stall:** no output for `[budget] stall_minutes` (default 15) → status `stalled`.
  - **Hard cap:** a run exceeding `max_run_minutes` → status `timeout`. The default is 90 min for worktree roles
    (builder, and QA reviewing in a worktree) and `max_coord_run_minutes` = 30 for everyone else. Chat runs are
    exempt from the hard cap but not from the stall rule.
  - **On stall or timeout:**
    - kill the process group plus verified descendants in other groups (by pid ancestry, never by name);
    - re-queue the run's mail and apply the normal failure backoff (REQ-ENG-015), so an agent can't loop into the
      same hang;
    - log a feed event and a needs-help notification (REQ-ENG-047);
    - the Agent view shows the reason on the run.
  - **Zombie runs:** each tick, a run marked `running` whose process no longer exists becomes `interrupted` (as
    REQ-ENG-004 does at start).
  - Test:
    - a silent fake runner is killed after a small `stall_minutes`, with no orphans (descendants included);
    - it's marked `stalled` with mail re-queued, backoff applied and a notification raised;
    - a chatty long run survives until `max_run_minutes`, then `timeout`;
    - chat runs are exempt from the cap;
    - a vanished process becomes `interrupted`.
- **REQ-ENG-016 [x]** Rate-limit awareness: when a backend reports a usage/rate limit, back off that backend
  globally until its reset time and surface it in the GUI. (#2)
  - Detection: claude `rate_limit_event` with status ≠ `allowed`, or a failed run whose error text mentions a
    rate/usage limit; codex error events mentioning a rate/usage limit.
  - Reset = the reported reset time; if none is reported, now + 15 min. Stored in kv (`limit.<backend>`) so
    it survives an engine restart.
  - While limited, **no** run of any kind (including chat) starts on that backend; agents on other backends
    are unaffected. Pending chat stays queued and the Chat view shows "Claude limited · resets in 42m" on the
    working bubble instead of silently waiting.
  - A rate-limited run does not count toward the per-agent failure backoff (REQ-ENG-015) and its mail is re-queued.
  - Top bar shows one pill per limited backend with the time until reset (REQ-GUI-001); it disappears at reset.
  - **Countdown copy (human preference, 13:55):** every user-facing reset, backoff or pacing time is shown as
    time *until* ("resets in 4h"), never as clock time, in the GUI, TUI, `troupe status` and notifications.
    Format: design/system.md "Countdown copy" (#105). Stored values stay absolute timestamps (`resets_at`).
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
  | pm_1 | claude · fable · high → claude · opus · high (REQ-ROLE-030: strongest model) |
  | spec_1 | codex · default · high → claude · opus · high |
  | designer_1 | claude · sonnet · medium → codex · default · medium |
  | builder_1, builder_2 | codex · default · high → claude · sonnet · high → local · detected |
  | architect_1 (#36) | claude · opus · high → codex · default · high |
  | qa_1 | claude · opus · high → codex · default · high |
  | researcher_1 (#41) | local · detected → codex · default · medium |
  | gadfly_1 | local · detected → codex · default · medium |
  - "detected" = the first model the local server reports during `troupe init`. If no local server answers, local
    entries are written commented out, with a note saying how to enable them.
  - Default `provider_limits`: claude `five_hour: 80, seven_day: 50` (the human's choice, question #18: the 5h window
    refills fast, and the weekly one protects the subscription); codex and local uncapped.
  - `troupe init` writes the architect and researcher once their roles exist (#36, #41), and either can be disabled
    in team.yaml.
  - Test: the generated team.yaml validates and matches the table.
  - This is the default for **new** projects. troupe's own roster is the human's separate choice (2026-09-24, "for
    now"): mixed builders (1 claude, 1 codex, 1 local for basic tasks), PM on claude, spec on codex. It lives in this
    project's team.yaml, not in this table.

## Prompts
- **REQ-ENG-020 [x]** System prompt = team charter (roster, rules, tools) + role prompt (`roles.py`).
- **REQ-ENG-021 [x]** Wake prompt contains: reason, new messages, current task brief, other open tasks,
  board (lead/pm/spec/gadfly), the agent's unanswered questions, recent team decisions, private notes, team
  status, what changed since last look (proactive), and a reason-specific instruction.

## Tasks
Lifecycle: `backlog → ready → in_progress ⇄ blocked → review → approved → done` (+ `cancelled`).
- **REQ-ENG-030 [x]** Tasks created by the Lead or human start `ready`; by anyone else `backlog` (Lead triages).
- **REQ-ENG-031 [x]** Dispatch: unassigned `ready` tasks go to the least-loaded enabled agent of the task's
  role; worktree roles (including builders) hold at most one active task, even with a single-agent roster.
  `ready`, `in_progress` and `blocked` assignments count toward that cap; backlog, review, approved,
  done and cancelled do not. Explicit lead assignments bypass dispatch, and a rejected task may return
  while another is active; current-task selection retains its existing priority order. `depends_on` must be done first.
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
  - (#81) **Main moved during the check.** If main moves while the check runs (which it often does, because of docs
    autocommits), the result is not a failure.
    - The worker re-merges main into the branch and re-runs the check, up to 3 times with backoff. It doesn't message
      the builder, and it doesn't count toward `max_task_attempts`.
    - [ ] (#107) **Doc-only movement doesn't count.** If every path changed on main since the checked main
      head matches `[git] doc_only_paths`, the checked tree merges onto current main without a re-check, and
      the merged tree contains both. The default globs are `specs/**`, `design/**`, `docs/**`, `*.md`,
      `README*` and `LICENSE`. A commit set with any path outside the globs takes the re-merge + re-check path.
      `doc_only_paths` is guarded by REQ-SAFE-021, since widening it would skip re-checks.
      Config load also rejects over-broad patterns as a config error, even ones the human approved. That means
      `*`, `**`, and any pattern matching a typical source, test or build path (`src/x.py`, `tests/test_x.py`,
      `x.py`, `pyproject.toml`, `uv.lock`). The human's approval stays the primary guard, because those probe
      paths assume a Python layout. Test: each probe-matching pattern is refused with a message naming it.
    - [ ] (#107) **Exhausted retries don't bounce approved work.** After 3 consecutive "main moved" retries
      caused by real code movement, the task stays `approved` and the merge is requeued with a backoff
      (`next_attempt_at`). It's logged to the check log only: no builder mail, no builder wake, no QA re-review.
      (This replaces the earlier rule that sent the task back to the builder.)
    - A real check failure or merge conflict goes back immediately.
    - The merge into main is always of the checked tree, plus only doc-only commits, and the worker stays serial.
    - Test: main moving during a check retries and merges silently; a real failure still goes back with output; a
      specs/*.md commit during the check merges on the first attempt with both changes; a src/ commit re-checks;
      a mixed doc+code commit re-checks; exhausted retries leave the task approved and requeued, and
      `check_failed` isn't called.
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
- **REQ-ENG-045 [x]** (#50) Milestones are first-class (human: Pulse should show the major work and how close the goal is).
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
- **REQ-ENG-046 [x]** (#50) The engine publishes each agent's **wait state** and mail backlog every tick, as data (additive
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
  - `waiting_on` is null while the agent has a run in progress (it's working, not waiting). The API serializes this
    as `WaitingOn` with a `targets` list (REQ-API).
  - Mail: `mail_queued` = unread messages not yet delivered. `mail_reading` = messages delivered to the currently
    running run.
  - Test: one fixture per kind, precedence when several apply, and a stable `since`.

## The human's attention and requests
- **REQ-ENG-047 [~]** (#35 shipped; #89/#91 extensions pending; human: "we can then also notify when someone needs help") The service sends **needs-help**
  notifications, and they work with the GUI closed.
  - Events:
    - a new `ask_human` question or `propose_idea`, or an approval card (REQ-SAFE-020);
    - a task going `blocked`;
    - the merge gate (REQ-ENG-040) failing twice in a row on one task;
    - a backend rate-limited or every provider unavailable (REQ-ENG-016, REQ-BE-012), once per limit window with
      the time until reset;
    - an agent whose failure backoff has reached its cap;
    - a crash loop (REQ-ENG-042);
    - a process crash report (REQ-ENG-057, notification kind `crash`, #91);
    - a run watchdog stall or timeout kill (REQ-ENG-050);
    - a budget throttle;
    - safety events (REQ-SAFE-040).
  - **Anti-noise:** at most 1 notification per 30 s per project. Pending events coalesce into "3 things need you in
    troupe" listing the top ones, and the same item is never repeated. A ★ task going blocked (REQ-ENG-048) skips
    the 30 s delay.
  - **One notifier:**
    - OS notifications are skipped while a GUI is focused (the GUI writes a `gui_focused_at` heartbeat to kv; the
      GUI shows toasts instead);
    - they are also skipped while an API client with `notifications: true` is connected (REQ-API-010);
    - the GUI's own osascript path is removed.
  - Titles use full handles plus the project, e.g. "qa_1@troupe needs you".
  - Config: `[notify] enabled = true` and a `quiet` list of event kinds to mute.
  - **Must-deliver kinds** (#89, found by QA; `crash` added by #91, pending): `safety`, `crash_loop`, `concern`,
    `kill` and `crash` ignore `quiet` and
    `enabled`, so no config edit can silence them. `[notify]` also joins the human-approved config (REQ-SAFE-021),
    so an agent's edit to it isn't enforced until the human approves the card. #89's diff adds `[notify]` to the
    SAFE-021 text, which is protected, so the human approves it. Test: with every kind in `quiet` and
    `enabled = false`, all five must-deliver kinds still notify.
  - Delivery for now is osascript, with safe escaping. There's no click-through until the Mac app (REQ-MAC).
  - Test: coalescing, dedupe, focus suppression and `quiet`, with delivery mocked. Manual: GUI closed + a question →
    a notification within about 5 s.
- **REQ-ENG-048 [ ]** (#45; human: "if I ask you, specifically to do something, I want you to run that to the ground")
  Human requests are ★ tasks.
  - Tasks the human creates, or that an agent creates with `create_task(for_human=True)`, get `human_request=1`
    (additive) and store the human's original words (quote plus message id).
  - ★ shows on board cards, the Pulse Work panel and the task modal. A "Your requests" filter lists each one with its
    status in plain words.
  - Within the same priority, ★ tasks dispatch first.
  - **Nothing is dropped silently:** an agent cancelling a ★ task or changing its acceptance/scope gets `ERROR:`,
    and a Needs-you card is created instead (Cancel / Keep going / Change to…). Only the human closes a ★ task
    without completing it.
  - **Persistence:** at `max_task_attempts`, a ★ task doesn't just block. It escalates to the lead to re-plan
    (split, reassign, research, different provider) before the human is asked, and the needs-help notification fires
    immediately.
  - **Done = reported:** when a ★ task, or every task from one request, is done, the human gets "Done: <their
    words> → what was delivered, where to look".
  - Test: the ★ flag and quote, dispatch order, the cancel/scope guard, escalation at max attempts, and the Done
    report. Screenshot of the badge and filter.
- **REQ-ENG-049 [x]** (#46; human: "is there some way we could insert a local llm to parse the busy work?") Cheap
  wake-ups via local-LLM mail triage.
  - Before a `messages` wake, a local model (`[triage] enabled, model`, using the `local` backend) reads the pending
    mail, the agent's role and its active task title. It returns `{wake_now, reason, digest}`.
  - `wake_now=false` holds the mail for the next wake, **never dropping it**. The feed shows "triage: held 2 for
    lead_1@troupe" with the digest.
  - **Hard rules the model can't override:** human mail, questions to the agent, review requests, mail about the
    agent's own active task, and ≥ N pending mails always wake. If the local model is down or errors, the agent
    wakes as today.
  - Several pending mails become one wake. `[triage]` defaults: `enabled=false`, `model=""` (use the local
    agent model), `max_pending=5`, `timeout=5.0` seconds (maximum 30). The count guard includes FYIs.
  - Classification is asynchronous and cached per pending batch/config; pending classification never blocks
    heartbeat/chat. New mail invalidates the batch. Holds do not mark mail read and do not themselves
    trigger cadence wakes. Questions/review words and explicit own-task links bypass conservatively.
  - Hourly metrics exclude chat; runs with a task link or task/review reason count as work, others as coordination.
    Synthetic regression replay: 28 arrivals → 8 message wakes; this is not a measured deployment saving.
  - Metrics: runs record `wake_reason` and tokens, and the Usage/Pulse view shows coordination vs work tokens per hour.
  - FYI mail never reaches triage (REQ-COM-013, #74). Triage sees only ambiguous mail.
  - Test: with a mocked model, a hold, the hard rules and the fallback. A replay of an hour of mail shows ≥ 50% fewer
    `messages` wakes.

## Git posture per project (#80)
Human, 2026-09-24: "a git posture: are we branching a lot, how do we merge, is there a deploy branch, do we push at
feature breaks or to a working group at night, etc." Also: GitHub-first. **Pushing is opt-in**, and nothing is
pushed, no remote is added and no history is rewritten until the PM confirms the human's commit-authorship decision
(lead hold, 2026-09-24).
- **REQ-ENG-052 [ ]** A `[git]` posture in the project config. It lives in `troupe.toml` next to `setup`/`check`
  (ENG-038/040) unless #73's core-vs-project boundary moves it. Keys, validated like ENG-019:
  | key | values | default |
  |---|---|---|
  | `branching` | `per_task` (worktree per task, today) \| `trunk` (reserved; rejected until specced) | `per_task` |
  | `merge` | `merge_commit` (today, `--no-ff`) \| `squash` \| `rebase` | `merge_commit` |
  | `main_branch` | branch name | `main` |
  | `deploy_branch` | branch name or empty | empty (none) |
  | `push` | `never` \| `on_merge` \| `on_milestone` \| `nightly@HH:MM` | **`never`** |
  | `push_remote` | a remote in `[safety] remotes` (REQ-SAFE-032) | `origin` |
  | `pull_requests` | `off` (only value supported now; `per_task`/`per_milestone` reserved) | `off` |
  | `commit_name`, `commit_email` | the identity for agent commits (the human's chosen address) | unset |
  | `allowed_emails`, `private_emails` | addresses allowed in commit metadata / addresses that must never appear in pushed content | empty |
  - Unknown or reserved values are rejected with an error naming the key. `[git]` push settings are guarded like
    `check` (REQ-SAFE-021): an agent's hand edit isn't enforced until the human approves it.
- **REQ-ENG-053 [ ]** Merge strategy. After approvals and the merge gate (REQ-ENG-040/043, SAFE-020), the engine
  merges with the configured `merge` into `main_branch`.
  - `squash`: one commit per task, with the message "#<id> <title>" and the task link.
  - `rebase`: the task branch is rebased onto main and fast-forwarded. A rebase conflict takes the REQ-ENG-034 path.
  - The engine never rewrites `main_branch` or `deploy_branch` history.
- **REQ-ENG-054 [ ]** Deploy branch. When `deploy_branch` is set, `troupe promote` (human-only CLI/API) or a
  milestone the lead marks `done` with `promote=true` merges `main_branch` into it (merge commit, never forced). A
  feed event records the promotion, including the commit range.
- **REQ-ENG-055 [ ]** Push policy (opt-in).
  - With `push` ≠ `never`, the engine pushes `main_branch` (and `deploy_branch` after a promotion) to `push_remote`
    at the configured moment: after each merge, when a milestone is marked done, or nightly at HH:MM local time.
  - **Pre-push checks.** A push is refused, with a needs-help notification (ENG-047) and a feed event, and never
    retried with force, if:
    - (a) `push_remote` isn't in `[safety] remotes`;
    - (b) `commit_email` is unset;
    - (c) any commit in the range to push has an author or committer email other than `commit_email` or the
      human's addresses in `[git] allowed_emails`. This stops a work email leaking into a public repo (Principle 0);
    - (d) the SAFE-031 secret scan finds anything in the range;
    - (e) any file content in the range contains an address from `[git] private_emails` (the human's addresses that
      must never be public, e.g. a work email). Found 2026-09-24: the work email was in `pyproject.toml`'s authors, so
      commit metadata alone isn't enough to check.
  - A non-fast-forward rejection is reported, never forced (SAFE-032). Force-push to main/deploy is impossible.
  - Every push, and every refusal, appears in the feed and the TUI (last push time and result).
  - The default for new projects is `never`. troupe's own posture (`per_task`, `merge_commit`, `on_merge` to
    `origin main`) only goes live after the human confirms via the PM.
  - troupe's own values: `commit_email` is the human's chosen personal address (decision in memory; it isn't
    repeated in specs). The existing history's work email is rewritten once, before the first public push, as a
    human-directed one-off operation in a quiet window the lead arranges. The engine itself never rewrites history
    (ENG-053).
  - Test, with a local bare remote:
    - each merge strategy produces the expected history;
    - `on_merge` pushes after a merge and `never` never pushes;
    - pushes are refused for an unlisted remote, a missing `commit_email`, a foreign email in the range, and a
      planted secret;
    - force is never used;
    - promote merges main into deploy.
- Future, as separate tasks: GitHub Issues ↔ board sync, and one PR per task (`pull_requests`).

## Task calibre tiers (#75)
- **REQ-ENG-051 [ ]** (#75; human: "one good model … and one medium model … for different calibre of tasks, which the
  lead should determine") The model a run uses comes from the **task's tier**. Builders stay provider lanes, so
  there's no extra agent per model.
  - Tasks gain `tier` = `hard | medium | basic` (additive column, default `medium`), and `create_task`/`update_task`
    accept it.
  - The lead's prompt gives the guidance: architecture, safety or tricky concurrency → hard; routine features →
    medium; docs, small edits and tests → basic. `roles.py` is protected, so the human approves the change.
  - `team.yaml`: each agent may have a `tiers:` map from tier to `{provider, model, level}`, for example the claude
    builder `{hard: opus/high, medium: sonnet/high}` and the local builder `{basic: <local model>}`. An unmapped
    tier uses the agent's normal `providers` list (REQ-ENG-019). Validated like ENG-019.
  - **Dispatch** (extends REQ-ENG-031):
    - `basic` tasks prefer a free builder that maps `basic`, otherwise any free builder at its cheapest mapped tier;
    - `hard` tasks prefer builders that map `hard`;
    - `medium` tasks go to the least-loaded builder as today.
  - At launch, the tier's entry is tried first, then fallback follows BE-012 (caps, rate limits, down providers).
  - **Visibility:** the task (board card, TUI Tasks pane, API Task) shows its tier and the model its last run
    actually used. Runs record the tier, so usage per tier can be reported.
  - Test:
    - a hard task on the claude builder launches opus and a medium one sonnet (launch args);
    - the codex builder maps hard/medium to its configured models;
    - a basic task goes to the local builder when it's free;
    - caps/fallback still apply;
    - an unmapped tier uses the default.

## Open questions
- Should QA be able to push small fixes itself, or always bounce to the builder?
- Should the human approve tasks before builders start ("human-gated" autonomy mode)?

## Changelog
- 2026-09-24 — ENG-040: over-broad `doc_only_paths` patterns are rejected at load (#107, lead decision).
- 2026-09-24 — ENG-040 (#107): doc-only main movement merges without a re-check (`[git] doc_only_paths`, guarded
  by SAFE-021), and exhausted main-moved retries keep the task approved and requeued instead of bouncing it.
- 2026-09-24 — ENG-059 hardened from QA's #92 review: runs the candidate tree's code, deterministic injection
  (10/10), skip decided up front (signal exit = fail), isolated HOME, cleanup in `finally`. New ENG-060: engines
  exit when their project disappears, plus test-session process hygiene (#103). ENG-008: missing-path entries
  hidden, `--prune` with backup, tests never write the real registry (#106). ENG-016/047: countdown copy (#105).
- 2026-09-24 — ENG-056..059 reconciled with the #91/#92 briefs: report fields transcribed (editorial dependency
  closed), entrypoint list, pre-window handler, one-tick filing to the builder role, startup triage + PM brief
  (human 12:59), ENG-059 now owned by #92 with live insertion of all three events, and a gate skip is never a pass.
- 2026-09-24 — ENG-056/057/059 refined from lead msg #818: report filenames, non-zero exits and stderr traceback,
  `(entrypoint, last frame)` deduplication, and both client launch checks with a pending safety card plus a live
  `needs_help` insertion. No off-machine reporting. ENG-047 marked partial for pending #89/#91 extensions.
- 2026-09-24 — ENG-056..059: crash reports, deduplicated P0 filing/PM+lead mail, must-deliver `crash`, engine
  restart ingestion and GUI/TUI launch smoke gate (#91, PM msg #814). Resolved the crash-watcher scope question:
  both client and engine, no client auto-relaunch, engine supervision remains deferred under #28. Exact report
  fields still need transcription from #91 (task/memory tools denied by approval policy).
- 2026-09-24 — ENG-040 retry exhaustion clarified; recorded the new crash-watcher request without assuming
  that the deferred background-service supervisor is its intended implementation.
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
- 2026-09-24 — new ENG-047 needs-help notifications (#35), ENG-048 ★ human requests (#45), ENG-049 local-LLM mail
  triage (#46). These tasks were in flight without REQs.
- 2026-09-24 — ENG-050 run watchdog (stall/timeout/zombie), one REQ for the overlapping #61 and #62 briefs.
- 2026-09-24 — lifecycle change: the TUI owns the engine ("quit and everything quits"). ENG-001/006 superseded for the default
  flow, ENG-009/042 deferred (human via pm msg #431).
- 2026-09-24 — ENG-051 task calibre tiers (#75, human). ENG-049: FYI never reaches triage (#74). ENG-041 note: troupe's
  own roster differs from the init default.
- 2026-09-24 — ENG-052..055 git posture per project (#80): push opt-in, identity/email and secret pre-push checks, never
  force. BE-016/ENG-041 caps are per-window, 80/50 (human, question #18).
- 2026-09-24 — ENG-040: auto-retry when main moves during checks (#81).
- 2026-09-24 — ENG-047: must-deliver notification kinds and a guarded `[notify]` (#89).
