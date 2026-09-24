# Engine local API — v0

Each project's engine service serves a local API. The native Mac app (`mac/`) talks to each engine only
through it. The raylib GUI keeps reading the DB directly until it is retired at parity.
Code: `src/troupe/api.py` (server, runs inside the engine service), `src/troupe/api_client.py` (Python client +
`troupe api` CLI), `tests/test_api.py`.

Conventions: timestamps are **float epoch seconds** (UTC, same as the DB). Money is USD as a float. Ids are
integers unless noted. `handle` is the full `role_N@project` (REQ-COM-005). Every object may gain fields.

## Transport
- **REQ-API-001 [x]** One Unix domain socket per project at `<root>/.troupe/api.sock`. No TCP port, ever (v0).
  - The engine service creates it with mode 0600, owned by the user, and removes it on a clean stop. On start, a
    leftover socket file that refuses connections is unlinked and replaced. One that accepts connections means
    another engine owns it: the new engine logs an error and does not serve (ENG-003 already prevents this).
  - Path too long for `sun_path` (>100 bytes): the engine binds `~/.troupe/sock/<first 16 hex of sha1(root)>.sock`
    instead and writes that path to `.troupe/api.sock.path`. Clients check `api.sock.path` first, then `api.sock`.
  - No socket, or connection refused → the client shows "Engine offline" and retries with backoff (1 s → 10 s).
  - Test: permissions are 0600, a stale socket is replaced, the long-path fallback is used and found.
- **REQ-API-002 [x]** Framing: newline-delimited JSON, UTF-8, one object per line, `\n` terminated.
  - Request: `{"id": <int|str>, "method": "<name>", "params": {...}}` (`params` optional, defaults to `{}`).
  - Response: `{"id": ..., "ok": true, "result": ...}` or `{"id": ..., "ok": false, "error": {"code": "...",
    "message": "...", "data"?: {...}}}`. `message` is human-readable; clients branch only on `code`.
  - Push: `{"event": "<type>", "seq": <int>, "data": {...}}` (REQ-API-060). A line is a push iff it has `event`.
  - Requests on one connection are processed **sequentially**, and responses are sent **in request order**.
    Pushes may interleave between responses. Clients should still match responses by `id`.
  - A request line over **4 MiB**, or one that isn't valid JSON: error `bad_request` with `"id": null` (the line
    has no trustworthy id), then the server closes the connection.
  - Response lines are bounded by paging limits. Any single text field over 1 MiB (e.g. a huge transcript line) is
    cut to 1 MiB and the containing object gets `"truncated": true`.
- **REQ-API-003 [x]** Error codes (closed list for v0; clients treat unknown codes as `internal`):
  | code | meaning |
  |---|---|
  | `bad_request` | malformed line, missing or invalid params (message names the param) |
  | `handshake_required` | a method other than `hello` was sent before `hello` succeeded |
  | `unsupported_version` | `hello` asked for an API version this server doesn't speak |
  | `unknown_method` | no such method |
  | `not_found` | the referenced agent, task, question, memory, run or decision doesn't exist |
  | `forbidden` | the human isn't allowed to do this (e.g. dismissing an approval) |
  | `conflict` | the target changed state (question already answered, reload already running) |
  | `resync_required` | the requested event history is gone; re-fetch `snapshot` (REQ-API-062) |
  | `unavailable` | the method is in the contract but its feature hasn't shipped; `data: {req, task}` (REQ-API-006) |
  | `internal` | server bug; the message has a one-line reason, the traceback goes to `engine.log` |
- **REQ-API-004 [x]** Multi-project: a client opens one connection per project listed in `~/.troupe/projects.json`
  (REQ-ENG-008). There are no cross-project methods in v0. Each connection is independent.
- **REQ-API-006 [x]** Staged availability. The contract is complete now, but some features ship in other tasks.
  #48 does **not** build those features. Instead:
  - **Reads:** every object shape is served from day one. Fields whose feature hasn't shipped get neutral defaults:
    `milestones: []`, `milestone_id: null`, `waiting_on: null`, `mail_reading: 0`, `major/pinned: false`, memory
    `status: "active"`, `comments: []`, `room: null`. Clients render them without special cases.
  - **Commands and events:** these return `unavailable` with `data: {req, task}` until the owning task ships. That
    task wires its method, fields and events into the API as part of its own acceptance (a test calling it over
    the socket).
    | method / data | owning REQ → task |
    |---|---|
    | `room_message`, `room` field | COM-024 → #4 |
    | `stop_team` (live), `mark_seen human_last_seen` catch-up, `troupe start` (live) | ENG-006, GUI-028 → #24 |
    | `reload`, `service.json`, Engine `reloading` | ENG-009/042, API-074 → #28 |
    | `stop_now`, approval questions (`decision`) | SAFE-010/020 → #42 (wire in #48 if #42 has merged first) |
    | `update_memory`, `delete_memory`, `major/pinned/status` | COM-032/033/034 → #8 |
    | `comment_decision`, `comments`, `decisions_seen_at` | COM-035..037 → #27 |
    | `update_config` | ENG-019 + SAFE-021 → #5 |
    | `milestones`, `milestone_id`, `waiting_on`, `mail_reading`, `milestone.changed` | ENG-045/046 → #50 |
  - **Must land in #48:**
    - transport, handshake, error codes and security (API-001..006, 010, 011);
    - `snapshot` and every read for data that exists in main;
    - the commands backed by today's GUI actions: `chat`, `mark_chat_read`, `answer_question`,
      `dismiss_question`, `wake`, `stop_run`, `set_agent_enabled`, `new_session`, `pause`/`resume`, `create_task`,
      `update_task`, `add_task_note`, and `mark_seen` storing the kv keys;
    - `subscribe` with resume, backpressure and the latency target;
    - the non-functional REQs, `troupe api`, and `tests/test_api.py`.
  - Test: each staged method returns `unavailable` with its `req`/`task`, and a snapshot on current main has every
    field present.
- **REQ-API-005 [x]** Security: anyone who can open the socket is the human (file permissions are the auth).
  The API never returns secret values. Config values whose key matches `key|token|secret|password`
  (case-insensitive) are returned as `"***"`, as is anything read from the environment.

## Handshake
- **REQ-API-010 [x]** `hello` must be the first request. Params: `{"api_version": 0, "client": "troupe-mac/0.1",
  "notifications": bool}`. With `notifications: true` the client says it shows OS notifications itself. While at
  least one such client is connected, the engine's own notifications (task #35) are suppressed, to avoid duplicates.
  - Result: `{api_version, project, handle_suffix, root, troupe_version, epoch, seq, server_time, engine}`.
    `project` is `[project] name`; `handle_suffix` is `@<project>` as used in handles; `root` is the absolute
    project path; `epoch` is a random string that changes every time the engine (re)starts or reloads; `seq` is
    the latest event seq; `engine` is an Engine object.
  - A different `api_version` → `unsupported_version` with `data: {"supported": [0]}`, then the server closes.
  - Any other method first → `handshake_required`; the connection stays open.
- **REQ-API-011 [x]** `ping` → `{"pong": true, "server_time": <ts>}`. Allowed before `hello`.

## Object shapes
Fields marked `?` may be null. Lists are never null.
```
Agent      id: str, handle, name, role, state: "idle"|"running", enabled: bool, parked: bool (REQ-GUI-002 rule),
           status: str (set_status text), activity: str (live, ≤160 chars), current_run: int?,
           providers: [str] (configured chain, primary first; REQ-BE-012), provider: str (the one the current or
           next run uses), fallback: bool (provider ≠ providers[0]), model: str, level: str?,
           chat_unread: int (chat from this agent the human hasn't read), mail_queued: int (mail to this agent not
           yet delivered), mail_reading: int (mail delivered to the running run; REQ-ENG-046), waiting_on: WaitingOn?, runs: int, tokens: int, cost: float, last_run_at: ts?
WaitingOn  kind: "human"|"review"|"dependency"|"blocked"|"rate_limit"|"slot"|"providers"|"parked",
           target: str|int? (question id | reviewer handle | task id | provider), since: ts, detail: str,
           reset_at?: ts (rate_limit), queue_position?: int (slot). Null while running. If several apply, the
           first in this order wins: rate_limit, providers, slot, human, review, dependency, blocked, parked.
Task       id, title, description, acceptance, territory, status, priority: 0..3, role, assignee: str?,
           reviewer: str?, created_by, depends_on: [int], milestone_id: int?, branch: str?, worktree: str?,
           attempts, next_attempt_at: ts, result, review_notes, created: ts, updated: ts, notes_count: int,
           flags: {checks_failed: bool, arch_review: bool, human_request: bool, territory_conflict: [int]}
           + from `task` only: notes: [TaskNote], reviews: [{ts, reviewer, verdict: "approve"|"reject", notes}]
TaskNote   id, ts, author: str, text
Milestone  id, name, goal, order: int, done: int, total: int (tasks, excluding cancelled)
Message    id, ts, sender, recipient, subject, body, kind: "msg"|"chat"|"system", room: str? ("team"),
           reply_to: int?, task_id: int?, read_at: ts?
Question   id, ts, asker, kind: "question"|"idea"|"approval", question, context, options: [str],
           status: "open"|"answered"|"dismissed", answer: str?, answered_at: ts?,
           answered_via: "inbox"|"chat"? (REQ-COM-026), task_id: int?,
           approval?: {task_id, branch, paths: [str]} (kind approval only; REQ-SAFE protected paths)
Memory     id, ts, agent, kind: "decision"|"note"|"fact"|"idea"|"preference", title, content, rationale,
           scope: "team"|"private", major: bool, pinned: bool, superseded_by: int?,
           status: "active"|"superseded"|"reverted", comments_open: int
Comment    id, decision_id, ts, author, body, outcome: "acknowledged"|"revised"|"superseded"|"reverted"?
Run        id, agent, started: ts, ended: ts?, reason, status: "running"|"ok"|"error"|"interrupted", chat: bool,
           provider, model, cost, tokens, task_id: int?, summary, lines: int
RunLine    seq: int (run_lines.id), ts, kind: str ("text"|"tool"|"result"|"error"|…), text
Activity   id, ts, agent, kind, text, ref: str ("task:12", "msg:40", …), significant: bool
Engine     state: "live"|"stopped"|"paused"|"throttled"|"reloading", paused: bool, stopped: bool (kill switch,
           REQ-SAFE-010; only the human resumes), throttled: str?, heartbeat: ts,
           version, pid, started_at: ts, running_runs: int, draining_runs: int, config_errors: [{file, message}]
           (state precedence: stopped > reloading > paused > throttled > live)
Usage      budget: {max_runs_per_hour, max_usd_per_day, max_concurrent}, runs_1h: int, cost_24h: float,
           throttled: str?, providers: [{provider, limited_until: ts?, windows: [{name: "5h"|"7d"|…,
           used_pct: float, cap_pct: float?, resets_at: ts?}]}]
Seen       human_last_seen: ts?, decisions_seen_at: ts?
```

## Reads
- **REQ-API-020 [x]** `snapshot` returns everything the first frame needs in one round-trip:
  `{seq, server_time, engine, usage, seen, agents, tasks, milestones, questions (open), recent_messages}`.
  - `tasks` is every task (no notes bodies). `recent_messages` is the newest `messages_limit` (default 200,
    max 1000), newest first. `seq` is the last event already reflected in the result, so
    `subscribe {since_seq: seq}` continues with no gap and no loss.
  - Test: on a fixture with 500 tasks and 5k messages it returns in <200 ms (REQ-API-070).
- **REQ-API-021 [x]** Single-type reads (each returns `{items: [...]}` unless noted):
  | method | params | notes |
  |---|---|---|
  | `agents` | — | all agents, roster order |
  | `tasks` | `status?: [str]`, `milestone_id?` | priority, then id |
  | `task` | `id` | one Task with `notes` and `reviews` (result is the Task) |
  | `milestones` | — | by `order` |
  | `messages` | `before_id?`, `limit` (≤500, default 100), `agent?` (sender or recipient), `chat_with?` (1:1 human thread), `kind?`, `room?`, `task_id?` | newest first; `next_before_id` null when exhausted |
  | `questions` | `status` (`open`\|`answered`\|`dismissed`\|`all`, default `open`), `limit` (default 50) | newest first |
  | `memories` | `kind?`, `status?`, `major?`, `limit` (≤1000, default 400) | newest first, private included |
  | `decision_comments` | `decision_id` | oldest first |
  | `runs` | `agent?`, `before_id?`, `limit` (≤200, default 30) | newest first; `next_before_id` |
  | `run_lines` | `run_id`, `after_seq?`, `limit` (≤2000, default 500) | oldest first; `next_after_seq`, `done` (run ended and no more lines) |
  | `activity` | `before_id?`, `limit` (≤500, default 300), `significant_only?` | the Pulse feed; newest first |
  | `usage`, `engine`, `seen` | — | result is the object |
  | `config` | — | `{troupe_toml, team_yaml}` parsed, secrets redacted (REQ-API-005) |
  - Unknown ids → `not_found`. Bad filter values → `bad_request`.
- **REQ-API-022 [x]** Docs are **not** in the API. The client reads `README.md`, `specs/`, `design/` and `docs/`
  straight from `root` (from `hello`). They are plain read-only files on the same machine, so a round-trip adds
  nothing. Only mutations and engine-owned state go through the API.
- **REQ-API-023 [x]** Direct reads of `.troupe/troupe.db` stay possible for the CLI and scripts. They are not a
  supported client contract: the schema may change without an `api_version` bump. The Mac app uses only the API.

## Commands
- **REQ-API-040 [x]** Every command acts as actor `human`, has the same DB effect and logs the same activity event
  as the matching raylib GUI action today (or the cited REQ for new ones), and returns once its effect is
  committed. For commands the engine loop must carry out (marked ⟳), the result means "accepted"; the effect
  follows as pushed events.
  | method | params | effect | result |
  |---|---|---|---|
  | `chat` | `agent`, `text` | chat message human → agent (REQ-COM-020) | `{message_id}` |
  | `room_message` | `text` | Team room message + mention routing (REQ-COM-024) | `{message_id, woken: [id]}` |
  | `mark_chat_read` | `agent` | marks that agent's chat to the human read | `{count}` |
  | `answer_question` | `id`, `text`, `decision?` | answer + mail to asker (REQ-COM-021) | `{question}` |
  | `dismiss_question` | `id` | dismiss (REQ-COM-023); `forbidden` for kind `approval` | `{question}` |
  | `wake` ⟳ | `agent` | poke: wake now, clear failure backoff (REQ-ENG-010) | `{accepted}` |
  | `stop_run` ⟳ | `agent` | kill the agent's current run (REQ-BE-006) | `{accepted, run_id?}` |
  | `set_agent_enabled` ⟳ | `agent`, `enabled` | enable/disable; disabling kills a live run | `{agent}` |
  | `new_session` ⟳ | `agent` | reset session + local history | `{accepted}` |
  | `pause` / `resume` | — | kv `paused` (REQ-ENG-014) | `{engine}` |
  | `stop_now` ⟳ | — | REQ-SAFE kill switch: every agent process group killed within 2 s, then paused | `{killed: int}` |
  | `stop_team` ⟳ | — | graceful service stop (REQ-ENG-006); response sent before the socket closes | `{accepted}` |
  | `reload` ⟳ | — | graceful reload (REQ-ENG-009); `conflict` if one is running | `{accepted}` |
  | `create_task` | `title`, `description?`, `acceptance?`, `territory?`, `role` (default builder), `priority` (default 2), `depends_on?`, `milestone_id?` | status `ready`, `created_by` human (REQ-ENG-030) | `{task}` |
  | `update_task` | `id`, `fields` | see REQ-API-041 | `{task}` |
  | `add_task_note` | `id`, `text` | note; mailed to the assignee as "Note on #id", else an event | `{note}` |
  | `comment_decision` | `decision_id`, `body` | REQ-COM-035 comment + routing mail | `{comment}` |
  | `update_config` | `file: "team.yaml"\|"troupe.toml"`, `patch` (keys → values) | validated with REQ-ENG-019 rules, written preserving comments, counts as the human's approval for `[safety]` and `[git] check` (REQ-SAFE-021); invalid → `bad_request` naming the field | `{config}` |
  | `update_memory` | `id`, `pinned?`, `major?`, `title?`, `content?`, `rationale?` | REQ-COM-033/034 | `{memory}` |
  | `delete_memory` | `id` | REQ-COM-033 (the client confirms first) | `{deleted: true}` |
  | `mark_seen` | `key` (`human_last_seen`\|`decisions_seen_at`), `ts?` (default now) | kv write (GUI-028/041); never moves backwards | `{seen}` |
  - Test (`tests/test_api.py`): each command's row/kv/event effect in a temp DB, and each error path listed here.
- **REQ-API-041 [x]** `update_task` uses the human's permissions (REQ-COM-003: the same as the Lead's).
  - Allowed fields: `title, description, acceptance, territory, status, priority (0..3), role, assignee,
    depends_on, milestone_id`. Anything else → `bad_request` naming the field. Unknown assignee → `not_found`.
  - Status rules are REQ-ENG-039's: `done` on a task with a branch sets `approved` (merge then done); `review` wakes
    QA; `approved` also adds the note "Approved by the human."; `ready` resets `attempts` and `next_attempt_at`.
  - The event text matches today's (`You updated #12: status=ready`), or ENG-039's move wording once it lands.
- **REQ-API-042 [~]** Protected-merge approvals are questions of kind `approval` (REQ-SAFE).
  `answer_question` on one requires `decision: "approve"|"reject"` (else `bad_request`); `text` is an optional
  reason. The effect on the task is defined by REQ-SAFE. `decision` is rejected on other kinds.
- **REQ-API-043 [x]** Idempotency: `chat`, `room_message`, `create_task`, `add_task_note` and `comment_decision`
  accept `idempotency_key` (string, ≤64 chars). A repeat of the same key within **10 min** returns the first
  call's result with `"duplicate": true` and no new effect. Keys are stored in the DB, so a retry after a reload or
  reconnect is still deduplicated. Other commands are naturally idempotent or return `conflict`.
  - Test: a repeated `chat` with the same key inserts one message; after 10 min it inserts a second.

## Push events
- **REQ-API-060 [x]** `subscribe {topics?: [str], since_seq?: int}` starts pushes on this connection.
  - `topics` holds event types or prefixes ending in `.*` (`"run.*"`); omitted = all. Calling `subscribe` again
    replaces the filter. `unsubscribe` stops pushes. Result: `{epoch, seq}`.
  - `seq` is a per-epoch monotonic integer across all event types (filtered events still consume numbers).
  - Every event `data` carries the **full current object**, not a diff, so clients just replace by id.
- **REQ-API-061 [x]** Event types (clients ignore unknown types):
  | event | data |
  |---|---|
  | `message.new` | `{message}` |
  | `agent.state` | `{agent}`: any Agent field changed; coalesced to ≤4 per second per agent |
  | `task.changed` | `{task}`: created, any field, a note (`notes_count`) or a flag changed |
  | `question.new`, `question.answered` | `{question}` (answered covers dismissed) |
  | `memory.new`, `memory.changed` | `{memory}` (changed covers pin, edit, supersede, delete: `{id, deleted: true}`) |
  | `decision.comment` | `{comment}` |
  | `run.started`, `run.finished` | `{run}` |
  | `run.output` | `{run_id, agent, line: RunLine}` |
  | `activity.new` | `{activity}` |
  | `engine.state` | `{engine}`, on state change only; heartbeat alone doesn't push |
  | `usage.changed` | `{usage}`, at most 1 per second |
  | `milestone.changed` | `{milestone}` |
  | `seen.changed` | `{seen}` |
  | `resync_required` | `{reason}`: the subscription has ended (REQ-API-062/063) |
  - Clients never need to poll: every change a snapshot or read can show produces one of these.
- **REQ-API-062 [x]** Resume: the server keeps the last 10,000 events or 10 min, whichever is smaller, in memory.
  - `since_seq` inside the ring → the missed events are replayed in order, then live ones follow, with no gap.
  - `since_seq` older than the ring, or a `since_seq` given with a stale epoch (engine restarted or reloaded since
    the client's `hello`) → error `resync_required`. The client re-runs `snapshot`, then subscribes with its `seq`.
  - Test: disconnect, make 5 changes, resume → exactly those 5 replayed; resume past the ring → `resync_required`.
- **REQ-API-063 [x]** Backpressure: the engine never blocks on a client. If a connection's unsent pushes exceed
  **8 MiB**, the server drops them, sends a `resync_required` event and ends that subscription. The connection
  stays open for requests. If unsent output (responses included) exceeds 32 MiB, the connection is closed.
  - Test: a client that subscribes and never reads gets `resync_required`, and the engine heartbeat keeps its
    normal cadence.
- **REQ-API-064 [x]** Latency: an event is pushed within **250 ms (p95)** of the DB commit that caused it, whichever
  process wrote it (engine, an agent's MCP server, the CLI, the raylib GUI). The mechanism is the implementer's
  choice; today's 1 s engine tick alone is not enough.
  - Test: a separate process writes 50 messages through `Store`; p95 from commit to `message.new` < 250 ms.

## Non-functional
- **REQ-API-070 [x]** Performance: `snapshot` for 500 tasks and 5k messages returns in <200 ms. Paged reads at their
  max limit return in <100 ms.
- **REQ-API-071 [x]** The API runs on its own thread (with its own event loop and SQLite connections), never in the
  engine tick. A slow or stuck client, or a slow read, never delays the heartbeat, dispatch or launches.
- **REQ-API-072 [~]** Lifecycle: the socket is up within 1 s of the engine starting. On reload (REQ-ENG-009) it
  closes and reopens with a new `epoch`; clients reconnect, `hello` again, and resync. A clean stop closes all
  connections, then removes the socket.
- **REQ-API-073 [x]** Versioning: `api_version` is an integer. New fields, methods, event types and error `data`
  don't bump it. Removing or renaming anything, or changing a type or meaning, does. Clients must ignore unknown
  fields and unknown event types. The server rejects unknown **params** with `bad_request`, so typos surface.

- **REQ-API-074 [~]** Service state when the socket is down: the supervisor (REQ-ENG-042) writes
  `.troupe/service.json` = `{state: "running"|"restarting"|"crashed"|"stopped", reason, since, restarts}` on every
  transition. With no socket, clients read that file (a read-only local file, like docs) to show "Restarting…" or
  "Crashed" (REQ-GUI-029) instead of a bare "offline".

## Tooling
- **REQ-API-080 [x]** `troupe api <method> [json-params]` connects to the current project's socket, does `hello`,
  calls the method, and prints the result as indented JSON.
  - Exit 0 on `ok`. On an error, it prints `code: message` to stderr and exits 1. With no engine running, it
    prints "engine not running" and exits 2.
  - `troupe api subscribe [json-params]` prints one event per line until Ctrl-C.
- **REQ-API-081 [x]** `tests/test_api.py` uses the Python client against an engine on a temp project and covers:
  hello and version mismatch, handshake_required, every read, every command's DB effect, subscribe and push
  latency, resume, `resync_required` (ring and backpressure), oversize lines, and socket permissions.

## Implementation coverage (#48)
The existing-data surface and transport are implemented and tested through `api_client.Client` against
real Unix sockets. API-006's staged commands return `unavailable` with the owning requirement and task.
Approval decisions and `stop_now` await #42 integration; service reload and supervisor transitions await
#24/#28. `service.write_service_state` currently records running/stopped lifecycle transitions.
`engine.api.notifications_suppressed` exposes connected-client notification ownership for #35.

The stream uses an additive SQLite change journal populated by triggers, collected on the API thread every
50 ms. Provider/model metadata is captured when a run is inserted, so later roster edits do not rewrite
history; runs predating this metadata return empty provider/model strings. Client queues have independent backpressure; a new API instance has a new epoch. Python tests cover
external-process delivery latency, snapshot/paging performance, replay and backpressure, malformed/oversize
requests, commands and idempotency, socket ownership/permissions, and lifecycle. No Swift client integration
is claimed here.

## Open questions
1. Should the API also carry doc/spec file contents (and file-change events) for a future remote client?
2. Auth beyond file permissions if the socket ever leaves the machine (out of scope for v0).


## Out of scope
TCP or remote access, multiple users, cross-project calls, streaming raw backend JSONL (`.troupe/runs/*.jsonl`),
starting an offline engine over the API (the client runs `troupe up`; REQ-ENG-001).

## Changelog
- 2026-09-23 — API v0 written (human confirmed the native SwiftUI app; pm msg #110).
- 2026-09-23 — `stopped` engine state (kill switch), `mail_reading`, client `notifications` presence, `update_config`
  (Settings go through the API, so validation stays in one place), and `service.json` for crash/restart visibility
  (closes former open questions 3 and 4).
- 2026-09-24 — API-006 staged availability + `unavailable` error: #48 ships the full contract shape, owning tasks light
  up their methods (builder-2 msg #293).

- 2026-09-24 — #48 implements the existing-data NDJSON API, Python client and CLI; staged domains follow API-006.
