# Codex usage numbers (REQ-BE-014)

Where troupe gets Codex's rate-limit percentages and reset times, and why.

## Sources checked

| Source | Verdict |
| --- | --- |
| `codex exec --json` stdout (what troupe already captures per run) | Does **not** carry `rate_limits`. Only `token_count` totals for the run itself; no window/percent/reset data (codex-cli 0.156.1). Confirmed by grepping every run's `.troupe/runs/*.jsonl`. |
| `codex` CLI status/usage subcommand | No such subcommand exists in 0.156.1 (`codex --help` has no `usage`/`status`/`limits` verb). |
| Session rollout files, `<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*-<thread-id>.jsonl` | **Used as the fallback.** Every `token_count` event's `payload.rate_limits` carries `primary`/`secondary` windows (`used_percent`, `window_minutes`, `resets_at`), `plan_type`, and `rate_limit_reached_type`. The thread id in the filename is the session id troupe already stores per codex agent. |
| `codex app-server --stdio`, `account/rateLimits/read` | **Used as the primary source.** Read-only JSON-RPC method that returns the same `rate_limits` shape (camelCase: `usedPercent`/`windowDurationMins`/`resetsAt`) without needing a completed run or file access. It costs no model quota — it's a metadata call, not a turn. |

## Isolated CODEX_HOME (REQ-BE-015, #62)

Since #62, each codex agent runs under its own `.troupe/codex-home/<agent>` rather than the human's real
`~/.codex` — so there's no longer one shared sessions directory, and an app-server subprocess that inherited the
engine's own environment would default to the human's real `~/.codex` config (plugins, ChatGPT-account
connectors) or whatever `CODEX_HOME` the engine process happens to have. Both readers here are explicit about
which home they use instead of relying on any inherited default:

- `rollout_usage(session_ids, codex_home)` takes the home as a required argument — no environment-variable
  fallback. Callers always pass a specific agent's `codex_home_dir(cfg, agent.id)`.
- `app_server_usage(command, cwd, codex_home)` launches the RPC subprocess with `CODEX_HOME` set explicitly to
  the given home, overriding whatever the engine process's own environment has. The caller (`usage.monitor()`)
  first calls `ensure_codex_home()` for a codex agent to guarantee that home's `[features]` allowlist config
  exists before the read runs under it.
- Since `auth.json` is symlinked identically into every codex agent's isolated home, any one of them reads the
  same account/quota — the monitor just picks the first configured codex agent for the RPC's home, and merges
  the rollout fallback's per-agent results by `observed_at` across every codex agent that has a session.

## What troupe does

1. **Primary: app-server RPC.** `usage.app_server_usage()` spawns `codex app-server --stdio` under a codex agent's isolated `CODEX_HOME`, sends `initialize` → `initialized` → `account/rateLimits/read`, reads the one response, and tears the process down immediately (`terminate()`, then `kill()` after a 1s grace period). The whole exchange is wrapped in a 5s `asyncio.timeout`; on timeout, error, or a process that won't exit, it returns `None` and the caller falls back. Nothing is left running.
2. **Fallback: rollout-file parsing.** `usage.rollout_usage()` looks up `<codex_home>/sessions/*/*/*/rollout-*-<session-id>.jsonl` for each codex session id troupe holds, in that session's own agent's isolated home, reads only the last 4MB of each file (bounded I/O — these logs can grow large), and takes the most recent well-formed `token_count` event's `rate_limits`. Malformed lines, non-matching event types, and any file I/O error are skipped, not raised.
3. **Triggers.** After every codex run, `engine.py`'s `_run` (not `runners.py` — that file is protected under REQ-SAFE-020, and this post-run bookkeeping doesn't belong there) parses the fresh session's rollout from that agent's own isolated home and applies it via `apply_usage`. Independently, `usage.monitor()` runs off the engine's own asyncio loop (not the 60s scheduler tick — that stays free for its purpose) and refreshes at most once a minute: RPC first, rollout fallback (across every codex agent's own home) if the RPC is unavailable or times out, but only when the roster currently has a codex agent.
4. **Storage.** Both paths funnel through `usage.normalize()`, an allowlist that keeps only `used_percent`, `window_minutes`, `resets_at`, `plan_type`, and `rate_limit_reached_type` — everything else in the payload (`credits`, account identifiers, any nested extra field) is dropped before it ever reaches disk. The result is written to kv `usage:codex` in the same shape as `claude_ratelimit`'s `unifiedWindows` (a dict keyed `five_hour`/`seven_day` when the window matches Codex's known durations, else `window_<minutes>`), plus `plan_type`, `rate_limit_reached_type`, `observed_at`, and `source` (`"app-server"` or `"rollout"`). A newer sample never overwrites a fresher one already in kv (`apply_usage` checks `observed_at`).
5. **Backoff (REQ-ENG-016).** `usage.limited_until()` treats a window at `used_percent >= 100`, or the window named by `rate_limit_reached_type`, as exhausted, and returns the latest `resets_at` among exhausted windows. If a reported reset is missing, it falls back to `observed_at + 900s` so a stale reached-state can't wedge the backend forever. `Engine.record_limit("codex", until, reported=...)` is called with that time, same as the existing Claude/local backoff path.
6. **Display.** `gui/app.py`'s top bar reads `usage:codex` and `claude_ratelimit` from the already-collected kv snapshot (`gui/data.py`, no file I/O in draw code) and renders a meter per window for each provider, marking a Codex meter stale (`~`) if the sample is older than 120s.

## Privacy (Principle 0)

Only `rate_limits` (via the allowlist above) and the `token_count` event's own metadata (timestamp, session id) are read, and only from the reading agent's own isolated `CODEX_HOME` — never the human's real `~/.codex`, and never another agent's home. No conversation content, tool output, or credentials are copied out — `test_codex_usage.py::test_rollout_allowlist_windows_and_malformed_lines` and `test_post_run_updates_store_without_logging_conversation` assert this by checking that private markers written into fixture files never appear in what gets persisted to the store. `test_rpc_runs_under_the_isolated_codex_home_not_the_humans` and `test_post_run_ignores_a_foreign_agents_codex_home` assert the isolation itself: the RPC subprocess's `CODEX_HOME` env is the isolated home (not an inherited one), and a rollout sitting under a different agent's home is never picked up.

## Fallback order in practice

`app-server RPC → rollout file → nothing` (existing cached `usage:codex` sample is kept; a run without either source doesn't clear a still-valid reading). If both are unavailable — no codex CLI on PATH, or a brand new session with no rollout yet — `usage:codex` simply stays unset and codex is treated as uncapped per BE-011, same as before this change.
